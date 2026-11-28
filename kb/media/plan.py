"""把抓取层的 media 列表裁成「待下载清单」。**纯函数，一个 `list[dict]` 就能断言。**

## 为什么封面的判定必须写死在这里，而不是留给调用方

真机实测（2026-11-28，抖音视频第 12 集）：

```
media count: 1
[0] role='video' ext='mp4' 768x576
    url   = https://v95-aw-cold.douyinvod.com/...     ← mp4
    thumb = https://p95-zjwztc-sign.douyinpic.com/...jpeg?x-expires=...
```

而 `kb/extract/normalize.py` 给 `role` 的取值只有 `video` / `image` 两种
（`default_role = "video" if post_type == "video" else "image"`）——**`role='cover'`
今天没有任何生产者**。所以「遍历每个 media 下载 url」的结果是：封面一张下不到，
反而把决定不下的 mp4 全下了。

## 三条互斥的回落链

| 情形 | `thumbUrl` | 本设计的处理 |
|---|---|---|
| 视频有封面 | 有值（实测） | `role='cover'`，取 `thumbUrl` |
| 图文 | 无（图文项没有该字段） | **不额外产封面**，`primary_media` 指向第一张图 |
| 文章 / 抓不到封面 | 无 | 不产 `role='cover'`，`primary_media` 留 `null` |

漏掉第 2 行会让图文条目的卡片**全部没有封面**，而图文是小红书/微博这类内容的
主形态。漏掉第 3 行会让 `primary_media` 指向一个不存在的附件 id。

## 为什么第 2 行不是「再产一条 role='cover'」

设计 §0.2 原本写的是「同一张图会同时有一条 `role='cover'` 和一条 `role='image'`，
靠 `storage_key` 的 `sha256(item_id+内容)` 去重成**一份文件、两条 attachments 行**」。

**这句话在 schema 上不成立**，真机验证时抓到了：`attachments_key_uniq` 是

```sql
create unique index attachments_key_uniq on attachments (storage_key);
```

——`storage_key` 上的**全局**唯一索引，索引里**没有 `role`**。所以同一个 key 插
第二行必然撞索引，而 `replace_for_item` 不写 `on conflict do nothing`（写了等于
把数据完整性异常伪装成正常），结果是**整条 `replace_for_item` 失败**，图文条目
一张图都存不下来。真机复现的形态正是如此：cover 与 image 派生出了同一个
`storage_key`。

两条出路，选后者：

| 出路 | 代价 |
|---|---|
| key 里混入 `role` | 同一张图在磁盘上存两份（图文每条多 ~150 KB），换「cover 行一定存在」 |
| **不额外产封面行**（本实现） | 图文条目没有 `role='cover'` 的行；靠 `primary_media` 指向第一张 `role='image'` |

选后者的理由：封面与第一张图字节**完全相同**，为它多存一份文件正是设计
§0.3 想避免的那类浪费；而 `items.primary_media` 的语义是「**这条内容用来做
展示图的那张附件**」，不是「`role='cover'` 的那张」——第一张图完全满足它。
前端按 `primary_media` 取图，不需要知道那张附件的 `role` 是什么。

`attachments.primary_media_id` 因此改成「优先 cover，没有则取 sort_order 最小的
image」，这个顺序保证视频（有 cover）与图文（无 cover）两种形态都拿得到展示图。

## 数量上限

抖音图文可达 35 张。PRD §5.5 只限了单张字节、没限条数，不设上限会让单次 fetch
的耗时从 20 秒涨到分钟级。上限 20 张，超出截断，真实总数由 `image_total` 带出去
写 `props.mediaTruncated`。
"""

from __future__ import annotations

from typing import Final

from .models import MediaSpec

#: 每条内容最多下载的图片数。抖音图文上限约 35 张，20 张足够覆盖「多图」语义，
#: 又不让单次 fetch 超时。
MAX_IMAGES_PER_ITEM: Final[int] = 20

#: 单文件原始字节上限。与 `config.MAX_SOURCE_BYTES` 同值，在这里再导出一次是为了
#: 让「计划阶段就要知道体积上限」这件事在签名旁可见（调用方从 plan 取，不另 import）。
MAX_SOURCE_BYTES: Final[int] = 8 * 1024 * 1024

#: `media` 项里 `thumbUrl` 的键名。camelCase 与 `store.items.apply_extract`
#: 收的 `durationSec` 保持一致——worker 是唯一的翻译者，两边命名必须对齐。
THUMB_KEY: Final[str] = "thumbUrl"

_ROLE_COVER: Final[str] = "cover"
_ROLE_IMAGE: Final[str] = "image"
_ROLE_VIDEO: Final[str] = "video"


#: 判定「这是一张图」用的扩展名集合。**只在 `role` 缺失时兜底**，不参与正常判定。
_IMAGE_EXTS: Final[frozenset[str]] = frozenset(
    {"jpg", "jpeg", "png", "gif", "webp", "avif", "heic", "bmp"}
)


def _text(media: dict[str, object], key: str) -> str:
    """取一个非空字符串字段。类型不对、空串、纯空白一律当「没有」。"""
    value = media.get(key)
    if isinstance(value, str):
        return value.strip()
    return ""


def _is_image(media: dict[str, object]) -> bool:
    """这条 media 是不是一张图。

    `role` 有值时**只信 `role`**：视频的 `url` 是 mp4，本轮不下。

    `role` 缺失时按 `ext` 兜底。这一步是必要的防御，不是洁癖——本层收的是
    `list[dict]`，由 worker 从抓取层翻译过来，而「翻译时漏了 `role` 键」
    真机发生过一次：抖音图文的 `specs` 静默变成空列表，日志里只有一句
    「specs 为空」，看起来像「这条内容本来就没图」。兜底让这类漂移表现为
    「图片照常下载」，而不是一类内容整体失去封面。
    """
    role = _text(media, "role")
    if role:
        return role == _ROLE_IMAGE
    return _text(media, "ext").lower() in _IMAGE_EXTS


def _image_urls(media: list[dict[str, object]]) -> list[str]:
    """按顺序取图片的 url，**按 url 去重**（保首次出现）。

    去重的理由：同一个 url 出现两次时，两份内容必然一样、`storage_key` 也一样，
    结果是两条指向同一个文件的 attachments 行——详情页会出现两张一模一样的图，
    而用户以为是两条不同内容。抓取层偶发重复（图文里的 `backup_urls` 混入主列表）
    时这里能兜住。
    """
    urls: list[str] = []
    seen: set[str] = set()
    for item in media:
        if not _is_image(item):
            continue
        url = _text(item, "url")
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _cover_url(media: list[dict[str, object]], images: list[str]) -> str:
    """封面取哪一条。**只看 `thumbUrl`，不对图片做回落。**

    为什么不回落到 `images[0]`：那张图已经会以 `role='image'` 被下载一次，
    内容与 key 完全相同。再产一条 `role='cover'` 会让两行指向同一个
    `storage_key`，而 `attachments_key_uniq` 是 `storage_key` 上的全局唯一索引
    （索引里没有 `role`）→ 第二行撞索引 → **整条 `replace_for_item` 失败**，
    图文内容一张图都存不下来。真机复现过，详见模块 docstring。

    图文的封面由 `items.primary_media` 指向第一张 `role='image'` 承担，
    不需要一条 `role='cover'` 行。
    """
    for item in media:
        thumb = _text(item, THUMB_KEY)
        if thumb:
            return thumb
    # `images` 参数保留在签名里：将来若 key 设计改成混入 role，
    # 「回落到第一张图当封面」只需改这里一处，不用改调用方。
    del images
    return ""


def plan_attachments(
    media: list[dict[str, object]],
    *,
    kind: str,
    max_images: int = MAX_IMAGES_PER_ITEM,
) -> list[MediaSpec]:
    """把抓取层的 media 列表裁成待下载清单。

    四条规则，前三条互斥（产封面），第四条按序产出图片：

    1. 有 `thumbUrl` → 一条 `role='cover'`，`sort_order=0`
    2. 无 `thumbUrl` 但有图片 → **不产封面行**（第一张图就是它，见
       `_cover_url` 的注释）；`items.primary_media` 由 store 层指向那张图
    3. 两者都没有 → **不产封面**
    4. 每条图片的 url → `role='image'`，`sort_order` 从 1 递增，
       最多 `max_images` 条

    `role='video'` 的 url **本轮不下**（PRD M4-07 把视频原文件定为 P2「按需」，
    PRD §5.5「视频默认不存原文件，只存封面 + 字幕 + 摘要」）。这不是技术细节，
    是产品层面已做掉的决策。

    `kind` 目前不参与分支，但保留在签名里：将来「文章不下载配图」这类规则
    应当落在这里，而不是散到调用方。它的存在让加规则时不用改调用点。
    """
    del kind  # 显式说明「本轮按 media 的形状分支，不按 kind」
    if max_images < 0:
        raise ValueError("max_images 不能为负")

    images = _image_urls(media)
    specs: list[MediaSpec] = []

    cover = _cover_url(media, images)
    # `thumbUrl` 恰好等于某张图的 url 时**不产 cover**：两条 spec 会派生同一个
    # `storage_key`（同内容同 item），真库里撞 `attachments_key_uniq`，
    # 整条 `replace_for_item` 失败。图那一行已经在了，`primary_media`
    # 的 image 回落会指向它。这是极端情形但代价是「一张图都存不下来」。
    if cover and cover not in images:
        specs.append(MediaSpec(role=_ROLE_COVER, url=cover, sort_order=0))

    for index, url in enumerate(images[:max_images], start=1):
        specs.append(MediaSpec(role=_ROLE_IMAGE, url=url, sort_order=index))
    return specs


def image_total(media: list[dict[str, object]]) -> int:
    """`role='image'` 的条数（去重后），**不受上限影响**。

    单独一个函数而不是把总数塞进 `MediaSpec` 的返回值里：`plan_attachments`
    的契约是「待下载清单」，混进一个「统计值」会让每个遍历它的调用方都要跳过
    那一条。worker 用它和实际产出的图片数比较，就能知道有没有截断。
    """
    return len(_image_urls(media))


def dropped_images(media: list[dict[str, object]], specs: list[MediaSpec]) -> int:
    """被上限截掉的图片数。0 表示没截断，worker 据此决定写不写 `props.mediaTruncated`。

    判据用「真实总数 − 实际产出的图片数」而不是「真实总数 − 上限」：后者在
    上限大于总数时算出负数，而「没截断」必须能用 `== 0` 判断，不能靠符号。
    """
    produced = sum(1 for spec in specs if spec.role == _ROLE_IMAGE)
    total = image_total(media)
    return max(total - produced, 0)

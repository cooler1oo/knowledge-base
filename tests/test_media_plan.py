"""封面合成、数量上限、图文与视频分支（纯函数，一个 `list[dict]` 就能断言）。

**这一组守的是设计 §0.2 的「缺口一」，它是本轮最容易犯且最难发现的错。**

真机实测（2026-11-28，抖音视频第 12 集）：

```
media count: 1
[0] role='video' ext='mp4' 768x576
    url   = https://v95-aw-cold.douyinvod.com/...        ← mp4
    thumb = https://p95-zjwztc-sign.douyinpic.com/...jpeg ← 封面
```

而 `kb/extract/normalize.py` 给 `role` 的取值只有 `video` / `image`——**`role='cover'`
今天没有任何生产者**。所以「遍历每个 media 下载 url」的结果是：封面一张下不到，
反而把决定不下的 mp4 全下了。

真机验证还抓到一个真 bug：worker 翻译 media 时**漏了 `role` 键**，导致图文的
`specs` 静默变成空列表（日志里只有一句「specs 为空」，看起来像「这条内容本来
就没图」）。`tests/test_media_worker.py` 钉住那个翻译，这里钉住 plan 本身的
兜底逻辑。
"""

from __future__ import annotations

import pytest

from kb.media import plan

# 真机结构：视频只有一条 media，封面在 thumbUrl
VIDEO_MEDIA = [
    {
        "role": "video",
        "url": "https://v95-aw-cold.douyinvod.com/v.mp4",
        "ext": "mp4",
        "thumbUrl": "https://p95-zjwztc-sign.douyinpic.com/t.jpeg?x-expires=1797048000",
        "width": 768,
        "height": 576,
        "durationSec": 445,
    }
]

# 真机结构：图文没有 thumbUrl，图片在 url 里
IMAGE_TEXT_MEDIA = [
    {
        "role": "image",
        "url": "https://p95-zjwztc-sign.douyinpic.com/0.jpg",
        "ext": "jpg",
        "width": 1440,
        "height": 2131,
    },
    {
        "role": "image",
        "url": "https://p95-zjwztc-sign.douyinpic.com/1.jpg",
        "ext": "jpg",
        "width": 1440,
        "height": 2131,
    },
    {
        "role": "image",
        "url": "https://p95-zjwztc-sign.douyinpic.com/2.jpg",
        "ext": "jpg",
        "width": 1440,
        "height": 2131,
    },
]


# ── 缺口一：封面只可能来自 thumbUrl（视频）────────────────────


def test_视频封面取自thumbUrl而不是url():
    """**这是本模块最重要的用例。**

    视频的 `media[0].url` 是 mp4。若按「遍历 media 下载 url」实现，
    这条会产出 role='video' 的 mp4 下载任务而封面为零——正好反了。
    """
    specs = plan.plan_attachments(VIDEO_MEDIA, kind="video")

    assert [s.role for s in specs] == ["cover"], (
        f"视频应只产出一条封面，实际 {[s.role for s in specs]}"
    )
    assert specs[0].url == VIDEO_MEDIA[0]["thumbUrl"], "封面必须取 thumbUrl"
    assert specs[0].sort_order == 0


def test_视频的mp4本轮不下():
    """PRD M4-07 把视频原文件定为 P2「按需」，PRD §5.5 写「默认不存原文件」。

    这是**产品层面已经做掉的决策**，不是技术实现细节。断言里必须能看到
    「mp4 的 url 没有出现在任何 spec 里」。
    """
    specs = plan.plan_attachments(VIDEO_MEDIA, kind="video")
    assert all(".mp4" not in s.url for s in specs), "视频原文件不该被下载"
    assert all(s.role != "video" for s in specs), "本轮不产出 role='video'"


# ── 缺口一第二行：图文的回落 ─────────────────────────────────


def test_图文没有thumbUrl时不产cover行而只产image():
    """**真机验证抓到的第二个 bug 的回归用例。**

    设计 §0.2 原本写「同一张图会同时有一条 `role='cover'` 和一条 `role='image'`，
    靠 `storage_key` 的 `sha256(item_id+内容)` 去重成**一份文件、两条 attachments
    行**」。但 `attachments_key_uniq` 是

        create unique index attachments_key_uniq on attachments (storage_key);

    ——`storage_key` 上的**全局**唯一索引，索引里没有 `role`。所以同一个 key
    插第二行必然撞索引，而 `replace_for_item` 不写 `on conflict do nothing`，
    结果是**整条写入失败**，图文一张图都存不下来。真机复现的形态正是：
    cover 与 image 派生出了同一个 `storage_key`。

    修法：图文不额外产封面行。展示图由 `items.primary_media` 指向第一张
    `role='image'` 承担（`attachments.primary_media_id` 的 image 回落）。
    """
    specs = plan.plan_attachments(IMAGE_TEXT_MEDIA, kind="image_text")

    assert [s.role for s in specs] == ["image", "image", "image"], (
        f"图文不该产 cover 行（会撞 storage_key 唯一索引），"
        f"实际 {[s.role for s in specs]}"
    )
    assert [s.sort_order for s in specs] == [1, 2, 3]


def test_图文的spec里URL互不重复():
    """**上面那个 bug 的直接判据。**

    同一 URL 被规划两次 → 同一字节 → 同一个 `storage_key` → 真库撞唯一索引。
    """
    specs = plan.plan_attachments(IMAGE_TEXT_MEDIA, kind="image_text")
    urls = [s.url for s in specs]
    assert len(set(urls)) == len(urls), f"同一 URL 被规划了两次：{urls}"


def test_视频有thumbUrl时仍产cover行():
    """视频必须有一条 `role='cover'`：封面只在 `thumb_url` 里，不在图片列表里。"""
    specs = plan.plan_attachments(VIDEO_MEDIA, kind="video")
    assert [s.role for s in specs] == ["cover"]
    assert specs[0].url == VIDEO_MEDIA[0]["thumbUrl"]


def test_既有thumbUrl又有图片时两条都产且URL不重复():
    """图文偶尔会有 `thumbUrl`（封面图）。此时 cover 与 image 是**不同 URL**，
    不撞 key，两条都该产。
    """
    media = [
        {
            "role": "video",
            "url": "https://x.invalid/v.mp4",
            "ext": "mp4",
            "thumbUrl": "https://x.invalid/cover.jpg",
        },
        {"role": "image", "url": "https://x.invalid/a.jpg", "ext": "jpg"},
    ]
    specs = plan.plan_attachments(media, kind="video")
    assert [s.role for s in specs] == ["cover", "image"]
    assert len({s.url for s in specs}) == 2, "两条 spec 指向同一 URL 会撞 storage_key"


def test_thumbUrl恰好等于某张图片时只留一条():
    """极端情形：`thumbUrl` 与某张图的 url 完全相同。

    此时必须**只留一条**，否则两条 spec 派生同一个 `storage_key`（同 item 同内容），
    真库里撞 `attachments_key_uniq`，整条 `replace_for_item` 失败。

    保留 `role='image'` 而不是 `role='cover'`：两者字节与 key 完全相同，
    留哪一条对展示都没影响——`primary_media_id` 的排序是「cover 先于 image」，
    没有 cover 时回落取 image，所以展示图仍然拿得到。
    """
    shared = "https://x.invalid/same.jpg"
    media = [
        {
            "role": "video",
            "url": "https://x.invalid/v.mp4",
            "ext": "mp4",
            "thumbUrl": shared,
        },
        {"role": "image", "url": shared, "ext": "jpg"},
    ]
    specs = plan.plan_attachments(media, kind="video")
    assert len(specs) == 1, f"同一 URL 被规划了两次：{[s.role for s in specs]}"
    assert specs[0].url == shared


def test_文章无图无封面时不产cover():
    """漏了这条，`items.primary_media` 会指向一个**不存在的附件 id**。

    前端按无封面渲染占位（M7-01 的「不裁切」要求下，占位比拉伸一张错图好）。
    """
    specs = plan.plan_attachments(
        [{"role": "doc", "url": "https://x.invalid/a.pdf", "ext": "pdf"}],
        kind="article",
    )
    assert specs == []


def test_空media列表返回空清单():
    assert plan.plan_attachments([], kind="article") == []


def test_thumbUrl为空串时不产cover():
    """`thumb_url` 可能是空串而不是 `None`。两者都必须当「没有」。

    注意：图文**不会**因此失去封面——展示图由 `items.primary_media`
    指向第一张 `role='image'`（`attachments.primary_media_id` 的回落），
    而不是靠一条 `role='cover'` 行。
    """
    media = [
        {
            "role": "video",
            "url": "https://x.invalid/v.mp4",
            "ext": "mp4",
            "thumbUrl": "",
        },
        {"role": "image", "url": "https://x.invalid/a.jpg", "ext": "jpg"},
    ]
    specs = plan.plan_attachments(media, kind="video")
    assert [s.role for s in specs] == ["image"], "空串 thumbUrl 不该产 cover"


def test_thumbUrl为纯空白时也当没有():
    media = [{"role": "video", "url": "https://x.invalid/v.mp4", "thumbUrl": "   "}]
    assert [s.role for s in plan.plan_attachments(media, kind="video")] == []


# ── 缺口三：数量上限 ─────────────────────────────────────────


def _many_images(n: int) -> list[dict[str, object]]:
    return [
        {"role": "image", "url": f"https://x.invalid/{i}.jpg", "ext": "jpg"}
        for i in range(n)
    ]


def test_抖音图文的35张被截到20张():
    """抖音图文可达 35 张。PRD §5.5 只限了单张字节、没限条数。

    不设上限，一条 35 图的内容会让单次 fetch 的耗时从 20 秒涨到分钟级。
    """
    specs = plan.plan_attachments(_many_images(35), kind="image_text")
    images = [s for s in specs if s.role == "image"]
    assert len(images) == plan.MAX_IMAGES_PER_ITEM == 20
    assert plan.MAX_IMAGES_PER_ITEM == 20


def test_截断后sort_order仍然连续():
    """截断不能留下空洞：详情页按 sort_order 排列，空洞会让顺序看起来跳号。"""
    specs = plan.plan_attachments(_many_images(35), kind="image_text")
    images = [s.sort_order for s in specs if s.role == "image"]
    assert images == list(range(1, 21))


def test_少于上限时全部保留():
    specs = plan.plan_attachments(_many_images(3), kind="image_text")
    assert len([s for s in specs if s.role == "image"]) == 3


def test_max_images可覆盖():
    specs = plan.plan_attachments(_many_images(10), kind="image_text", max_images=2)
    assert len([s for s in specs if s.role == "image"]) == 2


def test_max_images为0时图片全截掉():
    """上限 0 是合法输入（虽然生产不用）：图片全截掉、也不产封面。

    封面在图文里就是第一张图，图片被截光之后没有可当展示图的东西——
    返回空清单，`item.primary_media` 留 `null`，前端按无封面渲染占位。
    """
    specs = plan.plan_attachments(_many_images(3), kind="image_text", max_images=0)
    assert specs == []


def test_负的max_images是编程错误():
    with pytest.raises(ValueError):
        plan.plan_attachments(_many_images(1), kind="image_text", max_images=-1)


def test_dropped_images算出截断数():
    """`props.mediaTruncated` 写的是**真实总数**，让人知道还有多少没用上。"""
    media = _many_images(35)
    specs = plan.plan_attachments(media, kind="image_text")
    assert plan.dropped_images(media, specs) == 15


def test_没截断时dropped为0():
    """必须是 `0` 而不是负数：`props.mediaTruncated` 的存在与否靠 `> 0` 判断。"""
    media = _many_images(3)
    specs = plan.plan_attachments(media, kind="image_text")
    assert plan.dropped_images(media, specs) == 0


def test_image_total不受上限影响():
    assert plan.image_total(_many_images(35)) == 35


# ── 去重与脏数据 ─────────────────────────────────────────────


def test同一url出现两次只下载一次():
    """同一个 url 两份内容必然一样、key 也一样，结果是两条指向同一文件的
    附件行——详情页会出现两张一模一样的图，用户以为那是两条不同内容。
    """
    media = [
        {"role": "image", "url": "https://x.invalid/same.jpg", "ext": "jpg"},
        {"role": "image", "url": "https://x.invalid/same.jpg", "ext": "jpg"},
    ]
    specs = plan.plan_attachments(media, kind="image_text")
    images = [s for s in specs if s.role == "image"]
    assert len(images) == 1


def test缺url的条目被跳过():
    media = [
        {"role": "image", "ext": "jpg"},
        {"role": "image", "url": "", "ext": "jpg"},
        {"role": "image", "url": "https://x.invalid/ok.jpg", "ext": "jpg"},
    ]
    images = [
        s.url
        for s in plan.plan_attachments(media, kind="image_text")
        if s.role == "image"
    ]
    assert images == ["https://x.invalid/ok.jpg"]


def test非字符串url被跳过():
    """抓取层偶发把数字塞进 url 字段。`str()` 一下会得到 `'123'` 这种假 URL，
    下载必然失败，但失败原因看起来像「CDN 挂了」。这里直接跳过。"""
    media = [{"role": "image", "url": 123, "ext": "jpg"}]
    assert plan.plan_attachments(media, kind="image_text") == []


def test_role缺失时按ext兜底():
    """**真机抓到的 bug 的回归用例。**

    worker 翻译 media 时漏了 `role` 键 → `_image_urls` 一条都匹配不到 →
    图文的 `specs` 静默变成空列表。日志里只有一句「specs 为空」，
    看起来像「这条内容本来就没图」。

    兜底让这类漂移表现为「图片照常下载」，而不是一类内容整体失去封面。
    注意图文不产 `role='cover'` 行（见 `test_图文没有thumbUrl时不产cover行
    而只产image`）——展示图靠 `primary_media` 指向这张 image。
    """
    media = [{"url": "https://x.invalid/a.jpg", "ext": "jpg"}]
    specs = plan.plan_attachments(media, kind="image_text")
    assert [s.role for s in specs] == ["image"], f"ext 兜底没生效：{specs}"


def test_role缺失且ext不是图片时不当图片():
    media = [{"url": "https://x.invalid/v.mp4", "ext": "mp4"}]
    assert plan.plan_attachments(media, kind="video") == []


def test_role有值时不信ext():
    """`role='video'` 但 `ext='jpg'` 也不能当图——`role` 是权威，
    `ext` 只是缺它时的兜底。"""
    media = [{"role": "video", "url": "https://x.invalid/v.mp4", "ext": "jpg"}]
    assert plan.plan_attachments(media, kind="video") == []


# ── kind 参数 ────────────────────────────────────────────────


def test_kind当前不参与分支但签名保留():
    """`kind` 保留是为了让将来「文章不下载配图」这类规则落在 plan 里，
    而不是散到调用方。本轮它的存在只是让加规则时不用改调用点。
    """
    same_video = plan.plan_attachments(VIDEO_MEDIA, kind="video")
    same_other = plan.plan_attachments(VIDEO_MEDIA, kind="article")
    assert same_video == same_other

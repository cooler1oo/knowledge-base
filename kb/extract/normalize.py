"""抓取层的中间数据结构与归一化。

存在的唯一理由：ParseHub 的 `to_dict()["media"]` 有**三种形状**——
视频给 `dict`、图文给 `list[dict]`、无媒体给 `None`（源码 `result.py:68-83`）。
不归一，调用方就得在每处判断类型；只按视频写，遇到图文会对 `list` 调 `.get()` 崩。

三条实测事实写死在换算里，不要「优化」掉：

1. **`duration` 的单位因平台而异**，上游库自己不一致（2026-11-27 实测）：

   | 平台 | 实测值 | 单位 |
   |---|---|---|
   | 抖音 | `445071`（整数） | 毫秒 |
   | B站 | `302.934`（浮点） | **秒** |

   ParseHub 源码注释写「单位: 秒」——对 B站 成立、对抖音不成立。所以既不能
   一律 `// 1000`（B站 会变 0），也不能一律当秒（抖音会变 123 小时）。

   判据用**数值量级**而不是平台名：真实视频不可能超过 ~12 小时，
   即秒值上限 43200、毫秒值下限 43200000。平台名会变（有 25 个），量级不会。

2. **图文的 `backup_urls` 字段存在但为空**，视频有 3 条。两种都要能取，
   所以统一走 `.get("backup_urls") or []`。

3. **浮点必须能收**。B站 给 `302.934`；上一版 `_as_int` 对 float 返回 `None`，
   导致 B站 时长整条丢失（线上表现为「视频没有时长」）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 与 `item_kinds` 表登记的预设值对齐（技术方案 §6.2）
KIND_VIDEO = "video"
KIND_IMAGE_TEXT = "image_text"
KIND_ARTICLE = "article"
KIND_IMAGE = "image"
KIND_LINK = "link"

# 有长正文的门槛，来自技术方案 §5.1 的 decide_kind
ARTICLE_WORD_COUNT = 300
IMAGE_TEXT_MIN_IMAGES = 3

# `duration` 单位是**毫秒**的平台。实测抖音是毫秒，B站是秒。
# 为什么按平台列而不是按数值猜：3000 既可能是抖音的 3 秒短视频（毫秒），
# 也可能是某个平台的 50 分钟长视频（秒），阈值无法区分。
_MS_PLATFORMS = frozenset({"douyin", "tiktok"})

# ParseHub 的 PostType → 本项目 kind。未登记的值一律降级为 link，
# 因为「记录不能丢」优先于「类型猜得准」（技术方案 §4.10）。
_POST_TYPE_TO_KIND: dict[str, str] = {
    "video": KIND_VIDEO,
    "image": KIND_IMAGE,
}


@dataclass(frozen=True)
class MediaItem:
    """一条媒体。`role` 与 `attachments.role` 的取值对齐。"""

    role: str
    url: str
    ext: str
    thumb_url: str | None = None
    width: int | None = None
    height: int | None = None
    # None 表示「这个概念不适用于该媒体」（如图片），0 表示「时长确实是 0」
    duration_sec: int | None = None
    backup_urls: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExtractResult:
    """抓取层对外的唯一契约。抓取层不碰数据库，只产出它。"""

    kind: str
    title: str
    content_text: str
    raw_url: str
    platform: str
    media: list[MediaItem] = field(default_factory=list)


def _as_media_dicts(media: object) -> list[dict[str, Any]]:
    """把三种形状统一成列表。这是本模块的核心。

    视频：`{"url": ...}` → `[{...}]`
    图文：`[{"url": ...}, ...]` → 原样
    无媒体：`None` → `[]`
    """
    if media is None:
        return []
    if isinstance(media, list):
        return [m for m in media if isinstance(m, dict)]
    if isinstance(media, dict):
        return [media]
    return []


def _as_int(value: object) -> int | None:
    """宽转 int。JSON 里数字可能以字符串形式来，非数字给 None。

    **浮点必须收**：B站 的 `duration` 给的是 `302.934`。上一版这里对 float
    直接落到 `return None`，导致 B站 视频时长整条丢失（用户看到「没有时长」）。
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool 是 int 的子类，必须先排除
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            # "302.934" 这类字符串也要能收
            try:
                return int(float(text))
            except ValueError:
                return None
    return None


def _duration_to_seconds(raw_value: object, platform: str) -> int | None:
    """把平台的时长统一换算成秒。

    单位**必须按平台判**，不能按数值量级猜。理由是数值量级存在歧义：
    抖音的 3 秒短视频给 `3000`（毫秒），而 B站 的 50 分钟长视频可能给 `3000`
    （秒）——同一个数值在两种单位下都对应真实视频，无法用阈值区分。

    已知平台（2026-11-27 实测）：

    | 平台 | 实测值 | 单位 |
    |---|---|---|
    | 抖音 douyin | 445071 | 毫秒 |
    | B站 bilibili | 302.934 | 秒 |

    未登记的平台按**秒**处理（ParseHub 注释的口径），因为多数平台沿用秒；
    抖音是已知的例外。若接入新平台后发现时长明显不合理，往 `_MS_PLATFORMS` 加一项。
    """
    raw = _as_int(raw_value)
    if raw is None:
        return None
    if platform in _MS_PLATFORMS:
        return raw // 1000
    return raw


def _as_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _to_media_item(raw: dict[str, Any], role: str, platform: str) -> MediaItem | None:
    """转一条媒体。没有 `url` 的丢弃——没有地址的附件无法下载也无法回溯。"""
    url = _as_str(raw.get("url")).strip()
    if not url:
        return None

    ext = _as_str(raw.get("ext")).strip()
    duration_sec = _duration_to_seconds(raw.get("duration"), platform)

    thumb = _as_str(raw.get("thumb_url")).strip() or None
    backups = raw.get("backup_urls")
    backup_list = (
        [u for u in backups if isinstance(u, str) and u.strip()]
        if isinstance(backups, list)
        else []
    )

    return MediaItem(
        role=role,
        url=url,
        ext=ext,
        thumb_url=thumb,
        width=_as_int(raw.get("width")),
        height=_as_int(raw.get("height")),
        duration_sec=duration_sec,
        backup_urls=backup_list,
    )


def decide_kind(post_type: str, media_roles: list[str], word_count: int) -> str:
    """按技术方案 §5.1 判定内容类型。

    顺序有意义，不要重排：
    有视频就是 video（视频里的截图不该被算成 image_text）；
    其次看「多图 + 少字」（小红书/微博的典型形态）；
    再次看长正文；最后才是单图与兜底。
    """
    if "video" in media_roles:
        return KIND_VIDEO
    images = [r for r in media_roles if r == "image"]
    if len(images) >= IMAGE_TEXT_MIN_IMAGES and word_count < ARTICLE_WORD_COUNT:
        return KIND_IMAGE_TEXT
    if word_count >= ARTICLE_WORD_COUNT:
        return KIND_ARTICLE
    if images:
        return KIND_IMAGE
    # 上游给了已知类型但没媒体：信任它，别把一条视频说成 link
    return _POST_TYPE_TO_KIND.get(post_type, KIND_LINK)


def normalize_payload(payload: dict[str, Any]) -> ExtractResult:
    """把 ParseHub 的 `to_dict()` 输出收成 `ExtractResult`。

    不抛异常：畸形输入一律降级。抓取失败要能让记录进库并标「处理失败」，
    而不是让一条脏数据打断整个 worker（技术方案 §4.10）。
    """
    post_type = _as_str(payload.get("type"))
    title = _as_str(payload.get("title")).strip()
    content = _as_str(payload.get("content"))
    raw_url = _as_str(payload.get("raw_url")).strip()
    platform = _as_str(payload.get("platform"))

    media_dicts = _as_media_dicts(payload.get("media"))
    # role 由 content type 决定，不由单个媒体决定：抖音视频就是 video，
    # 图文的每一张都是 image。这样 `attachments_role_allowed` 的 CHECK 才过得去。
    default_role = "video" if post_type == "video" else "image"
    media = [
        item
        for raw in media_dicts
        if (item := _to_media_item(raw, default_role, platform))
    ]

    kind = decide_kind(
        post_type=post_type,
        media_roles=[m.role for m in media],
        word_count=len(content),
    )

    return ExtractResult(
        kind=kind,
        title=title,
        content_text=content,
        raw_url=raw_url,
        platform=platform,
        media=media,
    )

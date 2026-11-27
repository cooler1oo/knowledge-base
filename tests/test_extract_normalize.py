"""抓取层的纯逻辑单测：把 ParseHub 的三种 media 形状归一成统一结构。

为什么不连网：这些用例全部用 `tests/fixtures/` 里的真实响应快照（已脱敏），
毫秒级跑完。真抓取由 `tests/test_extract_live.py` 标 `external` 单独跑。

夹具来源（2026-11-27 实测，两条都是真实抖音链接）：
- `douyin_video.json`：`media` 是 **dict**
- `douyin_image.json`：`media` 是 **list**

这两条形状差异是本模块存在的理由：不归一，视频能跑、图文必崩。
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict[str, object]:
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


# ── 归一化：media 三形态 ──────────────────────────────────────


def test_视频的_media_是单个_dict_也要归一成列表():
    """ParseHub 对视频返回 dict、对图文返回 list。归一后调用方只处理列表。"""
    from kb.extract.normalize import normalize_payload

    result = normalize_payload(load("douyin_video"))

    assert result.kind == "video"
    assert len(result.media) == 1
    assert result.media[0].role == "video"


def test_图文的_media_是列表_每一张都要保留():
    from kb.extract.normalize import normalize_payload

    result = normalize_payload(load("douyin_image"))

    assert result.kind == "image"
    assert len(result.media) == 1
    assert result.media[0].role == "image"


def test_多图图文_每张图都变成独立媒体项():
    """实测那条图文只有 1 张图，多图行为按源码推断。这里用构造数据把契约钉住：
    ImageParseResult 的 media 是 Sequence，有几张就该出几项。"""
    from kb.extract.normalize import normalize_payload

    payload = {
        "platform": "douyin",
        "type": "image",
        "title": "三张图",
        "content": "",
        "raw_url": "https://example.invalid/note/1",
        "media": [
            {
                "url": "https://example.invalid/a.jpg",
                "ext": "jpg",
                "width": 100,
                "height": 200,
            },
            {
                "url": "https://example.invalid/b.jpg",
                "ext": "jpg",
                "width": 100,
                "height": 200,
            },
            {
                "url": "https://example.invalid/c.jpg",
                "ext": "jpg",
                "width": 100,
                "height": 200,
            },
        ],
    }
    result = normalize_payload(payload)

    assert len(result.media) == 3
    assert [m.url for m in result.media] == [
        "https://example.invalid/a.jpg",
        "https://example.invalid/b.jpg",
        "https://example.invalid/c.jpg",
    ]


def test_media_为_None_时不崩():
    """上游可能对某些内容不给媒体。归一后应是空列表，不是 None。"""
    from kb.extract.normalize import normalize_payload

    payload = {
        "platform": "douyin",
        "type": "video",
        "title": "没有媒体",
        "content": "",
        "raw_url": "https://example.invalid/x",
        "media": None,
    }
    result = normalize_payload(payload)

    assert result.media == []


# ── duration 换算（毫秒 → 秒）──────────────────────────────────


def test_时长从毫秒换算成秒():
    """实测 duration=445071。这是毫秒：按秒解释是 123.6 小时，不可能。

    写库用 `props.duration_sec`，不换算会把 7 分钟的视频记成 5 天。
    """
    from kb.extract.normalize import normalize_payload

    result = normalize_payload(load("douyin_video"))

    assert result.media[0].duration_sec == 445


def test_图文没有时长字段_给_None_而不是_0():
    """0 表示「时长 0 秒」，None 表示「这个概念不适用」。两者不能混。"""
    from kb.extract.normalize import normalize_payload

    result = normalize_payload(load("douyin_image"))

    assert result.media[0].duration_sec is None


def test_时长为_0_或缺失都不崩():
    from kb.extract.normalize import normalize_payload

    for value in (0, None):
        payload = {
            "platform": "douyin",
            "type": "video",
            "title": "t",
            "content": "",
            "raw_url": "https://example.invalid/x",
            "media": {
                "url": "https://example.invalid/v.mp4",
                "ext": "mp4",
                "duration": value,
            },
        }
        result = normalize_payload(payload)
        assert result.media[0].duration_sec == (0 if value == 0 else None)


# ── 字段缺失与畸形输入 ─────────────────────────────────────────


def test_图文有空的_backup_urls_视频有三条_都要能取到():
    """实测差异：图文的 backup_urls 存在但为空，视频有 3 条。
    v9.10 曾推断「图文没有该字段」，实测是错的（有字段、值为空）。"""
    from kb.extract.normalize import normalize_payload

    video = normalize_payload(load("douyin_video"))
    image = normalize_payload(load("douyin_image"))

    assert len(video.media[0].backup_urls) == 3
    assert image.media[0].backup_urls == []


def test_标题里的首尾空白被去掉():
    from kb.extract.normalize import normalize_payload

    payload = {
        "platform": "douyin",
        "type": "video",
        "title": "  前后有空格  ",
        "content": "",
        "raw_url": "https://example.invalid/x",
        "media": None,
    }
    assert normalize_payload(payload).title == "前后有空格"


def test_未知的_type_降级为_link_而不是抛异常():
    """抓取层新增平台时会出现没见过的 type。降级比崩掉好：记录不能丢（§4.10）。"""
    from kb.extract.normalize import normalize_payload

    payload = {
        "platform": "unknown_platform",
        "type": "weird_kind",
        "title": "t",
        "content": "",
        "raw_url": "https://example.invalid/x",
        "media": None,
    }
    assert normalize_payload(payload).kind == "link"


def test_空标题与空内容不报错():
    from kb.extract.normalize import normalize_payload

    payload = {
        "platform": "douyin",
        "type": "video",
        "title": "",
        "content": "",
        "raw_url": "https://example.invalid/x",
        "media": None,
    }
    result = normalize_payload(payload)
    assert result.title == ""
    assert result.content_text == ""


# ── 类型判定（技术方案 §5.1）────────────────────────────────────


def test_类型判定_有视频媒体就是_video():
    from kb.extract.normalize import decide_kind

    assert (
        decide_kind(post_type="video", media_roles=["video"], word_count=0) == "video"
    )


def test_类型判定_图文多图且文字少是_image_text():
    from kb.extract.normalize import decide_kind

    roles = ["image", "image", "image"]
    assert (
        decide_kind(post_type="image", media_roles=roles, word_count=10) == "image_text"
    )


def test_类型判定_长正文且无视频是_article():
    from kb.extract.normalize import decide_kind

    assert (
        decide_kind(post_type="image", media_roles=["image"], word_count=500)
        == "article"
    )


def test_类型判定_单图是_image():
    from kb.extract.normalize import decide_kind

    assert (
        decide_kind(post_type="image", media_roles=["image"], word_count=5) == "image"
    )


def test_类型判定_什么都没有是_link():
    from kb.extract.normalize import decide_kind

    assert decide_kind(post_type="", media_roles=[], word_count=0) == "link"

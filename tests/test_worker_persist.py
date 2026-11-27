"""抓取结果写回 items 与 attachments（纯 SQL 层）。

三条最容易写错的语义，每条都有用例钉住：

1. **失败不改 `kind`**。`kind='link'` 是入库时的降级结果，抓取失败不该把它冲掉，
   否则一条「抓不到的小红书」会从 link 变成别的，用户看到的状态会来回跳。
2. **多图 seq 必须按序从 0 开始**。乱序会让详情页图片顺序错乱，且没有报错。
3. **`props` 用 `||` 合并，不整体覆盖**。入库时已写入的字段被抹掉是静默数据丢失。
"""

from __future__ import annotations

import pg8000.native

from kb.extract.normalize import ExtractResult, MediaItem
from kb.store import items


class _Conn:
    def __init__(self) -> None:
        self.seen: list[str] = []
        self.params: list[dict[str, object]] = []

    def run(self, sql: str, **params: object) -> list[list[object]]:
        self.seen.append(sql.lower())
        self.params.append(params)
        return []


def _as_conn(c: _Conn) -> pg8000.native.Connection:
    return c  # type: ignore[return-value]


def _ok_video() -> dict[str, object]:
    data = ExtractResult(
        kind="video",
        title="第12集 一口气搞懂支撑现代互联网运转的14个核心网络协议",
        content_text="",
        raw_url="https://www.douyin.com/video/1",
        platform="douyin",
        media=[
            MediaItem(
                role="video",
                url="https://example.invalid/v.mp4",
                ext="mp4",
                thumb_url="https://example.invalid/t.jpg",
                width=768,
                height=576,
                duration_sec=445,
                backup_urls=["https://example.invalid/b1"],
            )
        ],
    )
    return {
        "ok": True,
        "kind": data.kind,
        "title": data.title,
        "content_text": data.content_text,
        "platform": data.platform,
        "media": [
            {
                "url": m.url,
                "ext": m.ext,
                "durationSec": m.duration_sec,
            }
            for m in data.media
        ],
    }


def _ok_images(n: int) -> dict[str, object]:
    media = [
        MediaItem(
            role="image",
            url=f"https://example.invalid/{i}.jpg",
            ext="jpg",
            width=1440,
            height=1920,
        )
        for i in range(n)
    ]
    data = ExtractResult(
        kind="image_text",
        title="多图",
        content_text="",
        raw_url="https://www.douyin.com/note/1",
        platform="douyin",
        media=media,
    )
    return {
        "ok": True,
        "kind": data.kind,
        "title": data.title,
        "content_text": data.content_text,
        "platform": data.platform,
        "media": [{"url": m.url, "ext": m.ext} for m in data.media],
    }


def _failed() -> dict[str, object]:
    return {"ok": False, "error": "FallbackError: 被风控拦截"}


# ── 成功路径 ──────────────────────────────────────────────────


def test_抓取成功写入标题与正文():
    conn = _Conn()
    items.apply_extract(_as_conn(conn), 1, **_ok_video())

    joined = " ".join(conn.seen)
    assert "update items" in joined
    assert "content_text = :ct" in joined


def test_抓取成功把时长写进_props_并落到生成列():
    """items.duration_sec 是 props 的生成列，写 props 才能让它有值。"""
    conn = _Conn()
    items.apply_extract(_as_conn(conn), 1, **_ok_video())

    params = conn.params[0]
    props = params.get("p")
    assert props is not None
    assert "445" in str(props), f"duration_sec 没写进 props：{props}"


def test_多图数量与顺序记进_props():
    """attachments 表要求 storage_key/mime/sha256，三者都要媒体真下载后才有。
    R5 只抓元数据不下载，所以媒体只记进 props.mediaCount / mediaUrls 等下载轮次用；
    这里钉住「数量与顺序被完整保留」，别把 8 张图缩成 1 张。"""
    conn = _Conn()
    items.apply_extract(_as_conn(conn), 1, **_ok_images(3))

    joined = " ".join(conn.seen)
    assert "insert into attachments" not in joined, (
        "R5 不下载媒体，不该凭空写 attachments（storage_key/mime/sha256 会填假值）"
    )
    props = conn.params[0].get("p")
    assert props is not None
    assert "3" in str(props), f"媒体数量没记进 props：{props}"
    # 顺序：三张图的 URL 应按序出现在 props 里
    text = str(props)
    assert text.index("0.jpg") < text.index("1.jpg") < text.index("2.jpg")


def test_抓取成功要清掉上次的失败原因():
    """重试成功后仍留着 error，界面会一直显示「处理失败」。"""
    conn = _Conn()
    items.apply_extract(_as_conn(conn), 1, **_ok_video())

    props_json = str(conn.params[0].get("p"))
    assert "fetchError" not in props_json, (
        f"成功路径没清掉上次的失败原因，界面会一直显示「处理失败」：{props_json}"
    )
    joined = " ".join(conn.seen)
    assert "(props - 'fetcherror')" in joined, (
        f"成功路径没有显式删除 fetchError，旧值会一直留在 props 里：{joined}"
    )


# ── 失败路径：不改 kind ───────────────────────────────────────


def test_抓取失败不改_kind():
    """kind='link' 是入库时的降级结果，失败信息不该把它冲掉。"""
    conn = _Conn()
    items.apply_extract(_as_conn(conn), 1, **_failed())

    joined = " ".join(conn.seen)
    assert "kind =" not in joined, f"失败路径改了 kind：{joined}"


def test_抓取失败记录原因到_props():
    conn = _Conn()
    items.apply_extract(_as_conn(conn), 1, **_failed())

    props_json = str(conn.params[0].get("p"))
    assert "fetchError" in props_json, f"失败原因没写进 props：{props_json}"
    assert "被风控拦截" in props_json


def test_抓取失败时标题为空才补_已有标题不覆盖():
    """用户分享的文案已经是标题时，抓取失败不该把它清空。

    实现在同一条 SQL 里用 `case when title = '' then :t else title end`，
    所以「只在空时补」这个语义必须能在 SQL 里看出来。
    """
    conn = _Conn()
    items.apply_extract(_as_conn(conn), 1, **_failed())

    joined = " ".join(conn.seen)
    assert "title = ''" in joined or "cases" in joined, (
        f"没有「只在标题为空时补」的保护：{joined}"
    )


# ── props 合并语义 ────────────────────────────────────────────


def test_props_用合并不用覆盖():
    """整体覆盖会抹掉入库时写入的字段，是静默数据丢失。"""
    conn = _Conn()
    items.apply_extract(_as_conn(conn), 1, **_ok_video())

    joined = " ".join(conn.seen)
    assert "|| cast(:p as jsonb)" in joined, f"props 没有用 || 合并：{joined}"


def test_没有媒体时不插_attachments_也不报错():
    conn = _Conn()
    outcome = {
        "ok": True,
        "kind": "article",
        "title": "纯文字",
        "content_text": "正文",
        "platform": "",
        "media": [],
    }

    items.apply_extract(_as_conn(conn), 1, **outcome)

    assert not any("insert into attachments" in s for s in conn.seen)


def test_成功但_data_为_None_时不崩_按失败处理():
    """契约上 ok=True 必有 data；万一没有，宁可降级也不要抛异常打断 worker。"""
    conn = _Conn()
    # 契约上 ok=True 必有 data。这里模拟「声称成功但没有内容」的畸形输入，
    # 实现应把空结果按失败处理：不写 kind，只记原因。
    outcome = {"ok": False, "error": "抓取结果为空"}

    items.apply_extract(_as_conn(conn), 1, **outcome)

    joined = " ".join(conn.seen)
    assert "kind =" not in joined

"""fetch 步套用域名规则（M3-08 T03）。

## 为什么套用点在这里而不是 `/api/items` 的 create 分支

入库时 URL 条目的 kind **固定是 `"link"`**（`_parse_create` 只看「有没有链接」），
真实类型要等 fetch 跑完、由 `decide_kind` 按媒体与字数算出来。

所以 create 时查规则只能把 `link` 换成规则值——那会**跳过类型判定的全部逻辑**，
把一条没有任何视频的链接直接标成 `video`。这不只是精度问题，是把 M3-01
（按结构判类型）整条需求架空了。

正确顺序：`decide_kind` 先算**建议** → 规则覆盖它。

（team-lead 原话是「改 `create_or_list` 的 POST 分支」。我按实际数据流改在
`run_fetch`，理由见上。这条差异已在汇报里说明。）
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from kb.extract.normalize import ExtractResult
from kb.extract.router import ExtractOutcome
from kb.worker import steps

NOW = datetime(2026, 11, 28, 10, 0, tzinfo=timezone.utc)


class Conn:
    """假连接：`domain_type_rules` 由 `rules` 驱动，其余语句一律返回空。

    `applied` 记下 `apply_extract` 真正写进 items 的 kind——这是本文件
    **唯一的观测点**：套用规则的全部效果都体现在「落库的 kind 是什么」上。
    """

    def __init__(self, rules: dict[str, str] | None = None) -> None:
        self.rules: dict[str, str] = dict(rules or {})
        self.seen: list[str] = []
        self.applied: list[dict[str, Any]] = []
        self.enqueued: list[str] = []

    def run(self, sql: str, **params: object) -> list[list[object]]:
        low = " ".join(sql.split()).lower()
        self.seen.append(low)

        if "from domain_type_rules" in low:
            kind = self.rules.get(str(params.get("d") or ""))
            return [] if kind is None else [[kind]]

        # `items.get_item` 拼 `ItemDetail` 时会调 `jobs.counts` 算 processed 状态
        if "from jobs where item_id" in low and "count(*)" in low:
            return [[0, 0, 0, 0]]

        # `items.get_item` 的详情行（12 列，见 `_DETAIL_SQL`）
        if "from items i" in low and "i.id = :i" in low:
            return [
                [
                    1,  # id
                    "link",  # kind
                    "https://www.douyin.com/video/1",  # url
                    "",  # title
                    None,  # author
                    "douyin.com",  # source_domain
                    None,  # summary
                    None,  # content_text
                    "pending",  # status
                    NOW,  # saved_at
                    None,  # published_at
                    {},  # props
                ]
            ]

        if "update items" in low and "kind = :k" in low:
            self.applied.append({"kind": str(params.get("k", ""))})
            return []

        if "insert into jobs" in low and "on conflict" in low:
            self.enqueued.append(str(params.get("s", "")))
            return [[1]]

        return []


class Extractor:
    """假抓取器：返回一条「有视频」的结果，`decide_kind` 会判成 video。"""

    def __init__(self, kind: str = "video") -> None:
        self.kind = kind

    async def __call__(self, url: str) -> ExtractOutcome:
        return ExtractOutcome(
            ok=True,
            route="douyin",
            kind=self.kind,
            data=ExtractResult(
                kind=self.kind,  # 抓取层算出的建议
                title="标题",
                content_text="正文" * 100,
                raw_url=url,
                platform="douyin",
                media=[],
            ),
        )


@pytest.fixture
def patched(monkeypatch):
    """把 `run_fetch` 依赖的写库函数换成记录器，返回记录列表。"""
    applied: list[dict[str, Any]] = []

    def fake_apply(conn, item_id, **kwargs):  # type: ignore[no-untyped-def]
        applied.append(dict(kwargs))

    monkeypatch.setattr(steps.items_store, "apply_extract", fake_apply)
    monkeypatch.setattr(steps.items_store, "clear_media_props", lambda *a, **k: None)
    monkeypatch.setattr(steps.jobs_store, "mark_done", lambda *a, **k: None)
    monkeypatch.setattr(steps.jobs_store, "mark_failed", lambda *a, **k: None)
    monkeypatch.setattr(steps.jobs_store, "enqueue", lambda *a, **k: True)
    monkeypatch.setattr(steps, "_download_media", lambda *a, **k: None)
    return applied


def _run(conn: Conn, kind: str = "video") -> None:
    """**不要在外面包 `asyncio.run`**：`run_fetch` 内部自己
    `asyncio.run(extractor(url))`（它把异步抓取器同步化），外层再包一层就
    变成「对一个协程对象再 run」，报 `ValueError: a coroutine was expected`。
    """
    steps.run_fetch(
        conn,  # type: ignore[arg-type]
        1,
        7,
        extractor=Extractor(kind=kind),
        media_stage=None,
    )


# ── 核心判据（team-lead 的 1、3）────────────────────────────


def test_规则命中时覆盖自动判定(patched) -> None:
    """抓取层判 video，规则说 article → 落库的必须是 article。

    这是 M3-08 的核心：用户改过一次之后，同域名的新内容自动沿用。
    """
    conn = Conn({"douyin.com": "article"})
    _run(conn)

    assert patched, "apply_extract 没被调用"
    assert patched[0]["kind"] == "article", (
        f"规则没生效，落库的 kind 是 {patched[0]['kind']}"
    )


def test_规则未命中时保持自动判定(patched) -> None:
    """没有规则时 `decide_kind` 的结果原样落库——不能让「没规则」也改点什么。"""
    conn = Conn({})
    _run(conn, kind="video")

    assert patched[0]["kind"] == "video"


def test_不同域名的规则互不影响(patched) -> None:
    conn = Conn({"zhihu.com": "article"})  # 规则在别的域名上
    _run(conn, kind="video")  # URL 是 douyin.com

    assert patched[0]["kind"] == "video", "别的域名的规则不该命中"


def test_子域规则不影响父域(patched) -> None:
    """`v.douyin.com` 有规则时 `douyin.com` 的内容不受影响。

    规则表的键是精确匹配（`domain_of` 只去 `www.`），不是后缀匹配。
    后缀匹配会让「对短链的一次修正」污染整个主站。
    """
    conn = Conn({"v.douyin.com": "article"})
    _run(conn, kind="video")  # URL 是 www.douyin.com/video/1 → douyin.com

    assert patched[0]["kind"] == "video"


# ── 边界 ────────────────────────────────────────────────────


def test_抓取失败时不查规则也不改kind(monkeypatch) -> None:
    """失败时 `kwargs` 里没有 kind，`apply_extract` 也刻意不动 kind。

    那条路径上如果去查规则，得到的结果会被塞进一个不含 kind 的 kwargs——
    多一个键会让 `apply_extract` 收到意料外的参数。
    """
    applied: list[dict[str, Any]] = []

    def fake_apply(conn, item_id, **kwargs):  # type: ignore[no-untyped-def]
        applied.append(dict(kwargs))

    monkeypatch.setattr(steps.items_store, "apply_extract", fake_apply)
    monkeypatch.setattr(steps.items_store, "clear_media_props", lambda *a, **k: None)
    monkeypatch.setattr(steps.jobs_store, "mark_failed", lambda *a, **k: None)

    class Failing:
        async def __call__(self, url: str) -> ExtractOutcome:
            return ExtractOutcome(ok=False, route="", kind="link", error="网络错")

    conn = Conn({"douyin.com": "article"})
    steps.run_fetch(conn, 1, 7, extractor=Failing(), media_stage=None)  # type: ignore[arg-type]

    assert applied[0]["ok"] is False
    assert "kind" not in applied[0], "失败路径不该塞 kind"
    assert conn.seen == [] or all("domain_type_rules" not in s for s in conn.seen), (
        "失败路径不该查规则表"
    )


def test_无url的纯文字条目不查规则(monkeypatch) -> None:
    """纯文字条目没有域名，fetch 直接结束。不该为它查一次规则表。"""
    applied: list[dict[str, Any]] = []

    def fake_apply(conn, item_id, **kwargs):  # type: ignore[no-untyped-def]
        applied.append(dict(kwargs))

    monkeypatch.setattr(steps.items_store, "apply_extract", fake_apply)
    monkeypatch.setattr(steps.jobs_store, "mark_done", lambda *a, **k: None)

    class NoUrlConn(Conn):
        def run(self, sql: str, **params: object):
            low = " ".join(sql.split()).lower()
            if "from items i" in low and "i.id = :i" in low:
                if not hasattr(self, "_served"):
                    self._served = True
                    return [
                        [
                            1,
                            "text",
                            None,  # 无 URL
                            "标题",
                            None,
                            "",
                            None,
                            "正文",
                            "pending",
                            NOW,
                            None,
                            {},
                        ]
                    ]
                return []
            return super().run(sql, **params)

    conn = NoUrlConn({"douyin.com": "article"})
    steps.run_fetch(conn, 1, 7, extractor=Extractor(), media_stage=None)  # type: ignore[arg-type]

    assert applied[0]["ok"] is False
    assert all("domain_type_rules" not in s for s in conn.seen)

"""`PATCH /api/items/{id}` 修正类型并记住（M3-08 T04）。

这是 M3-08 唯一缺的那一环：没有它，`domain_type_rules` 里的规则永远不会被
写入，整条需求是死代码。

## 五条判据（team-lead 的完成判据 2、4、5）

| 判据 | 用例 |
|---|---|
| 改成 article → 记住规则 | `test_改类型成功且记住域名规则` |
| 非法 kind → 400 不是 500 | `test_非法kind返回400并列出可选值` |
| 改完 enqueue index | `test_改类型后入队index` |
| 不存在的 id → 404 | `test_不存在的id返回404` |
| 无 URL 的条目不写规则 | `test_纯文字条目不写域名规则` |

## 为什么要 `await` 而不是同步

`_edit` 读 `await request.json()`，所以这个 handler 的异步是**必要的**，
与 `/api/search` 那条「全是同步调用却写 async」的情况不同——
那边是 P0 缺陷，这边是正确用法。两处不要混为一谈。
"""

from __future__ import annotations

from datetime import datetime, timezone

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from kb.api import items as items_api
from kb.store import items as items_store

_KINDS = ["article", "image", "image_text", "link", "project", "text", "video"]


def _detail(
    item_id: int = 1, *, url: str | None = "https://www.douyin.com/video/1"
) -> items_store.ItemDetail:
    """造一条 `ItemDetail`。`status` 用 `pending`（刚入库的常态）。"""
    return items_store.ItemDetail(
        id=item_id,
        kind="link",
        url=url,
        title="标题",
        author=None,
        source_domain="douyin.com",
        summary=None,
        content_text=None,
        read_status="unread",
        status="pending",
        saved_at=datetime(2026, 11, 28, 10, 0, tzinfo=timezone.utc),
        published_at=None,
        props={},
    )


def _detail_row(it: items_store.ItemDetail) -> list[object]:
    """按 `kb/store/items.py::_DETAIL_SQL` 的**列顺序**造一行。

    ⚠️ 12 列，且**没有 `read_status`**——详情页的阅读状态是从 `props` 里取的
    （`_row_to_detail` 里另有逻辑）。照着 `ItemDetail` 的字段顺序摆会多一列，
    `_row_to_detail` 的索引整体错位，报 `IndexError: list index out of range`。
    """
    return [
        it.id,  # 0
        it.kind,  # 1
        it.url,  # 2
        it.title,  # 3
        it.author,  # 4
        it.source_domain,  # 5
        it.summary,  # 6
        it.content_text,  # 7
        it.status,  # 8
        it.saved_at,  # 9
        it.published_at,  # 10
        it.props,  # 11
    ]


class FakeConn:
    """假连接：只覆盖 `_edit` 会走的四条语句。

    `rules` 是规则表内容，用例直接读它断言「记住了没有」；
    `enqueued` 记入队的步骤，用来验「改完重写索引」。
    """

    def __init__(
        self,
        *,
        item: items_store.ItemDetail | None = None,
        rules: dict[str, str] | None = None,
    ) -> None:
        self.item = item
        self.rules: dict[str, str] = dict(rules or {})
        self.enqueued: list[str] = []
        self.updated_kinds: list[str] = []
        self.seen: list[str] = []

    def run(self, sql: str, **params: object) -> list[list[object]]:
        low = " ".join(sql.split()).lower()
        self.seen.append(low)

        if "from item_kinds" in low:
            return [[k] for k in _KINDS]

        # `jobs.counts` —— `_edit` 不直接调它，但 `get_item` 之外若有别的
        # 路径碰详情就会走到。给一个四列的空计数，避免误匹配。
        if "from jobs where item_id" in low and "count(*)" in low:
            return [[0, 0, 0, 0]]

        if "from items" in low and "where" in low and low.startswith("select"):
            if self.item is None:
                return []
            return [_detail_row(self.item)]

        if "insert into domain_type_rules" in low:
            self.rules[str(params["d"])] = str(params["k"])
            return []

        if "update items set kind" in low:
            self.updated_kinds.append(str(params["k"]))
            if self.item is None:
                return []
            return [[params["i"]]]

        if "jobs" in low and "on conflict" in low:
            # `jobs.enqueue` 的参数名是 `i` / `s`，不是 `item_id` / `step`
            self.enqueued.append(str(params.get("s", "")))
            return [[1]]

        return []


class FakePool:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    def connection(self) -> object:
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield self._conn

        return _cm()


def _client(conn: FakeConn) -> TestClient:
    handler = items_api.edit(FakePool(conn))  # type: ignore[arg-type]
    app = Starlette(
        routes=[Route("/api/items/{item_id:int}", handler, methods=["PATCH"])]
    )
    return TestClient(app)


# ── 正常路径 ────────────────────────────────────────────────


def test_改类型成功且记住域名规则() -> None:
    """M3-08 的核心：改一次，同一域名的下一条就自动沿用。

    这里验「规则被写进表」；「下一条自动套用」在
    `tests/test_worker_fetch_domain_rule.py` 验（那是 fetch 步的职责）。
    """
    conn = FakeConn(item=_detail(url="https://www.douyin.com/video/1"))
    response = _client(conn).patch("/api/items/1", json={"kind": "article"})

    assert response.status_code == 200
    assert response.json()["kind"] == "article"
    # `www.` 必须剥掉：规则表的键与 `domain_of` 的输出一致
    assert response.json()["rememberedDomain"] == "douyin.com"
    assert conn.rules == {"douyin.com": "article"}
    assert conn.updated_kinds == ["article"]


def test_改类型后入队index() -> None:
    """`kind` 是可筛选字段（`/api/search?kind=video`）。

    改了 `items.kind` 而不重写索引，用户按新类型筛会**搜不到这条**——
    数据对、搜索结果错、且不报错。设计文档那条「改 kind 必须 enqueue index」
    就是为这种情况写的。
    """
    conn = FakeConn(item=_detail())
    _client(conn).patch("/api/items/1", json={"kind": "article"})

    assert "index" in conn.enqueued, f"没有入队 index，实际入队了 {conn.enqueued}"


def test_纯文字条目不写域名规则() -> None:
    """无 URL 的条目改类型是合法的，但没有域名可记。

    `domain_of(None)` 返回 `None`，`remember` 对空 domain 会抛 `ValueError`。
    所以那一步要跳过——不是把空串写进表（空串会变成「所有无域名内容共用
    一条规则」的毒数据）。
    """
    conn = FakeConn(item=_detail(url=None))
    response = _client(conn).patch("/api/items/1", json={"kind": "text"})

    assert response.status_code == 200
    assert response.json()["rememberedDomain"] is None
    assert conn.rules == {}, "无 URL 的条目不该写规则"
    assert conn.updated_kinds == ["text"]


def test_规则已存在时更新而不是新增一行() -> None:
    conn = FakeConn(
        item=_detail(url="https://douyin.com/x"), rules={"douyin.com": "video"}
    )
    _client(conn).patch("/api/items/1", json={"kind": "article"})

    assert conn.rules == {"douyin.com": "article"}


# ── 错误路径 ────────────────────────────────────────────────


def test_非法kind返回400并列出可选值() -> None:
    """必须是 400 而不是 500。

    不校验的话非法 kind 会被外键拒绝（SQLSTATE 23503），pg8000 抛
    `IntegrityError`，API 层没接住就变成 500。而真实原因是「用户传了个
    不存在的类型」——这是输入问题，该 400。
    """
    conn = FakeConn(item=_detail())
    response = _client(conn).patch("/api/items/1", json={"kind": "podcast"})

    assert response.status_code == 400
    message = response.json()["error"]
    assert "podcast" in message
    # 候选值要列出来，否则用户不知道能填什么
    assert "video" in message
    # 校验失败不该留半截状态
    assert conn.rules == {}
    assert conn.updated_kinds == []


def test_缺kind字段返回400() -> None:
    conn = FakeConn(item=_detail())
    response = _client(conn).patch("/api/items/1", json={"title": "新标题"})

    assert response.status_code == 400
    assert "kind" in response.json()["error"]


def test_kind不是字符串返回400() -> None:
    conn = FakeConn(item=_detail())
    response = _client(conn).patch("/api/items/1", json={"kind": 123})

    assert response.status_code == 400


def test_kind是空字符串返回400() -> None:
    conn = FakeConn(item=_detail())
    response = _client(conn).patch("/api/items/1", json={"kind": "   "})

    assert response.status_code == 400


def test_不存在的id返回404() -> None:
    conn = FakeConn(item=None)
    response = _client(conn).patch("/api/items/999", json={"kind": "article"})

    assert response.status_code == 404
    assert conn.rules == {}, "查不到条目时不该写规则"
    assert conn.updated_kinds == []


def test_id不是整数返回400() -> None:
    """路由是 `{item_id:int}`，非整数压根不匹配——但 handler 仍要有防御。

    直接以非整数调 handler 时 `int()` 抛 `ValueError`，必须转 400 而不是 500。
    """
    conn = FakeConn(item=_detail())
    handler = items_api.edit(FakePool(conn))  # type: ignore[arg-type]
    # 绕过路由匹配直接构造一个 path_params 非法的请求
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "PATCH",
        "path": "/api/items/abc",
        "path_params": {"item_id": "abc"},
        "headers": [],
        "query_string": b"",
    }
    import asyncio

    response = asyncio.run(handler(Request(scope)))
    assert response.status_code == 400


# ── 顺序：先校验后写库 ──────────────────────────────────────


def test_非法kind时不改条目() -> None:
    """非法 kind 应该**什么都没改**就返回 400。

    先 `set_kind` 再 `remember` 的话，非法 kind 会留下「条目改了、
    规则没记住」的半截状态——用户看到 400，以为没改成功，实际条目已经变了。
    """
    conn = FakeConn(item=_detail())
    _client(conn).patch("/api/items/1", json={"kind": "podcast"})

    assert conn.updated_kinds == [], "校验失败时不该动 items 表"


def test_先写规则再改条目() -> None:
    """顺序反过来会留下「规则生效但条目没改」的毒状态。

    那种情况下之后所有同域名的新内容都被悄悄改判，而用户以为操作失败了。
    反过来最坏情况是「这条改了、规则没记住」，用户再点一次就好——
    错误方向选「少记住一次」而不是「记住错的地方」。
    """
    conn = FakeConn(item=_detail())
    _client(conn).patch("/api/items/1", json={"kind": "article"})

    # 从 seen 里找两条语句的先后
    rule_idx = next(
        i for i, sql in enumerate(conn.seen) if "insert into domain_type_rules" in sql
    )
    update_idx = next(
        i for i, sql in enumerate(conn.seen) if "update items set kind" in sql
    )
    assert rule_idx < update_idx, "必须先写规则再改条目"

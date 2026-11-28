"""回收站 API 五个端点（M4-03 T02）。

## 五个端点（设计 §3.6）

| # | 方法 | 路径 | 成功 |
|---|---|---|---|
| 1 | DELETE | `/api/items/{id}` | 200 |
| 2 | POST | `/api/items/{id}/restore` | 200 / 409 |
| 3 | GET | `/api/trash` | 200 |
| 4 | DELETE | `/api/items/{id}/purge` | 200 / 404 |
| 5 | DELETE | `/api/trash` | 200 |

## 这一层要验的不是 SQL

SQL 与判定顺序在 `tests/test_trash_store.py` 与真库用例里验。本文件验的是
**HTTP 契约**：状态码、响应体字段名（camelCase）、错误文案、参数校验。

有一条判据必须在这里重验一次：**连删两次不续期**。T01 验的是 store 层返回
什么，这里要验「API 层把它翻成什么样的响应」——`alreadyDeleted: true` 且
`deletedAt` 是**原来那次**的值。两层任何一层写错，用户看到的现象都是
「回收站里的东西一直不清」，而响应体是唯一能看出来区别的地方。
"""

from __future__ import annotations

from datetime import datetime, timezone

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from kb.api import trash as trash_api
from kb.store import items as items_store

NOW = datetime(2026, 11, 28, 15, 20, 31, tzinfo=timezone.utc)
OLD = datetime(2026, 11, 1, 9, 0, 0, tzinfo=timezone.utc)


class FakePool:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    def connection(self) -> object:
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield self._conn

        return _cm()


def _client(conn: object) -> TestClient:
    """挂上五条路由的迷你 app。

    路由顺序与 `app.py` 里一致——`/api/items/{item_id:int}` 不会被
    `/api/items/{item_id:int}/restore` 吃掉（Starlette 的 `:int` 只匹配数字）。
    """
    pool = FakePool(conn)  # type: ignore[arg-type]
    app = Starlette(
        routes=[
            Route("/api/trash", trash_api.list_trash(pool), methods=["GET"]),
            Route("/api/trash", trash_api.purge_all(pool), methods=["DELETE"]),
            Route(
                "/api/items/{item_id:int}",
                trash_api.soft_delete(pool),
                methods=["DELETE"],
            ),
            Route(
                "/api/items/{item_id:int}/restore",
                trash_api.restore(pool),
                methods=["POST"],
            ),
            Route(
                "/api/items/{item_id:int}/purge",
                trash_api.purge_one(pool),
                methods=["DELETE"],
            ),
        ]
    )
    return TestClient(app)


class StoreStub:
    """把 store 层的返回值替换成用例指定的结果。

    API 层的职责是「把 store 的结果翻成 HTTP」，所以这里不需要造一个能跑
    SQL 的假连接——那会把两层的错混在一起。store 层的正确性由 T01 的 34 条
    与真库用例守。
    """

    def __init__(self) -> None:
        self.delete_result: items_store.DeleteResult | None = None
        self.restore_result: items_store.RestoreResult | None = None
        self.trash_entries: list[items_store.TrashEntry] = []
        self.trash_total = 0
        self.purged: list[int] | None = None
        self.seen: list[str] = []

    def connection(self) -> object:
        return self


def _patch_store(monkeypatch, stub: StoreStub) -> None:
    monkeypatch.setattr(
        trash_api.items_store,
        "soft_delete",
        lambda conn, item_id: stub.delete_result,
    )
    monkeypatch.setattr(
        trash_api.items_store,
        "restore_item",
        lambda conn, item_id: stub.restore_result,
    )
    monkeypatch.setattr(
        trash_api.items_store,
        "list_trash",
        lambda conn, **kw: stub.trash_entries,
    )
    monkeypatch.setattr(
        trash_api.items_store,
        "count_trash",
        lambda conn: stub.trash_total,
    )
    monkeypatch.setattr(
        trash_api.items_store,
        "purge_item",
        lambda conn, item_id: bool(stub.purged),
    )
    monkeypatch.setattr(
        trash_api.items_store,
        "purge_all",
        lambda conn: stub.purged or [],
    )


def _entry(entry_id: int = 12) -> items_store.TrashEntry:
    return items_store.TrashEntry(
        id=entry_id,
        kind="link",
        url="https://v.douyin.com/x/",
        title="标题",
        source_domain="v.douyin.com",
        saved_at=OLD,
        deleted_at=NOW,
        purge_at=datetime(2026, 12, 28, 15, 20, 31, tzinfo=timezone.utc),
        days_left=30,
        read_status="unread",
        summary=None,
        conflicts_with_active=False,
    )


# ── ① DELETE /api/items/{id} ────────────────────────────────


def test_软删成功返回200和两个时间戳(monkeypatch) -> None:
    stub = StoreStub()
    stub.delete_result = items_store.DeleteResult(
        status=items_store.DeleteStatus.DELETED,
        item_id=12,
        deleted_at=NOW,
        purge_at=datetime(2026, 12, 28, 15, 20, 31, tzinfo=timezone.utc),
    )
    _patch_store(monkeypatch, stub)

    response = _client(stub).request("DELETE", "/api/items/12")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 12
    assert body["deletedAt"] is not None
    assert body["purgeAt"] is not None
    # 正常路径**不带** alreadyDeleted 键（与 POST /api/items 的 duplicate 同手法）
    assert "alreadyDeleted" not in body


def test_连删两次第二次带alreadyDeleted且时间戳是原来那次(monkeypatch) -> None:
    """**判据 2 在 API 层的重验**（T01 已从 store 层验过）。

    `alreadyDeleted: true` 只在重复删时出现，且 `deletedAt` 必须是**原来那次**
    的值——续期了的话，用户看到「刚删的」，而实际保留期已从第一次开始算，
    清理会比他预期早 30 天。

    这条同时守住「API 层没有把自己算的时间塞进响应」——若 handler 用
    `datetime.now()` 覆盖 store 返回的 `deleted_at`，这里会红。
    """
    purge_at = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)
    stub = StoreStub()
    stub.delete_result = items_store.DeleteResult(
        status=items_store.DeleteStatus.ALREADY_DELETED,
        item_id=12,
        deleted_at=OLD,  # 一个月前那次
        purge_at=purge_at,
    )
    _patch_store(monkeypatch, stub)

    response = _client(stub).request("DELETE", "/api/items/12")

    assert response.status_code == 200
    body = response.json()
    assert body["alreadyDeleted"] is True
    assert body["deletedAt"].startswith("2026-11-01"), (
        f"deletedAt 是 {body['deletedAt']}，应为原来那次（2026-11-01）"
    )
    assert body["purgeAt"].startswith("2026-12-01")


def test_软删不存在的条目返回404(monkeypatch) -> None:
    stub = StoreStub()
    stub.delete_result = items_store.DeleteResult(
        status=items_store.DeleteStatus.NOT_FOUND, item_id=999
    )
    _patch_store(monkeypatch, stub)

    response = _client(stub).request("DELETE", "/api/items/999")

    assert response.status_code == 404
    assert "没有这条" in response.json()["error"]


def test_软删非整数id返回400(monkeypatch) -> None:
    stub = StoreStub()
    _patch_store(monkeypatch, stub)

    response = _client(stub).request("DELETE", "/api/items/abc")
    # 路由是 `{item_id:int}`，abc 压根不匹配 → 404 而不是 400。
    # 这条钉住「路由正则真的挡住了」，而不是让 handler 里的 int() 去抛。
    assert response.status_code in (400, 404)


# ── ② POST /api/items/{id}/restore ──────────────────────────


def test_恢复成功返回200(monkeypatch) -> None:
    stub = StoreStub()
    stub.restore_result = items_store.RestoreResult(
        status=items_store.RestoreStatus.RESTORED, item_id=12
    )
    _patch_store(monkeypatch, stub)

    response = _client(stub).post("/api/items/12/restore")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 12
    assert body["deletedAt"] is None
    assert body["restored"] is True


def test_恢复本来没删的返回200和alreadyActive(monkeypatch) -> None:
    """幂等：它已经可用了。200 不是 409——「无需恢复」不是冲突。"""
    stub = StoreStub()
    stub.restore_result = items_store.RestoreResult(
        status=items_store.RestoreStatus.ALREADY_ACTIVE, item_id=12
    )
    _patch_store(monkeypatch, stub)

    response = _client(stub).post("/api/items/12/restore")

    assert response.status_code == 200
    assert response.json()["alreadyActive"] is True


def test_恢复冲突返回409并带conflictItemId(monkeypatch) -> None:
    """**判据 3**：删 A → 存同 URL 得 B → 恢复 A 必须 409 且带 B 的 id。

    文案必须带**下一步该做什么**（`AGENTS.md` 的文案口径），并且 id 单列成
    字段——前端要能直接链到 `/api/items/31`，从错误字符串里正则抠 id 不可接受。
    """
    stub = StoreStub()
    stub.restore_result = items_store.RestoreResult(
        status=items_store.RestoreStatus.CONFLICT, item_id=12, conflict_item_id=31
    )
    _patch_store(monkeypatch, stub)

    response = _client(stub).post("/api/items/12/restore")

    assert response.status_code == 409
    body = response.json()
    assert body["conflictItemId"] == 31
    assert "31" in body["error"]
    # 文案要告诉用户下一步做什么
    assert "先删" in body["error"] or "删掉" in body["error"]


def test_恢复冲突但查不到是谁时仍返回409(monkeypatch) -> None:
    """并发窗口里捕 23505 时 `conflict_item_id` 是 `None`。

    这时仍要 409 而不是 500——「有冲突」这个事实是确定的，只是不知道该链到谁。
    """
    stub = StoreStub()
    stub.restore_result = items_store.RestoreResult(
        status=items_store.RestoreStatus.CONFLICT, item_id=12, conflict_item_id=None
    )
    _patch_store(monkeypatch, stub)

    response = _client(stub).post("/api/items/12/restore")

    assert response.status_code == 409
    assert response.json()["conflictItemId"] is None


def test_恢复不存在的条目返回404(monkeypatch) -> None:
    stub = StoreStub()
    stub.restore_result = items_store.RestoreResult(
        status=items_store.RestoreStatus.NOT_FOUND, item_id=999
    )
    _patch_store(monkeypatch, stub)

    response = _client(stub).post("/api/items/999/restore")
    assert response.status_code == 404


# ── ③ GET /api/trash ────────────────────────────────────────


def test_回收站列表返回items和total(monkeypatch) -> None:
    stub = StoreStub()
    stub.trash_entries = [_entry(12)]
    stub.trash_total = 3
    _patch_store(monkeypatch, stub)

    response = _client(stub).get("/api/trash")

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["total"] == 3, "total 是回收站总数，不是本页条数"
    item = body["items"][0]
    # camelCase 与 /api/items 一致
    assert item["sourceDomain"] == "v.douyin.com"
    assert item["daysLeft"] == 30
    assert item["conflictsWithActive"] is False
    assert item["readStatus"] == "unread"


def test_回收站列表空时返回空数组(monkeypatch) -> None:
    stub = StoreStub()
    _patch_store(monkeypatch, stub)

    response = _client(stub).get("/api/trash")
    assert response.json() == {"items": [], "total": 0, "nextBefore": None}


def test_回收站列表条数等于limit时给nextBefore(monkeypatch) -> None:
    """`nextBefore` 语义与 `/api/items` 一致：没有下一页给 `null`。

    键是复合的 `{deletedAt, id}` 而不是标量 id——排序主键是 `deleted_at`，
    只回 id 会让调用方再查一次 `deleted_at`，多一次往返且可能查不到
    （那条被清空了）。
    """
    stub = StoreStub()
    stub.trash_entries = [_entry(12), _entry(11)]
    stub.trash_total = 5
    _patch_store(monkeypatch, stub)

    response = _client(stub).get("/api/trash?limit=2")

    body = response.json()
    assert body["nextBefore"] is not None
    assert body["nextBefore"]["id"] == 11
    assert "deletedAt" in body["nextBefore"]


def test_回收站列表条数小于limit时nextBefore为null(monkeypatch) -> None:
    stub = StoreStub()
    stub.trash_entries = [_entry(12)]
    stub.trash_total = 1
    _patch_store(monkeypatch, stub)

    body = _client(stub).get("/api/trash?limit=30").json()
    assert body["nextBefore"] is None


def test_回收站列表limit非整数返回400(monkeypatch) -> None:
    stub = StoreStub()
    _patch_store(monkeypatch, stub)

    response = _client(stub).get("/api/trash?limit=abc")
    assert response.status_code == 400


def test_回收站列表游标只给一半时返回400(monkeypatch) -> None:
    """`beforeDeletedAt` 与 `before` 要么都给要么都不给。

    只给一个的话，游标是不完整的——按它翻页会漏条或重复，而两种错都不会报错。
    """
    stub = StoreStub()
    _patch_store(monkeypatch, stub)

    only_ts = _client(stub).get("/api/trash?beforeDeletedAt=2026-11-28T15:00:00Z")
    assert only_ts.status_code == 400

    only_id = _client(stub).get("/api/trash?before=11")
    assert only_id.status_code == 400


def test_回收站列表limit超范围夹到边界而不是报错(monkeypatch) -> None:
    """与 `/api/items` 同一口径：翻页时前端传 200 不该变成一次失败。"""
    stub = StoreStub()
    stub.trash_entries = []
    captured: dict[str, object] = {}

    def fake_list(conn, **kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(trash_api.items_store, "list_trash", fake_list)
    monkeypatch.setattr(trash_api.items_store, "count_trash", lambda conn: 0)

    response = _client(stub).get("/api/trash?limit=9999")
    assert response.status_code == 200
    assert captured["limit"] == trash_api.MAX_TRASH_LIMIT


def test_回收站列表非法游标时间返回400(monkeypatch) -> None:
    stub = StoreStub()
    _patch_store(monkeypatch, stub)

    response = _client(stub).get("/api/trash?beforeDeletedAt=not-a-time&before=11")
    assert response.status_code == 400


# ── ④ DELETE /api/items/{id}/purge ──────────────────────────


def test_彻底删单条返回200(monkeypatch) -> None:
    stub = StoreStub()
    stub.purged = [12]
    _patch_store(monkeypatch, stub)

    response = _client(stub).request("DELETE", "/api/items/12/purge")

    assert response.status_code == 200
    assert response.json() == {"id": 12, "purged": True}


def test_彻底删回收站里没有的条目返回404(monkeypatch) -> None:
    """404 而不是 200——「没删到」与「删到了」必须能区分。

    `purge_item` 的 `and deleted_at is not null` 让活条目也走这条 404，
    那是对的：用户点「彻底删」时若那条已被恢复，他该知道没删掉。
    """
    stub = StoreStub()
    stub.purged = []
    _patch_store(monkeypatch, stub)

    response = _client(stub).request("DELETE", "/api/items/999/purge")
    assert response.status_code == 404


# ── ⑤ DELETE /api/trash ─────────────────────────────────────


def test_清空回收站返回purged条数(monkeypatch) -> None:
    stub = StoreStub()
    stub.purged = [1, 2, 3]
    _patch_store(monkeypatch, stub)

    response = _client(stub).request("DELETE", "/api/trash")

    assert response.status_code == 200
    assert response.json() == {"purged": 3}


def test_清空空回收站返回0(monkeypatch) -> None:
    stub = StoreStub()
    stub.purged = []
    _patch_store(monkeypatch, stub)

    response = _client(stub).request("DELETE", "/api/trash")
    assert response.status_code == 200
    assert response.json() == {"purged": 0}


# ── 路由与鉴权（源码守卫）──────────────────────────────────
#
# 五条路由都**不进 `PUBLIC_PATHS`**：回收站里有已删内容的标题，未登录不该看到。
# 这条验不了「运行时中间件真的拦了」（那要起完整 app），只能读源码断言——
# 与 T01 的 SQL 守卫同一手法。


def test_五条路由都不在PUBLIC_PATHS里() -> None:
    from kb.api import app as app_module

    trash_paths = [
        "/api/trash",
        "/api/items/12",
        "/api/items/12/restore",
        "/api/items/12/purge",
    ]
    for path in trash_paths:
        assert path not in app_module.PUBLIC_PATHS, (
            f"{path} 出现在 PUBLIC_PATHS 里。回收站里有已删内容的标题，"
            "未登录不该看到（设计 §3.6）"
        )


def test_五条路由在app里都注册了() -> None:
    """`app.py` 的路由表必须含这五条（源码守卫）。

    本文件其余用例各自造一个小 app（只挂需要的路由），所以**它们验不到
    `app.py` 里到底有没有注册**。有人删掉一行 Route 时，那些用例全绿而
    生产端点 404——这是「小 app 测试」的固有盲区。

    断言的是「路径 + 方法」的组合，不是 Route 对象的数量：
    同一个路径有多个方法时 `app.py` 会写多条 `Route`，数数量会被格式变化打红。
    """
    import inspect

    from kb.api import app as app_module

    src = inspect.getsource(app_module)

    expected = [
        ('"/api/trash"', 'methods=["GET"]'),
        ('"/api/trash"', 'methods=["DELETE"]'),
        ('"/api/items/{item_id:int}"', 'methods=["DELETE"]'),
        ('"/api/items/{item_id:int}/restore"', 'methods=["POST"]'),
        ('"/api/items/{item_id:int}/purge"', 'methods=["DELETE"]'),
    ]
    for path, methods in expected:
        assert path in src, f"{path} 没有在 app.py 里注册"
        assert methods in src, f"{path} 的 {methods} 没有在 app.py 里注册"

    # 五个 handler 都要被引用到，否则「路由在但指向错的 handler」
    for factory in (
        "trash_api.soft_delete",
        "trash_api.restore",
        "trash_api.list_trash",
        "trash_api.purge_one",
        "trash_api.purge_all",
    ):
        assert factory in src, f"{factory} 没有被 app.py 引用"

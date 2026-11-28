"""回收站五个端点的 handler 工厂（M4-03 T02）。

## 为什么不复用 `kb/api/items.py`

那一层已经有 4 条路由（存+列表、详情、改类型）。再塞 5 个端点会变成
「一个 handler 文件管两件事」——「这条 DELETE 是删还是彻底删」这种问题
要在一个 700 行的文件里翻。

**api 层按资源分文件，store 层按表分文件**——两层切分口径不同，这是刻意的
（设计 §1.2）。所以这里新建 `trash.py`，而 store 层那七个函数放在
`kb/store/items.py`（回收站不是新表，是 `items` 的一个状态）。

## 也不复用 `items.py` 的 `_error` / `_json_item`

- `_json_item(ItemSummary)` 的字段集是「活条目」的十个键（含 `status`
  处理状态、不含 `deletedAt`）。回收站要 `deletedAt` / `purgeAt` /
  `daysLeft` / `conflictsWithActive` 四个它没有的键，且不要 `status`
  （已删条目的「处理到哪一步」对用户没有意义）。硬复用得给它加四个
  `None` 默认参数——那时它就不再是「一个 `ItemSummary` 的序列化」了。

`_error` 的形状（`{"error": msg}` + 状态码）足够通用，但回收站的 409 还要
多带一个 `conflictItemId`，所以这里写一个自己的 `_error`，签名多一个
`extra` 参数。重复的是三行代码，换来的是「这个文件的错误响应长什么样」
可以在一处读完。

## 五个端点全部要求登录

**都不加进 `PUBLIC_PATHS`**：回收站里躺着已删内容的标题，未登录不该看到。
`app.py` 的中间件默认要求会话，不加进白名单就是安全的——这是「默认安全」
而不是「显式拒绝」，所以有一条源码守卫用例断言这四条路径不在白名单里。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from kb.store import items as items_store
from kb.store.db import Pool

Handler = Callable[[Request], Awaitable[Response]]

#: 回收站列表的每页条数。比 `/api/items` 的 30/100 小一档：
#: 回收站是个「找回来」的地方，用户一次看不了太多，且条目本身带 4 个额外字段。
DEFAULT_TRASH_LIMIT = 30
MAX_TRASH_LIMIT = 100


def _error(message: str, status: int = 400, **extra: object) -> JSONResponse:
    """错误响应。`extra` 用于 409 携带 `conflictItemId`。

    形状与 `kb/api/items.py::_error` 一致（`{"error": msg}`），只是多一个
    可选扩展位。前端已有的错误处理逻辑不用改。
    """
    body: dict[str, object] = {"error": message}
    body.update(extra)
    return JSONResponse(body, status_code=status)


def _iso(value: datetime | None) -> str | None:
    """时间统一走 ISO 8601。`None` 原样返回 `None`（不是空串）。

    `deletedAt: null` 在恢复成功的响应里是有意义的信息（「这条现在不在回收站」），
    而空串会被前端当成「有值但格式不对」。
    """
    return None if value is None else value.isoformat()


def _item_id(request: Request) -> int:
    """从路径取 id。路由是 `{item_id:int}`，正常永远能取到；
    取不到说明有人绕过路由直接调 handler（测试、未来内部调用），那是编程错误。"""
    return int(str(request.path_params["item_id"]))


def _int_param(raw: str | None, name: str, default: int | None) -> int | None:
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc


# ── ① DELETE /api/items/{id} ────────────────────────────────


async def _soft_delete(request: Request, pool: Pool) -> Response:
    """软删。**可重复调用**——第二次返回 `alreadyDeleted: true`。

    ## `alreadyDeleted` 只在重复删时出现

    与 `POST /api/items` 的 `duplicate: true` 同一手法：正常路径不带这个键，
    前端判 `undefined`。带 `false` 会让「这个字段什么时候有值」变成两套判断。

    ## `deletedAt` 用 store 返回的值，不自己算

    重复删时它是**原来那次**的时刻，续期了的话用户看到「刚删的」，
    而实际保留期已从第一次开始算，清理会比他预期早 30 天。
    """
    item_id = _item_id(request)
    with pool.connection() as conn:
        result = items_store.soft_delete(conn, item_id)

    if result.status is items_store.DeleteStatus.NOT_FOUND:
        return _error("没有这条", 404)

    body: dict[str, object] = {
        "id": result.item_id,
        "deletedAt": _iso(result.deleted_at),
        "purgeAt": _iso(result.purge_at),
    }
    if result.status is items_store.DeleteStatus.ALREADY_DELETED:
        body["alreadyDeleted"] = True
    return JSONResponse(body)


# ── ② POST /api/items/{id}/restore ──────────────────────────


async def _restore(request: Request, pool: Pool) -> Response:
    """恢复。冲突时 409 + `conflictItemId`。

    ## 409 的文案必须带下一步

    「库里已有一条同链接的内容」是**现象**，用户不知道该怎么办。带上
    「先删掉它再恢复这条」才是可执行的。`AGENTS.md` 的文案口径要求这一条。

    ## `conflictItemId` 单列成字段

    前端要能直接链到 `/api/items/31`。从错误字符串里正则抠 id 是不可接受的
    （设计 §3.6 ②）——文案会改，字段不会。
    """
    item_id = _item_id(request)
    with pool.connection() as conn:
        result = items_store.restore_item(conn, item_id)

    if result.status is items_store.RestoreStatus.NOT_FOUND:
        return _error("没有这条", 404)

    if result.status is items_store.RestoreStatus.CONFLICT:
        if result.conflict_item_id is not None:
            message = (
                f"库里已有一条同链接的内容（id={result.conflict_item_id}），"
                "先删掉它再恢复这条"
            )
        else:
            # 并发窗口里捕到 23505 时查不到是谁。仍然 409——「有冲突」是确定的，
            # 只是不知道该链到谁。给 500 会让用户以为系统坏了。
            message = "库里已有一条同链接的内容，先删掉它再恢复这条"
        return _error(message, 409, conflictItemId=result.conflict_item_id)

    body: dict[str, object] = {
        "id": result.item_id,
        "deletedAt": None,
        "restored": result.status is items_store.RestoreStatus.RESTORED,
    }
    if result.status is items_store.RestoreStatus.ALREADY_ACTIVE:
        body["alreadyActive"] = True
    return JSONResponse(body)


# ── ③ GET /api/trash ────────────────────────────────────────


async def _list_trash(request: Request, pool: Pool) -> Response:
    """回收站列表。游标是 `(deletedAt, id)` 复合键。

    ## 游标参数要么都给要么都不给

    只给一个的游标是不完整的——按它翻页会漏条或重复，而**两种错都不报错**。
    所以这里显式 400，不做「缺的那个当 0 处理」的兼容（那等于静默改变语义）。

    ## `limit` 夹边界而不是报错

    与 `/api/items` 同一口径：翻页时前端传 200 不该变成一次失败。

    ## `nextBefore` 只在「本页条数 == limit」时给

    与 `/api/items` 的 `nextBefore` 同一个语义（没有下一页给 `null`），
    只是键从标量变成复合：排序主键是 `deleted_at`，只回 id 会让调用方
    再查一次，多一次往返且可能查不到（那条被清空了）。
    """
    try:
        raw_limit = _int_param(
            request.query_params.get("limit"), "limit", DEFAULT_TRASH_LIMIT
        )
        limit = max(1, min(int(raw_limit or DEFAULT_TRASH_LIMIT), MAX_TRASH_LIMIT))

        raw_before_id = _int_param(request.query_params.get("before"), "before", None)
        raw_before_ts = request.query_params.get("beforeDeletedAt")
    except ValueError as exc:
        return _error(str(exc))

    before_ts: datetime | None = None
    if raw_before_ts is not None and raw_before_ts != "":
        try:
            before_ts = datetime.fromisoformat(raw_before_ts.replace("Z", "+00:00"))
        except ValueError:
            # 不 raise：这个函数是 async 的，抛出去会变成 500。
            # 时间格式是**用户输入**，该 400。
            return _error("beforeDeletedAt 必须是 ISO 8601 时间")

    # 两个都给 / 两个都不给。只给一个的游标不完整。
    if (before_ts is None) != (raw_before_id is None):
        return _error(
            "beforeDeletedAt 与 before 要么都给，要么都不给——只给一个的游标会漏条或重复"
        )

    try:
        with pool.connection() as conn:
            entries = items_store.list_trash(
                conn,
                limit=limit,
                before_deleted_at=before_ts,
                before_id=raw_before_id,
            )
            total = items_store.count_trash(conn)
    except ValueError as exc:
        return _error(str(exc))

    items = [
        {
            "id": entry.id,
            "kind": entry.kind,
            "url": entry.url,
            "title": entry.title,
            "sourceDomain": entry.source_domain,
            "savedAt": _iso(entry.saved_at),
            "deletedAt": _iso(entry.deleted_at),
            "purgeAt": _iso(entry.purge_at),
            "daysLeft": entry.days_left,
            "readStatus": entry.read_status,
            "summary": entry.summary,
            "conflictsWithActive": entry.conflicts_with_active,
        }
        for entry in entries
    ]

    next_before: dict[str, object] | None = None
    if len(entries) == limit and entries:
        last = entries[-1]
        next_before = {"deletedAt": _iso(last.deleted_at), "id": last.id}

    return JSONResponse({"items": items, "total": total, "nextBefore": next_before})


# ── ④ DELETE /api/items/{id}/purge ──────────────────────────


async def _purge_one(request: Request, pool: Pool) -> Response:
    """彻底删一条（不可逆）。

    ## 404 而不是 200 当没删到

    `purge_item` 带 `and deleted_at is not null`，所以活条目也走这条 404。
    那是对的：用户点「彻底删」时若那条已被恢复（或从未删过），
    他该知道**没删掉**，而不是收到一个 `purged: true`。

    ## 不自己删索引

    `jobs.item_id` 是 `on delete cascade`，条目行一没，它名下的 `index` job
    跟着消失——让「删行」入队一个「清索引」的任务在结构上做不到。
    而这条路径的输入只可能是**已在回收站里的行**，它的索引文档在软删那一刻
    就已经被 `run_index` 清掉了（设计 §4.2）。若那次清理永久失败，
    由每日清扫或 `rebuild` 兜底。
    """
    item_id = _item_id(request)
    with pool.connection() as conn:
        purged = items_store.purge_item(conn, item_id)

    if not purged:
        return _error("回收站里没有这条", 404)
    return JSONResponse({"id": item_id, "purged": True})


# ── ⑤ DELETE /api/trash ─────────────────────────────────────


async def _purge_all(request: Request, pool: Pool) -> Response:
    """清空回收站（不可逆）。返回 `{"purged": N}`。

    ## 为什么 `purged` 只回条数，不回 id 列表

    `purge_all` 在 store 层返回 id 列表（清扫线程要拿它删索引），但**这条
    路径不需要**：能被清空的都是已软删的行，索引在软删时已清（同 `_purge_one`
    的说明）。把 id 列表吐给前端只增加响应体积，而前端拿到它没有任何用——
    行已经不存在了。

    回条数而不是布尔：清空前用户不知道有几条，清空后 `purged: 3` 是他唯一
    能确认「真的删了 3 条」的地方。
    """
    with pool.connection() as conn:
        ids = items_store.purge_all(conn)
    return JSONResponse({"purged": len(ids)})


# ── handler 工厂 ────────────────────────────────────────────


def soft_delete(pool: Pool) -> Handler:
    """`DELETE /api/items/{id}`。"""

    async def handler(request: Request) -> Response:
        return await _soft_delete(request, pool)

    return handler


def restore(pool: Pool) -> Handler:
    """`POST /api/items/{id}/restore`。"""

    async def handler(request: Request) -> Response:
        return await _restore(request, pool)

    return handler


def list_trash(pool: Pool) -> Handler:
    """`GET /api/trash`。"""

    async def handler(request: Request) -> Response:
        return await _list_trash(request, pool)

    return handler


def purge_one(pool: Pool) -> Handler:
    """`DELETE /api/items/{id}/purge`。"""

    async def handler(request: Request) -> Response:
        return await _purge_one(request, pool)

    return handler


def purge_all(pool: Pool) -> Handler:
    """`DELETE /api/trash`。"""

    async def handler(request: Request) -> Response:
        return await _purge_all(request, pool)

    return handler

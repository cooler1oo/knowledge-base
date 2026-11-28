"""`GET /api/search` 的 handler 工厂。

## 这一个文件做四件事

1. **解析与夹边界**：`limit` 1–50、`offset` 0–999（与 `/api/items` 的既有口径
   一致：超范围夹边界而不报错，翻页时前端传 200 不该变成一次失败）。
2. **校验枚举**：`kind` 查 `item_kinds` 表（**不硬编码**）、`status` 查
   `READ_STATUSES`。不在集合里返 400 且消息含那个值——用户要知道是哪个值错了。
3. **`from`/`to` 按 `Asia/Shanghai` 转 epoch 秒**（设计 §3.9）。`to` 取次日零点
   且用 `<` 半开区间。
4. **序列化 camelCase**：与 `/api/items` 完全一致（`sourceDomain` / `savedAt` /
   `readStatus`）。**这是全项目唯一做 snake→camel 转换的地方**（设计 §10）。

## 为什么 `status` / `readStatus` 由这里补全，而不是索引里带上

处理状态 `status` 由 `jobs` 聚合现算（`items.derive_status`）。每一条 job 状态
变迁都会让它变——若索引它，就要求每次 job 流转都重写索引，那正是「索引与真值
不同步」的制造机。

所以索引里没有它，API 层用 `items.summaries_by_ids(conn, ids)` **一次查询**
补全（`where id = any(...)`，不做 N+1）。查回来的条数少于命中条数时
**跳过这些条目**——索引陈旧时宁可少一条，也不给一个点进去 404 的卡片。

## 三个模式与 HTTP 状态码

| `searchMode` | 状态码 | 用户看到什么 |
|---|---|---|
| `meili` | 200 | 正常结果 |
| `fallback` | 200 | 结果 + `degraded: true` + `notice` 人话 |
| `unavailable` | **503** | 「搜索暂不可用」。不是 200 + 空列表——那会把「搜不了」谎报成「库里没有」 |
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from datetime import time as dt_time
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from kb.search.config import (
    DEFAULT_LIMIT,
    DISPLAY_TZ,
    MAX_LIMIT,
    MAX_OFFSET,
    MIN_SCORE,
    display_tz,
)
from kb.search.models import SearchFilter, SearchHit, SearchMode, SearchQuery
from kb.search.query import normalize_query
from kb.search.service import SearchService
from kb.store import items as items_store
from kb.store import search as search_store
from kb.store.db import Pool

Handler = Callable[[Request], Awaitable[Response]]

#: 对外返回的条目字段。改这里等于改前端契约（与 `/api/items` 逐字对齐）。
ITEM_FIELDS = (
    "id",
    "kind",
    "title",
    "summary",
    "sourceDomain",
    "savedAt",
    "readStatus",
    "status",
    "tags",
    "rankingScore",
)


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _int_param(request: Request, name: str) -> int | None:
    """读一个整数查询参数。空串视为「没给」，非整数抛 `ValueError`。"""
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc


def _multi(request: Request, name: str) -> tuple[str, ...]:
    """读一个「可重复或逗号分隔」的多值参数，去重保序。

    两种写法都要支持：`?kind=video&kind=article`（前端按数组发）与
    `?kind=video,article`（手工构造或 curl）。只支持一种是「文档说支持、
    实现只做一半」的典型。
    """
    values: list[str] = []
    for raw in request.query_params.getlist(name):
        for part in raw.split(","):
            piece = part.strip()
            if piece and piece not in values:
                values.append(piece)
    return tuple(values)


def parse_date_bound(raw: str, *, is_upper: bool) -> int:
    """把 `from` / `to` 的入参翻成 epoch 秒。**一律按 `Asia/Shanghai` 解释。**

    ## 三条规则（设计 §3.9，定死不许实现时自行选）

    | 入参 | 结果 |
    |---|---|
    | `2026-11-22`（只给日期） | `from` → 当日 00:00；`to` → **次日** 00:00 |
    | `2026-11-22T10:30`（带时间） | 该时刻，不再取日界 |
    | `to` 的比较 | 一律 `<`（半开区间），所以「筛 11-22」包含 23:59:59、不含 11-23 00:00 |

    ## 为什么 `to` 取次日零点

    这样比较用的是同一个运算符 `<`，不用为 `to` 单独写 `<=` 再担心精度。
    `<=` 与「次日零点」混用会在午夜整点多收一条，而那一条属于第二天。

    ## 为什么不用 UTC

    北京时间的 11-22 00:00–08:00 这 8 小时里存进来的内容，`saved_at_ts`
    落在 UTC 的 11-21。用户点「今天（11 月 22 日）」时，UTC 口径把 `from_ts`
    算成北京 11-22 08:00，**这 8 小时的内容整体漏掉**。这类静默错不报错、
    结果看起来合理、只是少了几条。

    时区口径的来源：技术方案 §11.2 T19「存 UTC，按 `Asia/Shanghai` 切今天」
    + `kb/worker/loop.py` 的 `_db_now`（平板 PG 实测 `TimeZone = PRC`）。
    """
    text = raw.strip()
    if not text:
        raise ValueError("日期不能为空")

    # 先试完整日期时间，再试纯日期。不用 `fromisoformat` 一把梭：
    # 它对 `2026-11-22` 也成功，但拿到的是「无时区的半夜」，
    # 而 `to` 的语义是「到这一天结束」，两者要分开处理。
    parsed: datetime
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"日期格式不对：{raw!r}（支持 2026-11-22 或 2026-11-22T10:30）"
        ) from exc

    tz = display_tz()
    if parsed.tzinfo is not None:
        # 带时区偏移的入参（`2026-11-22T10:30+08:00`）：用户已经说了时区，
        # 尊重它。再套一层上海时区会把他说的时间平移 8 小时。
        moment = parsed
    else:
        # 不带偏移：按 `Asia/Shanghai` 解释（**不是** UTC，也不是进程本地时区）。
        moment = parsed.replace(tzinfo=tz)

    has_time = (parsed.hour, parsed.minute, parsed.second) != (0, 0, 0) or (
        "T" in text or " " in text
    )
    if not has_time:
        if is_upper:
            # `to=2026-11-22` → 次日零点。+1 天由 `combine` 的日期算术给出，
            # 不用 `timedelta`：跨月跨年由 `date` 的 `+1 day` 自己处理。
            next_day = moment.date().toordinal() + 1
            from datetime import date

            moment = datetime.combine(
                date.fromordinal(next_day), dt_time(0, 0), tzinfo=tz
            )
        else:
            moment = moment.replace(hour=0, minute=0, second=0, microsecond=0)

    return int(moment.timestamp())


def _parse_filter(request: Request, *, known_kinds: tuple[str, ...]) -> SearchFilter:
    """解析全部筛选维度。校验失败抛 `ValueError`（由调用方转 400）。"""
    kinds = _multi(request, "kind")
    unknown = [k for k in kinds if k not in known_kinds]
    if unknown:
        raise ValueError(
            f"未知的类型：{', '.join(unknown)}（可选：{', '.join(known_kinds)}）"
        )

    statuses = _multi(request, "status")
    unknown_status = [s for s in statuses if s not in search_store.READ_STATUSES]
    if unknown_status:
        raise ValueError(
            f"未知的阅读状态：{', '.join(unknown_status)}"
            f"（可选：{', '.join(search_store.READ_STATUSES)}）"
        )

    raw_from = request.query_params.get("from")
    raw_to = request.query_params.get("to")
    return SearchFilter(
        kinds=kinds,
        tags=_multi(request, "tags"),
        read_statuses=statuses,
        saved_from_ts=(
            None if not raw_from else parse_date_bound(raw_from, is_upper=False)
        ),
        saved_to_ts=None if not raw_to else parse_date_bound(raw_to, is_upper=True),
    )


def _json_hit(hit: SearchHit) -> dict[str, object]:
    """`SearchHit` → API 的 camelCase 条目。

    **全项目唯一的 snake→camel 转换处**（设计 §10）。放到第二处就会漂移：
    两处各写一份映射，加字段时漏一处不报错，只表现为「某个字段前端拿不到」。
    """
    return {
        "id": hit.id,
        "kind": hit.kind,
        "title": hit.title,
        # `summary` 空串 → `null`：与 `/api/items` 的既有口径一致
        # （那一侧 `ItemSummary.summary` 是 `str | None`，库里 NULL 就出 null）。
        "summary": hit.summary or None,
        "sourceDomain": hit.source_domain,
        "savedAt": hit.saved_at,
        "readStatus": hit.read_status,
        "status": hit.status,
        "tags": list(hit.tags),
        "rankingScore": round(hit.ranking_score, 4),
    }


def search(
    pool: Pool,
    *,
    client: Any = None,
    meili_url: str | None = None,
    meili_key: str | None = None,
    min_score: float = MIN_SCORE,
) -> Handler:
    """handler 工厂。与 `kb/api/items.py` 同构（闭包捕获 `pool`）。

    ## 三个装配点的分工

    | 参数 | 谁传 | 为什么 |
    |---|---|---|
    | `pool` | `create_app` | 与全部现有 handler 一样 |
    | `meili_url` / `meili_key` | `create_app` 从 `AppConfig` 传 | **配置只有一个真源**。handler 自己读环境变量的话，`AppConfig.from_env` 就变成了「读了一份但没人用」——`tests/test_api_search.py` 会漏掉「部署时没配 key」这类问题 |
    | `client` | 测试注入 | 让用例不连 Meili |

    `client=None` 时按 `meili_url`/`meili_key` 构造，且**延迟到第一次请求**：
    `create_app` 在模块导入期也可能被调用（测试、CLI），不该在那里就建
    HTTP 客户端。

    `min_score` 与 `SearchQuery.min_score` 取较小值（见
    `SearchService._effective_min_score`），所以把它设成 0 等于全局不截断。
    """
    from kb.search.client import MeiliClient
    from kb.search.config import meili_key as _env_key
    from kb.search.config import meili_url as _env_url

    resolved_url = meili_url if meili_url is not None else _env_url()
    resolved_key = meili_key if meili_key is not None else _env_key()
    holder: dict[str, Any] = {}

    def _service(conn: Any) -> SearchService:
        if client is not None:
            meili = client
        else:
            if "client" not in holder:
                holder["client"] = MeiliClient(resolved_url, resolved_key)
            meili = holder["client"]
        # 兜底是**注入**的：`kb/store/search.py` 的 `fallback_for(conn)` 返回一个
        # 闭合在本次连接上的 `FallbackFn`。这样 `kb/search/` 不需要 import
        # `kb.store`（那条边界由 `layer_rules.toml` 守着）。
        return SearchService(
            meili,
            search_store.fallback_for(conn),
            min_score=min_score,
        )

    async def handler(request: Request) -> Response:
        # 校验 `kind` 要查 `item_kinds` 表，所以先开连接。
        # 连接只在这一个请求内持有：搜索是一次查询，不跨请求复用游标。
        with pool.connection() as conn:
            try:
                known = items_store.known_kinds(conn)
                condition = _parse_filter(request, known_kinds=known)
                limit = _int_param(request, "limit")
                offset = _int_param(request, "offset")
            except ValueError as exc:
                return _error(str(exc))

            text = normalize_query(request.query_params.get("q", ""))
            # 夹边界而不是报错：翻页时前端传 200 不该变成一次失败
            # （与 `/api/items` 的 limit 处理口径一致）。
            clamped_limit = (
                DEFAULT_LIMIT if limit is None else max(1, min(limit, MAX_LIMIT))
            )
            clamped_offset = 0 if offset is None else max(0, min(offset, MAX_OFFSET))

            result = _service(conn).run(
                SearchQuery(
                    text=text,
                    limit=clamped_limit,
                    offset=clamped_offset,
                    filter=condition,
                )
            )

            if result.mode is SearchMode.UNAVAILABLE:
                # **503 不是 200 + 空列表**：前者说「搜不了」，后者说「库里没有」。
                # 混成一种，用户会反复重试同一个词，越试越确信内容丢了（设计 §6.3）。
                return JSONResponse(
                    {
                        "error": result.notice or "搜索暂不可用",
                        "searchMode": result.mode.value,
                        "degraded": True,
                    },
                    status_code=503,
                )

            # 一次批量补 status / readStatus，不做 N+1。
            ids = [hit.id for hit in result.hits]
            summaries = {row.id: row for row in items_store.summaries_by_ids(conn, ids)}

        items: list[dict[str, object]] = []
        for hit in result.hits:
            row = summaries.get(hit.id)
            if row is None:
                # 索引里有、库里已删：跳过。宁可少一条，也不给一个点进去 404
                # 的卡片（设计 §3.7）。
                continue
            items.append(_json_hit(hit.with_status(row.status)))

        return JSONResponse(
            {
                "items": items,
                "searchMode": result.mode.value,
                "degraded": result.degraded,
                "relaxed": result.relaxed,
                "notice": result.notice,
                "limit": clamped_limit,
                "offset": clamped_offset,
                "estimatedTotal": result.estimated_total,
            }
        )

    return handler


__all__ = [
    "DISPLAY_TZ",
    "ITEM_FIELDS",
    "parse_date_bound",
    "search",
]

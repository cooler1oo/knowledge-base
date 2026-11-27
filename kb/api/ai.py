"""`GET /api/ai/usage` 的 handler 工厂：本月用量与预算状态。

**为什么是新端点而不是塞进 `/api/health`**（PRD §5.8）：

| 方案 | 判 | 理由 |
|---|---|---|
| 塞进 `GET /api/health` | 淘汰 | health 是**无鉴权**的端点（不在 `PUBLIC_PATHS` 之外的例外里），把「本月花了多少钱」暴露给未登录请求属于信息泄露；且它被外部探活高频调用，每次都读 `settings` 是浪费 |
| **新增 `GET /api/ai/usage`（有鉴权）** | **采用** | 语义单一、可独立演进；不在 `PUBLIC_PATHS` 里即自动走会话中间件，未登录 401（`D15`）。前端轮次直接接这个接口，不需要再改后端 |

**七个字段名定死**（`D18`）。少一个就得改前端，所以这里把它们列在同一处，
响应体形状一眼可核对。`budgetWarnedAt` **不出现在响应里**——时间戳对前端无用，
只在内部供 `D10` ② 断言。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from kb.store import db
from kb.store import usage as usage_store

Handler = Callable[[Request], Awaitable[Response]]

#: 对外暴露的七个字段（`D18`）。改这里等于改前端契约。
FIELDS = (
    "month",
    "budgetCny",
    "spentCny",
    "usedRatio",
    "budgetWarned",
    "budgetExhausted",
    "calls",
)

#: 金额与比例的小数位（`D1` / `D17`）。
#: `usedRatio` 必须到 4 位：返回 0.79 或 0.8 会让「是否已到 80%」在前端算错。
PRICE_DECIMALS = 4
RATIO_DECIMALS = 4


def _used_ratio(spent: float, budget: float) -> float:
    """`spent / budget`，**预算为 0 时返回 1.0**（`D16`）。

    返回 `1.0` 而不是 `Infinity` 或 `None`：JSON 表达不了 Infinity
    （`json.dumps` 会写 `Infinity`，那不是一个合法 JSON 值），而 `None`
    会让前端每次都要判空。语义上「预算 0 且没有任何花费」也确实等价于
    「额度已用尽」——一分钱都不能花。
    """
    if budget <= 0:
        return 1.0
    return round(spent / budget, RATIO_DECIMALS)


def build_usage(conn: Any, now: datetime) -> dict[str, object]:
    """读本月用量并算成响应体。**纯读**，不写任何东西。

    单独一个函数是为了让「七个字段的算法」能脱离 HTTP 单测：
    `D16`（预算 0 → `usedRatio=1`）与 `D17`（4 位小数）都是纯算术。
    """
    month = usage_store.month_key(now)
    state = usage_store.read(conn, month)
    budget = usage_store.budget_cny(conn)

    spent = round(state.cny, PRICE_DECIMALS)
    exhausted = spent >= budget

    return {
        "month": month,
        "budgetCny": round(budget, PRICE_DECIMALS),
        "spentCny": spent,
        "usedRatio": _used_ratio(spent, budget),
        "budgetWarned": state.budget_warned,
        "budgetExhausted": exhausted,
        "calls": state.calls,
    }


def usage(pool: db.Pool, *, now: Callable[[], datetime] | None = None) -> Handler:
    """handler 工厂。与 `kb/api/items.py` 同构（闭包捕获 `pool`）。

    `now` 可注入是为了让 `D12`（「进下月后 month 变为新月份」）能跑——
    靠真实时钟得等到下个月 1 日才验得了那条用例。
    """
    clock: Callable[[], datetime] = now or (lambda: datetime.now().astimezone())

    async def handler(_request: Request) -> Response:
        with pool.connection() as conn:
            payload = build_usage(conn, clock())
        return JSONResponse(payload)

    return handler


#: 让 `tests/test_api_ai_usage.py` 能断言「端点与记账共用同一个月份函数」。
#: 两处各写一份格式化，月底跨月时一处算上月、一处算下月，闸门读到的永远是 0。
month_key = usage_store.month_key

__all__ = ["FIELDS", "build_usage", "month_key", "usage"]

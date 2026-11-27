"""`GET /api/ai/usage` 的七个字段（D13–D18）。

**为什么是新端点而不是塞进 `/api/health`**（PRD §5.8）：

| 方案 | 判 | 理由 |
|---|---|---|
| 塞进 `GET /api/health` | 淘汰 | health 是**无鉴权**的端点，把「本月花了多少钱」暴露给未登录请求属于信息泄露；且它被外部探活高频调用，每次都读 `settings` 是浪费 |
| 新增 `GET /api/ai/usage`（有鉴权） | 采用 | 语义单一、可独立演进；前端轮次直接接这个接口，不需要再改后端 |

**七个字段名定死**（`D18`）：`month` / `budgetCny` / `spentCny` / `usedRatio` /
`budgetWarned` / `budgetExhausted` / `calls`。类型分别为
str / float / float / float / bool / bool / int。少一个就得改前端，所以
「缺任一字段即失败」是本文件的中心断言。

`budgetWarnedAt` **不出现在响应里**：时间戳对前端无用，只在内部供
`D10` ② 的断言使用。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from kb.api import ai as ai_api
from kb.store import usage as usage_store

NOW = datetime(2026, 11, 27, 14, 32, tzinfo=timezone.utc)

#: D18 定死的七个字段及类型
FIELDS = {
    "month": str,
    "budgetCny": float,
    "spentCny": float,
    "usedRatio": float,
    "budgetWarned": bool,
    "budgetExhausted": bool,
    "calls": int,
}


class _Conn:
    """假连接：只实现 usage 端点需要的那两条读。"""

    def __init__(
        self,
        *,
        settings: dict[str, object] | None = None,
        tags_rows: list[list[object]] | None = None,
    ) -> None:
        self._settings = settings or {}
        self._tags = tags_rows or []
        self.seen: list[str] = []

    def run(self, sql: str, **params: object) -> list[list[object]]:
        low = " ".join(sql.split()).lower()
        self.seen.append(low)
        if low.startswith("select value from settings"):
            key = str(params.get("k", ""))
            return [[self._settings[key]]] if key in self._settings else []
        return []


class _Pool:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    def connection(self) -> object:
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield self._conn

        return _cm()


def _client(
    pool: _Pool,
    *,
    logged_in: bool = True,
    now: datetime | None = None,
) -> TestClient:
    """只挂 usage 路由的迷你 app。

    鉴权的验证方式：`logged_in=False` 时用一个「不在 `PUBLIC_PATHS` 就 401」
    的中间件模拟 `app.py` 的 `SessionMiddleware`。用真实 `create_app` 会牵扯
    数据库、bcrypt 与登录页，把「字段对不对」这件事埋在一堆无关依赖里。

    `now` 注入给 handler 用的时钟（默认 `datetime.now().astimezone()`）：
    `D12` 要断言「进下月后 month 变成新月份」，靠真实时钟得等到 12 月 1 日
    才跑得了那条用例。
    """
    from starlette.middleware.base import (
        BaseHTTPMiddleware,
        RequestResponseEndpoint,
    )
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response

    class _Auth(BaseHTTPMiddleware):
        async def dispatch(
            self, request: Request, call_next: RequestResponseEndpoint
        ) -> Response:
            if logged_in:
                return await call_next(request)
            return JSONResponse({"error": "未登录"}, status_code=401)

    handler = ai_api.usage(pool, now=(lambda: now) if now is not None else None)
    app = Starlette(
        routes=[Route("/api/ai/usage", handler)],
        middleware=[Middleware(_Auth)],
    )
    return TestClient(app)


def _payload(extra: dict[str, object] | None = None) -> dict[str, object]:
    """构造一份 `settings` 应答。默认「本月无调用」（空字典）。

    键名带连字符（`ai_usage_2026-11`），所以不能用 `**kwargs` 关键字传参。
    """
    return dict(extra or {})


# ── D18：字段完整性 ───────────────────────────────────────────


def test_七个字段全部存在且类型正确_D18():
    """D18：`month`/`budgetCny`/`spentCny`/`usedRatio`/`budgetWarned`/
    `budgetExhausted`/`calls` 全部存在，类型为 str/float/float/float/bool/bool/int。
    """
    conn = _Conn(settings=_payload())
    response = _client(_Pool(conn)).get("/api/ai/usage")
    body = response.json()

    assert response.status_code == 200
    missing = set(FIELDS) - set(body)
    assert not missing, f"缺少字段：{sorted(missing)}"
    extra = set(body) - set(FIELDS)
    assert not extra, f"多出字段（前端按七字段对接）：{sorted(extra)}"
    for name, expected in FIELDS.items():
        assert isinstance(body[name], expected), (
            f"{name} 类型错：期望 {expected.__name__}，实际 {type(body[name]).__name__}"
        )


def test_响应里没有budgetWarnedAt_8点5节():
    """`budgetWarnedAt` 只在内部供 `D10` ② 使用，对前端无用。"""
    conn = _Conn(settings=_payload())
    body = _client(_Pool(conn)).get("/api/ai/usage").json()
    assert "budgetWarnedAt" not in body


# ── D15：鉴权 ────────────────────────────────────────────────


def test_未登录返回401_D15():
    """D15：**不得返回用量数据，也不得返回 200 空对象。**"""
    conn = _Conn(settings=_payload())
    response = _client(_Pool(conn), logged_in=False).get("/api/ai/usage")
    assert response.status_code == 401
    assert "spentCny" not in response.text


# ── D14：当月无调用 ──────────────────────────────────────────


def test_当月无调用时全零不返回null_D14():
    """D14：`spentCny: 0.0`、`usedRatio: 0.0`、`calls: 0`、
    `budgetWarned: false`、`budgetExhausted: false`；**没有 500，没有 null**。
    """
    conn = _Conn(settings=_payload())
    body = _client(_Pool(conn)).get("/api/ai/usage").json()

    assert body["spentCny"] == 0.0
    assert body["usedRatio"] == 0.0
    assert body["calls"] == 0
    assert body["budgetWarned"] is False
    assert body["budgetExhausted"] is False
    assert body["budgetCny"] == 20.0, "未设置时默认 20（D13）"
    for value in body.values():
        assert value is not None, f"字段不能是 null：{body}"


def test_month是_YYYY_MM_格式_D18():
    conn = _Conn(settings=_payload())
    body = _client(_Pool(conn)).get("/api/ai/usage").json()
    assert len(body["month"]) == 7
    assert body["month"][4] == "-"
    assert body["month"][:4].isdigit() and body["month"][5:].isdigit()


# ── D13 / D16：预算为 0 / 未设置 ────────────────────────────


def test_budget_cny未设置时返回20_D13():
    conn = _Conn(settings=_payload())
    body = _client(_Pool(conn)).get("/api/ai/usage").json()
    assert body["budgetCny"] == 20.0


def test_budget_cny为0时usedRatio为1_D16():
    """D16：`usedRatio: 1.0`（**不是 `Infinity` 也不是 null**），
    且 `budgetExhausted: true`。
    """
    conn = _Conn(settings=_payload({"ai_budget_cny": 0}))
    body = _client(_Pool(conn)).get("/api/ai/usage").json()

    assert body["budgetCny"] == 0.0
    assert body["usedRatio"] == 1.0
    assert body["budgetExhausted"] is True


# ── D17：usedRatio 精度 ─────────────────────────────────────


def test_usedRatio保留4位小数_D17():
    """D17：预算 20、花费 15.99 → `usedRatio` 在 0.7994–0.7996 之间。

    **不得返回 0.79 或 0.8 这类粗粒度值**，否则「是否已到 80%」在前端会算错。
    """
    conn = _Conn(
        settings=_payload({"ai_usage_2026-11": json.dumps({"calls": 3, "cny": 15.99})})
    )
    body = _client(_Pool(conn)).get("/api/ai/usage").json()
    assert 0.7994 <= body["usedRatio"] <= 0.7996, body["usedRatio"]


def test_usedRatio不会超过1以外的怪值_D17():
    """花超了时 `usedRatio > 1` 是事实，不夹到 1——夹了会让前端看不出超了多少。"""
    conn = _Conn(
        settings=_payload({"ai_usage_2026-11": json.dumps({"calls": 1, "cny": 25.0})})
    )
    body = _client(_Pool(conn)).get("/api/ai/usage").json()
    assert body["usedRatio"] == pytest.approx(1.25)
    assert body["budgetExhausted"] is True


# ── D8 / D9 / D11：预算标记 ─────────────────────────────────


def test_未跨80时budgetWarned为false_D8():
    conn = _Conn(
        settings=_payload({"ai_usage_2026-11": json.dumps({"calls": 3, "cny": 15.99})})
    )
    body = _client(_Pool(conn)).get("/api/ai/usage").json()
    assert body["budgetWarned"] is False


def test_跨80后budgetWarned为true_D9():
    conn = _Conn(
        settings=_payload(
            {
                "ai_usage_2026-11": json.dumps(
                    {"calls": 3, "cny": 16.01, "budgetWarned": True}
                )
            }
        )
    )
    body = _client(_Pool(conn)).get("/api/ai/usage").json()
    assert body["budgetWarned"] is True


def test_跨100后budgetExhausted为true_D11():
    conn = _Conn(
        settings=_payload(
            {
                "ai_usage_2026-11": json.dumps(
                    {"calls": 3, "cny": 20.5, "budgetWarned": True}
                )
            }
        )
    )
    body = _client(_Pool(conn)).get("/api/ai/usage").json()
    assert body["budgetWarned"] is True
    assert body["budgetExhausted"] is True


def test_calls从用量里读出_D1():
    conn = _Conn(settings=_payload({"ai_usage_2026-11": json.dumps({"calls": 2663})}))
    body = _client(_Pool(conn)).get("/api/ai/usage").json()
    assert body["calls"] == 2663


# ── D12：跨月 ───────────────────────────────────────────────


def test_进下月后month变为新月份且spentCny归零_D12():
    """D12：旧月份键里的 `budgetWarned` 保持 true 不被清除（历史可查），
    新月份键的 `budgetWarned` 不存在。
    """
    conn = _Conn(
        settings=_payload(
            {
                "ai_usage_2026-11": json.dumps(
                    {"calls": 9, "cny": 16.01, "budgetWarned": True}
                )
            }
        )
    )
    body = (
        _client(_Pool(conn), now=datetime(2026, 12, 2, 10, 0))
        .get("/api/ai/usage")
        .json()
    )

    assert body["month"] == "2026-12"
    assert body["spentCny"] == 0.0
    assert body["calls"] == 0
    assert body["budgetWarned"] is False


# ── 与 store 层口径一致 ─────────────────────────────────────


def test_端点与记账共用同一个month_key_6点4节():
    """闸门与记账必须共用 `store.usage.month_key` 一个函数。

    两处各写一份格式化，月底跨月时一处算上月、一处算下月，
    闸门读到的永远是 0——额度永不生效，且没有任何地方报错。
    """
    assert ai_api.month_key is usage_store.month_key

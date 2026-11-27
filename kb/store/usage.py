"""月度用量与预算：读写的唯一落点，也是「累加必须一条 SQL」的唯一落点。

## 为什么 `add()` 必须是一条 upsert（本轮最容易被写错的一处）

`kb/store/` 走 `pg8000.native`，这条路径**不做事务管理**——`native.py` 与
`core.py` 里 `begin` / `commit` / `rollback` / `autocommit` 一次都没出现，
`CoreConnection.__init__` 也不接受 `autocommit` 参数（实测 pg8000 1.31.6）。
每条语句执行后立即生效，**没有任何事务能罩住「读」与「写」两步**。

现有 `settings` 的写入函数又是 `value = excluded.value`（整值覆盖，不是合并）。
两者叠加：两个 worker 并发记账时，后写的会**整块覆盖**先写的 → 用量少记 →
预算闸门以为还有额度 → 花超了才发现。这是「意外账单」的直接成因。

所以：四个字段的累加、`budgetWarned` 判定、`budgetWarnedAt` 冻结/首写
**全部在同一条 SQL 里**，Python 侧绝不做「读出来 → 加一加 → 写回去」。

## 四条不能改的实现要点

| 要点 | 为什么 |
|---|---|
| `cny` 用 `round(numeric, 4)` | 浮点累加出 `16.010000000000002`，`D10` ② 的「逐字不变」会被噪声连坐 |
| `budgetWarned` 分支里**保留原 `budgetWarnedAt`** | `D10` ② 要求冻结。每次重写时间戳是最容易犯的错，写法上必须显式取旧值 |
| **首次跨线才写 `budgetWarnedAt`** | `D9` 要求跨线即写；`D10` ② 要求之后不变。两个 `when` 的**顺序**决定语义 |
| **不写任何写入计数器** | `D10a` 明确禁止。重复写同一个 `true` 在最终状态上不可观测 |
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pg8000.native

log = logging.getLogger(__name__)

#: 预算键名（PRD §8.1）。**未设置时按 20 元执行**（`D5`/`D13`），不返回 null。
BUDGET_KEY = "ai_budget_cny"
DEFAULT_BUDGET_CNY = 20.0

#: 80% 预警线（`AI-14`）。`budget × 0.8`，预算 20 时是 16.00 元。
#: 与「100% 停止」是两件事：80% 是预警（继续调用），100% 是停止（AI-05 拦请求）。
WARN_RATIO = 0.8

#: 用量键前缀。完整键是 `ai_usage_YYYY_MM`，月份取本地时区自然月。
USAGE_KEY_PREFIX = "ai_usage_"

#: 跨月重试的时刻：下月 1 日 00:05。留 5 分钟余量，避开「月初第一秒的时钟偏差」。
NEXT_MONTH_HOUR = 0
NEXT_MONTH_MINUTE = 5


@dataclass(frozen=True)
class UsageState:
    """某个月的用量快照。无记录时所有字段为零/假（`D7`/`D14`）。"""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cny: float = 0.0
    budget_warned: bool = False
    budget_warned_at: str | None = None


def month_key(now: datetime) -> str:
    """本地时区的自然月键，形如 `2026-11`。**唯一的月份格式化处。**

    闸门与记账必须共用这一个函数（设计 §6.4）：两处各写一份格式化，月底
    跨月时一处算上月、一处算下月，闸门读到的永远是 0。

    时区由传入的 `now` 携带。代码里**不读 `TZ` 环境变量**（`E6` 禁止
    `kb/ai` import os，而这里读它也不该——部署侧设 `TZ` 是部署检查的事）。
    """
    return f"{now.year:04d}-{now.month:02d}"


def next_month_start(
    now: datetime, hour: int = NEXT_MONTH_HOUR, minute: int = NEXT_MONTH_MINUTE
) -> datetime:
    """下月 1 日的 `hour:minute`（默认 00:05），时区跟随传入的 `now`。

    `D3` 断言 `run_after` 的日期是下月 1 日且时分 ≤ 00:10。

    12 月跨年必须写对年份（`D12`）：写错会让任务被推到一年后才重试，
    而这期间用户看到的只是「摘要一直没出来」。

    ## `now` 必须来自数据库，不能来自 `datetime.now()`

    这个函数按 `now` 携带的时区算「月」与「本地钟点」。若调用方传的是进程
    本地时间，而进程时区与数据库时区不同（平板实测：容器 UTC、PG PRC），
    算出的「下月 1 日 00:05」在数据库眼里是 08:05，与 `month_key` 的月份
    也会错位。生产路径统一由 `kb/worker/loop._db_now(conn)` 提供
    `select now()` 的结果，此处不做兜底——静默兜底会让口径问题重新隐身。
    """
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return now.replace(
        year=year, month=month, day=1, hour=hour, minute=minute, second=0, microsecond=0
    )


def budget_cny(conn: pg8000.native.Connection) -> float:
    """读 `settings['ai_budget_cny']`，未设置时默认 20 元。

    **`0` 是合法值**（`D16`）：「一分钱都不想花」是用户的明确表达，
    不能被「0 是假值」的默认值逻辑吃掉。只有 `None`（键不存在）才回默认。
    负数按 0 处理——负预算在语义上就是「不允许花」。
    """
    rows = conn.run("select value from settings where key = :k", k=BUDGET_KEY)
    if not rows:
        return DEFAULT_BUDGET_CNY
    raw = rows[0][0]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return DEFAULT_BUDGET_CNY
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_CNY
    return max(value, 0.0)


def warn_line(budget: float) -> float:
    """80% 预警线。预算 20 → 16.00；预算 0 → 0。"""
    return round(budget * WARN_RATIO, 4)


def can_spend(*, spent: float, estimate: float, budget: float) -> bool:
    """预算闸门：`已花 + 本次预估 >= 上限` → 不发请求。

    **必须是 `>=` 而不是 `>`**：`D3` 的场景是 `0.999 + 0.001 == 1.0`，
    恰好落在线上。用 `>` 会放行，`D3` 断言的「零次 HTTP 请求」直接失败。
    这一点是 `MIN_ESTIMATE_CNY`（0.001 下限）存在的全部理由。
    """
    return spent + estimate < budget


def read(conn: pg8000.native.Connection, month: str) -> UsageState:
    """读某月用量。无记录或值损坏时返回全零（`D7` / `D14`）。

    「值损坏时按全零」的方向是「多花一点」，不是「worker 卡死」：
    库里的值被人手改坏属于异常状态，让它停下整条流水线的代价远大于多花几分钱。
    """
    rows = conn.run(
        "select value from settings where key = :k", k=USAGE_KEY_PREFIX + month
    )
    if not rows:
        return UsageState()

    payload: object = rows[0][0]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            log.warning("用量值不是 JSON，按全零处理 key=%s", USAGE_KEY_PREFIX + month)
            return UsageState()
    if not isinstance(payload, dict):
        return UsageState()

    warned_at = payload.get("budgetWarnedAt")
    return UsageState(
        calls=_as_int(payload.get("calls")),
        prompt_tokens=_as_int(payload.get("promptTokens")),
        completion_tokens=_as_int(payload.get("completionTokens")),
        cny=_as_float(payload.get("cny")),
        budget_warned=payload.get("budgetWarned") is True,
        budget_warned_at=warned_at if isinstance(warned_at, str) else None,
    )


# ── 累加：一条 upsert（§4.7）──────────────────────────────────────
#
# 整条语句分三段，每一段都不能省：
#
# ① `insert ... values`：首次调用时建行。**这里也要判 80% 线**——
#    若本月第一次调用就跨过 16 元（`D11` 的「15.99 直接跳到 20.5」
#    在「当月一条记录都没有」的变体里），只在 update 分支判会漏掉。
# ② `on conflict ... do update set value = <合并式>`：四个字段累加 +
#    预警分支。**不能写 `value = excluded.value`**（那是整值覆盖）。
# ③ `returning value`：调用方不必再读一次。多一次往返就是多一个丢更新的窗口。
#
# ⚠️ **每个占位符的首次出现必须带显式转型**（`::int` / `::numeric`）。
#
# 这不是洁癖，是平板真库上实测到的 42P08（`could not determine data type of
# parameter $N`）。两个触发点：
#
#   1. `jsonb_build_object` 的签名是 `VARIADIC "any"`。PG 在参数解析阶段
#      （`parse_param.c` 的 `check_parameter_resolution_walker`）遇到
#      `jsonb_build_object('calls', $2)` 时，**无法从 `any` 推断 `$2` 的类型**，
#      直接报 42P08。所以 `:d_calls` / `:d_prompt` / `:d_completion` 在
#      `values(...)` 里首次出现就必须 `::int`。
#   2. `round(:x, 4)` 有两个重载（`round(numeric,int)` / `round(double,int)`），
#      PG 在 `$2` 类型未定时选不出重载。所以 `:d_cny` 首次出现要 `::numeric`。
#
# **本机的假连接永远发现不了这个 bug**：`_Recorder.run` 只记 SQL 字符串，
# 不做真正的 PG 解析。这是「假连接能过、真库必炸」的典型，只有真库跑得出来。
#
# 写法约定：首次出现带转型，**后续出现也一律带**——虽然 PG 只按首次定类型，
# 但显式写全让「哪个占位符有转型、哪个没有」不用靠数出现次序来判。

_ADD_SQL = """
insert into settings (key, value, updated_at)
values (
  :k::text,
  jsonb_build_object(
    'calls',            :d_calls::int,
    'promptTokens',     :d_prompt::int,
    'completionTokens', :d_completion::int,
    'cny',              round(:d_cny::numeric, 4)
  )
  || case
       when :d_cny::numeric >= :warn_line::numeric
         then jsonb_build_object('budgetWarned', true,
                                 'budgetWarnedAt', to_jsonb(now()::text))
       else '{}'::jsonb
     end,
  now()
)
on conflict (key) do update set
  value = jsonb_build_object(
    'calls',            coalesce((settings.value->>'calls')::int, 0) + :d_calls::int,
    'promptTokens',     coalesce((settings.value->>'promptTokens')::int, 0) + :d_prompt::int,
    'completionTokens', coalesce((settings.value->>'completionTokens')::int, 0) + :d_completion::int,
    'cny',              round(coalesce((settings.value->>'cny')::numeric, 0)
                              + :d_cny::numeric, 4)
  )
  || case
       when settings.value->>'budgetWarned' = 'true'
         then jsonb_build_object('budgetWarned', true,
                                 'budgetWarnedAt', settings.value->'budgetWarnedAt')
       when round(coalesce((settings.value->>'cny')::numeric, 0)
                  + :d_cny::numeric, 4) >= :warn_line::numeric
         then jsonb_build_object('budgetWarned', true,
                                 'budgetWarnedAt', to_jsonb(now()::text))
       else '{}'::jsonb
     end,
  updated_at = now()
returning value
"""


def add(
    conn: pg8000.native.Connection,
    *,
    month: str,
    calls: int,
    prompt_tokens: int,
    completion_tokens: int,
    delta_cny: float,
    warn_line: float,
    now: datetime | None = None,
) -> UsageState:
    """把一次调用的用量累加进 `settings['ai_usage_YYYY_MM']`。

    **一条语句完成全部四件事**：四个字段累加、`budgetWarned` 判定、
    `budgetWarnedAt` 首写或冻结、`updated_at` 更新。理由见模块 docstring。

    `warn_line` 作为参数传入而不是在 SQL 里写死 16：那是 `budget × 0.8`
    的结果，预算可配，写死会让改预算必须改 SQL。
    """
    del now  # 时间基准在 SQL 里取 now()，与 settings.updated_at 同一口径
    rows = conn.run(
        _ADD_SQL,
        k=USAGE_KEY_PREFIX + month,
        d_calls=calls,
        d_prompt=prompt_tokens,
        d_completion=completion_tokens,
        d_cny=delta_cny,
        warn_line=warn_line,
    )
    if not rows:
        # 理论上 `returning` 一定有行。走到这里说明驱动异常，按「读一遍」兜底，
        # 不抛出——记账失败不该让已经成功的摘要变成失败任务。
        return read(conn, month)
    return _state_from_value(rows[0][0])


def _state_from_value(value: object) -> UsageState:
    """把 `returning value` 拿到的 jsonb 收成 `UsageState`。"""
    payload: object = value
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return UsageState()
    if not isinstance(payload, dict):
        return UsageState()
    warned_at = payload.get("budgetWarnedAt")
    return UsageState(
        calls=_as_int(payload.get("calls")),
        prompt_tokens=_as_int(payload.get("promptTokens")),
        completion_tokens=_as_int(payload.get("completionTokens")),
        cny=_as_float(payload.get("cny")),
        budget_warned=payload.get("budgetWarned") is True,
        budget_warned_at=warned_at if isinstance(warned_at, str) else None,
    )


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    if isinstance(value, str):
        try:
            return max(int(value), 0)
        except ValueError:
            return 0
    return 0


def _as_float(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "BUDGET_KEY",
    "DEFAULT_BUDGET_CNY",
    "NEXT_MONTH_HOUR",
    "NEXT_MONTH_MINUTE",
    "USAGE_KEY_PREFIX",
    "WARN_RATIO",
    "UsageState",
    "add",
    "budget_cny",
    "can_spend",
    "month_key",
    "next_month_start",
    "read",
    "warn_line",
]

"""worker 的 AI 运行时：配置装配、预算闸门、记账、失败落地。

**这是唯一同时认识 `kb.store` 与 `kb.ai` 类型的地方**（设计 §3.1 / §8.4）：

| 放哪 | 为什么不行 |
|---|---|
| 放 `kb/ai/` | 违反分层（要读 `settings`，`layer_rules.toml` 已禁）；且 AI 层就无法脱离数据库单测 |
| 放 `kb/store/` | store 会知道模型单价与 token 估算口径——那是业务知识不是存储知识。将来换供应商要改存储层 |
| 放 `loop.py` | 循环文件同时管调度与业务口径，60 行会变成 200 行 |
| **放本文件** | worker 是唯一同时持有数据库连接与 `AiConfig` 的地方 |

四件事各自对应一条验收：

| 函数 | 对应 |
|---|---|
| `load_config` | `AI-04` / `E1`–`E3` / `E7`（三键缺任一 → 未配置，不构造配置） |
| `check_budget` | `AI-05` / `D3`–`D6` / `D16` |
| `record_usage` | `AI-06` / `AI-14` / `D1`–`D2` / `D9`–`D12` |
| `apply_failure` | `B22` / `B25` / `B26`（按类别落地） |
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kb.ai import config as ai_config
from kb.ai.outcomes import FailureCategory, SummarizeFailed, TokenUsage
from kb.store import jobs as jobs_store
from kb.store import usage as usage_store

log = logging.getLogger(__name__)

#: 数据库连接的**类型别名**。
#:
#: 为什么不 `import pg8000.native`（哪怕放在 `TYPE_CHECKING` 里）：
#: `layer_rules.toml` 的 `[[driver_deny]]` 只允许 `kb/store/` 出现驱动 import，
#: 而 `eng_checks --arch` 扫的是字面量、不区分 `TYPE_CHECKING`。这条规则的目的
#: 是「防止有人绕过 store 直接跑 SQL」，而 worker 只是把 `conn` 转手传给
#: `store.*` 的函数，从不对它调 `run()`——用 `Any` 别名表达这层关系比放开
#: 检查更准确（放开会让真正的违规也漏过）。
Conn = Any

#: 未配置时推迟多久（`AI-04`：30 分钟）。
DEFER_SECONDS_NOT_CONFIGURED = 1800

#: 连接类失败退避多久（`B22`：3 分钟）。比指数退避的 1 分钟长，避免每 1 分钟打一次 DNS。
DEFER_SECONDS_CONNECT = 180.0

#: 配置键（与 `kb/ai/config.py` 的 `REQUIRED_KEYS` 一致，这里只是为了可读性）。
_CONFIG_KEYS = (*ai_config.REQUIRED_KEYS, ai_config.PRICE_KEY)


@dataclass(frozen=True)
class BudgetDecision:
    """闸门结论。`allowed=False` 时 `retry_at` 给出下次可执行的时刻。

    `spent` / `budget` 带出来是为了让错误消息能写「16.01 / 20.00 元」——
    用户看到具体数字才知道离上限还差多少（`§8.2` 的错误消息形态）。
    """

    allowed: bool
    spent: float = 0.0
    budget: float = 0.0
    retry_at: datetime | None = None


def load_values(conn: Conn) -> dict[str, object]:
    """读全部配置键的原始值。`load_config` 与「未配置」的错误消息共用它。

    只读一次库：四个键拼成一条 `where key in (...)`。分开读四次会让
    「读了一部分」成为一种可能状态，而它没有任何合理的处理方式。
    """
    placeholders = ", ".join(f":k{idx}" for idx in range(len(_CONFIG_KEYS)))
    params = {f"k{idx}": key for idx, key in enumerate(_CONFIG_KEYS)}
    rows = conn.run(
        f"select key, value from settings where key in ({placeholders})",
        **params,
    )
    return {str(row[0]): _plain(row[1]) for row in rows}


def load_config(conn: Conn) -> ai_config.AiConfig | None:
    """从 `settings` 读出配置。三键缺任一（或值为空串）返回 `None`。

    **返回 `None` 而不是抛异常**：未配置是**正常状态**（新装、换电脑后配置丢了），
    不是错误。调用方据此走「推迟 30 分钟」而不是「失败」。
    """
    return build_config(load_values(conn))


def _plain(raw: object) -> object:
    """jsonb 值收口。pg8000 有时给 Python 对象、有时给字符串。

    只对「看起来像 JSON 的字符串」调用 `json.loads`：纯字符串
    （如 `"gpt-4o-mini"` 被存成 jsonb 后的 `'"gpt-4o-mini"'`）会返回裸字符串，
    而 `https://...` 这类以 `h` 开头的值直接 `json.loads` 会抛异常。
    """
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if not text or text[0] not in '"{[':
        return raw
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return raw


def build_config(values: dict[str, object]) -> ai_config.AiConfig | None:
    """把键值字典收成 `AiConfig`。缺必需键时返回 `None`。

    与 `load_config` 分开是为了让「配置判断」能被纯函数单测（`E6`：
    AI 层的单测不建 `conn` fixture 也能跑；这一层同理）。
    """
    if ai_config.missing_keys(values):
        return None
    return ai_config.AiConfig.from_values(values)


def missing_keys_message(values: dict[str, object]) -> str:
    """「未配置」的错误消息，含缺失的键名（`§8.2`）。"""
    missing = ai_config.missing_keys(values)
    return f"已推迟：AI 未配置（缺 {'、'.join(missing)}），30 分钟后重试"


def check_budget(
    conn: Conn,
    config: ai_config.AiConfig,
    now: datetime,
    chars_in: int,
) -> BudgetDecision:
    """预算闸门：`已花 + 本次预估 >= 上限` → 不放行。

    **闸门放在 worker 而不是 AI 层**（设计 §6.4）：判预算要读 `settings`，
    `kb/ai/` 不许 import `kb.store`；放在 store 又会让它知道模型单价与
    token 估算口径。

    `chars_in` 是**实际要送给模型的字符数**（正文截到 3000 字后的长度），
    不是条目正文长度：用正文长度会把长文章的预估虚高十倍，闸门提前关门。
    """
    month = usage_store.month_key(now)
    state = usage_store.read(conn, month)
    budget = usage_store.budget_cny(conn)
    estimate = config.estimate_cny(chars_in)

    if usage_store.can_spend(spent=state.cny, estimate=estimate, budget=budget):
        return BudgetDecision(allowed=True, spent=state.cny, budget=budget)

    log.info(
        "预算超限，推迟到下月 month=%s spent=%.4f budget=%.2f estimate=%.4f",
        month,
        state.cny,
        budget,
        estimate,
    )
    return BudgetDecision(
        allowed=False,
        spent=state.cny,
        budget=budget,
        retry_at=usage_store.next_month_start(now),
    )


def budget_defer_reason(decision: BudgetDecision) -> str:
    """预算超限的错误消息（`§8.2`：带具体金额与下次重试时刻）。"""
    retry = decision.retry_at
    when = (
        f"下月 {retry.day} 日 {retry.hour:02d}:{retry.minute:02d}" if retry else "下月"
    )
    return (
        f"已推迟：本月额度已用完（{decision.spent:.2f} / {decision.budget:.2f} 元），"
        f"{when} 自动重试"
    )


def record_usage(
    conn: Conn,
    config: ai_config.AiConfig,
    *,
    usage: TokenUsage,
    now: datetime,
) -> None:
    """记账：累加用量并检查 80% 线。**一条 SQL 完成**（见 `store/usage.py`）。

    金额口径：`单价 × (prompt + completion) / 1000`。`usage` 里的 token 数
    若为 0（响应不带 `usage`），`AiClient` 已按字符数估算过（`D2`）——
    这里不再估算，避免两处各自估算出不同的数。

    记账失败不让任务失败：钱已经花出去了，把摘要标成失败会让用户重跑、
    再花一次钱。这里只记一条 warning。
    """
    total_tokens = usage.prompt_tokens + usage.completion_tokens
    delta_cny = config.price_cny_per_1k * total_tokens / 1000.0
    budget = usage_store.budget_cny(conn)
    usage_store.add(
        conn,
        month=usage_store.month_key(now),
        calls=1,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        delta_cny=delta_cny,
        warn_line=usage_store.warn_line(budget),
        now=now,
    )


def failure_message(failed: SummarizeFailed) -> str:
    """写进 `jobs.error` 的完整错误文本：**必须含响应前 200 字**。

    ## 为什么不能只写 `failed.message`

    `B1` / `B2` / `B28` 三条验收都要求「`jobs.error` 里能看到模型/网关到底
    回了什么」，否则排障时只知道「解析失败」，不知道是半截 JSON、HTML 网关页
    还是别的。真库上实测过：只写 `message` 时 `error` 里没有响应原文，
    `test_解析失败三次后failed且error含响应前200字_B2` 直接红。

    `AiClient` 把内容放在 `failed.snippet` 字段里（不在 `message` 里），
    因为「错误摘要」与「响应原文」是两件事，混在一个字段里会让 `message`
    在日志中变得很长。**拼接的责任在落库这一层**：这是唯一写
    `jobs.error` 的出口，改一处就覆盖全部失败类别。

    格式（`§8.2`）：`<摘要>；响应前 200 字：<原文>`。没有 `snippet` 时
    只写摘要，不留一个空的「响应前 200 字：」尾巴。
    """
    if not failed.snippet:
        return failed.message
    return f"{failed.message}；响应前 200 字：{failed.snippet}"


def apply_failure(conn: Conn, job_id: int, failed: SummarizeFailed) -> None:
    """按 `category` 落地（`§4.5` 的逐条对应）。

        | category | 动作 | `attempts` |
        |---|---|---|
        | `AUTH` | `fail_hard`（一次判死，`B26`） | +1 后直接 failed |
        | `CONNECT` | `mark_failed_delayed(180)`（`B22`） | +1 |
        | `HTTP_429` 且有 Retry-After | `mark_failed_delayed(retry_after)`（`B25`） | +1 |
        | 其余 | `mark_failed`（1/2 分钟指数退避） | +1 |

    `HTTP_429` 无 `Retry-After` 时退回 `mark_failed`：没拿到服务端的建议，
        就按我们自己的指数退避，行为仍然确定（`§9` 第 9 条）。

    所有分支都走 `failure_message(...)` 取文本——**四类落地必须写同样完整的
    错误消息**，否则「哪一类失败看不到响应原文」会随类别而变，排障时得先想起
    是哪一类才知道该不该去别处找原文。
    """
    text = failure_message(failed)

    if failed.category is FailureCategory.AUTH:
        jobs_store.fail_hard(conn, job_id, text)
        return

    if failed.category is FailureCategory.CONNECT:
        jobs_store.mark_failed_delayed(conn, job_id, text, DEFER_SECONDS_CONNECT)
        return

    if failed.category is FailureCategory.HTTP_429 and failed.retry_after_sec > 0:
        jobs_store.mark_failed_delayed(conn, job_id, text, failed.retry_after_sec)
        return

    jobs_store.mark_failed(conn, job_id, text)

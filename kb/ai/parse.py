"""响应解析：JSON 形状校验、四条质量判据、超长按句末标点截断。

**为什么单独成文件**（设计 §3.1）：本轮测试最密的一块（B1–B5、B11、B16），
单独成文件才能被 `--cov-report=term-missing` 精确统计到 ≥85%（`G8` 要求它
必须出现在覆盖率表里）。

**这一层不判 HTTP 状态码**。`AiClient` 把「响应 200 但 body 是 HTML」也原样送进
这里（设计 §T03 易错点 ③）：让 B28（HTML）与 B1（非 JSON）走同一条路径，
两条验收的错误消息才一致。自己判 Content-Type 会产生两套措辞。
"""

from __future__ import annotations

import json
import re

from .config import SUMMARY_MAX_CHARS, SUMMARY_MIN_CHARS
from .outcomes import (
    FailureCategory,
    SummarizeFailed,
    SummarizeOk,
    SummarizeSkipped,
    TokenUsage,
)

#: 句子终结标点。中文摘要按这组断句（`B4`）。
SENTENCE_END = "。！？…；!?;"

#: 错误消息里带多少字的响应原文。200 是 `§8.2` 定的口径，三处（B1/B2/B28）都断言它。
SNIPPET_CHARS = 200

# 兼容端点常在 JSON 外面套一层 markdown 代码块，剥离它不算「放宽校验」——
# 里面仍是同一个 JSON 对象，形状判据一条都不少。
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def parse_summary(
    raw_text: str, title: str
) -> SummarizeOk | SummarizeSkipped | SummarizeFailed:
    """把模型响应解析成三种结果之一。

    判定顺序（顺序有意义，改它会让错误消息对不上验收）：

    1. 能否解析成 JSON 对象（`B1`）
    2. `summary` 是否存在、是字符串、去空白后非空（`B3`）
    3. `tags` 是否存在且是数组（`B11`）
    4. 摘要长度 10–100（`B5`、`AI-13` ①②），超长按句末标点截断（`B4`）
    5. 摘要是否与标题逐字相同（`B16`，两侧都去空白）
    """
    snippet = _snippet(raw_text)
    payload = _load_object(raw_text)
    if payload is None:
        return SummarizeFailed(
            category=FailureCategory.BAD_RESPONSE,
            message="AI解析失败：响应不是合法 JSON 对象",
            snippet=snippet,
        )

    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return SummarizeFailed(
            category=FailureCategory.BAD_RESPONSE,
            message=(
                "AI输出非法：summary 缺失、不是字符串或为空"
                f"（实际 {type(summary).__name__}）"
            ),
            snippet=snippet,
        )
    summary = summary.strip()

    tags_raw = payload.get("tags")
    if not isinstance(tags_raw, list):
        return SummarizeFailed(
            category=FailureCategory.BAD_RESPONSE,
            message=(
                f"AI输出非法：tags 缺失或不是数组（实际 {type(tags_raw).__name__}）"
            ),
            snippet=snippet,
        )
    # 非字符串元素在这里就摘掉：B10 要求「不抛异常」，而不是「抛了再兜住」。
    # 空串保留，交给词表校验去丢（那一层负责归一化，职责不重叠）。
    tags = tuple(tag for tag in tags_raw if isinstance(tag, str))

    if len(summary) < SUMMARY_MIN_CHARS:
        return SummarizeFailed(
            category=FailureCategory.BAD_RESPONSE,
            message=(
                f"AI输出非法：摘要长度 {len(summary)} 字"
                f"（要求 {SUMMARY_MIN_CHARS}–{SUMMARY_MAX_CHARS} 字）"
            ),
            snippet=snippet,
        )

    summary = _truncate(summary)

    # 比较前两侧都去空白：模型常在标题前后带回车，只比一侧会漏判（B16）。
    # 标题为空时不做这个判断——「等于空标题」无从谈起。
    if title.strip() and summary == title.strip():
        return SummarizeSkipped(reason="title_only")

    return SummarizeOk(summary=summary, tags=tags, usage=_usage_of(payload))


def _snippet(raw_text: str) -> str:
    """响应前 200 字。写进 `jobs.error`，排障时能看到模型到底说了什么。"""
    return raw_text[:SNIPPET_CHARS]


def _load_object(raw_text: str) -> dict[str, object] | None:
    """剥掉 markdown 代码块后按 JSON 解析。顶层不是对象一律返回 `None`。

    返回 `None` 而不是抛异常：这一层的契约是「三种结果之一」，不是「可能抛」。
    """
    text = _FENCE_RE.sub("", raw_text.strip()).strip()
    try:
        loaded = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    return {str(k): v for k, v in loaded.items()}


def _usage_of(payload: dict[str, object]) -> TokenUsage:
    """从响应的 `usage` 字段取 token 数。

    不带 `usage` 时返回 0，**不在这里估算**：估算口径（「字符数 × 1.0」）
    属于记账决策，在 worker 侧（`D2`）。这里编一个数会让「真的没有 usage」
    与「估算出来的」在数据库里长得一样，事后无法区分。
    """
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return TokenUsage()
    return TokenUsage(
        prompt_tokens=_as_int(raw.get("prompt_tokens")),
        completion_tokens=_as_int(raw.get("completion_tokens")),
    )


def _as_int(value: object) -> int:
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


def _truncate(summary: str) -> str:
    """超长摘要按句末标点截断，找不到合适的标点才硬截。

    **硬截取 99 字 + `…` = 100 字**（设计 §0 第 5 条 / §6.8）。PRD 的写法是
    「硬截 100 字并补 `…`」，那样得到 101 字，与同一条需求里的
    `len(summary) ≤ 100` 直接冲突。两条都是 P0，取 99+`…` 让两条同时成立。

    句末标点太靠前时（截出来不足下限 10 字）也走硬截：`"短句。" + 174 字`
    若截到第 3 个字会产出一条「合法但没信息」的摘要，那比超长更难排查。
    """
    if len(summary) <= SUMMARY_MAX_CHARS:
        return summary

    window = summary[:SUMMARY_MAX_CHARS]
    # 从右往左找第一个句末标点：越靠后保留的信息越多
    for idx in range(len(window) - 1, -1, -1):
        if window[idx] in SENTENCE_END:
            candidate = window[: idx + 1]
            if len(candidate) >= SUMMARY_MIN_CHARS:
                return candidate
            break

    return summary[: SUMMARY_MAX_CHARS - 1] + "…"

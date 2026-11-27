"""`AiClient`：一次调用的完整语义——拼请求、调传输、把结果翻成三种类型。

**不做重试**（`AI-12`）。这条不是洁癖：一次任务在客户端里偷偷重试 2 次，
数据库只记 1 次 `attempts`，界面上「重试了几次」就是错的，`mark_failed` 的
指数退避也完全失效。两套计数必然有一天打架，且打架时无法从任何一张表看出来。

**不判「该不该调用」**。配置是否齐备、预算是够、客户端在不在，三件事全在
`kb/worker/`。所以这个类拿到的 `AiConfig` 永远是完整的，不需要在内部写
「配置可能为空」的分支——`E6` 要求的「AI 层能脱离数据库单测」由此成立。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import prompts
from .config import AiConfig
from .outcomes import (
    AiOutcome,
    FailureCategory,
    SummarizeFailed,
    SummarizeOk,
    TokenUsage,
)
from .parse import SNIPPET_CHARS, parse_summary
from .transport import Transport, TransportError, TransportResponse

log = logging.getLogger(__name__)

#: 各传输失败的 kind → 失败类别。**只在这一处映射**：两处各判一次的话，
#: 「`ConnectTimeout` 算 CONNECT 还是 TIMEOUT」这种分歧会出现在两个文件里。
_KIND_TO_CATEGORY: dict[str, FailureCategory] = {
    "timeout": FailureCategory.TIMEOUT,
    "connect": FailureCategory.CONNECT,
    "unexpected": FailureCategory.UNEXPECTED,
}

#: `job.error` 的中文类别名（`§8.2` 的错误消息格式定死了前缀）。
_CATEGORY_LABEL: dict[FailureCategory, str] = {
    FailureCategory.BAD_RESPONSE: "AI解析失败",
    FailureCategory.TOO_LARGE: "AI响应过大",
    FailureCategory.TIMEOUT: "AI超时",
    FailureCategory.CONNECT: "AI连接失败",
    FailureCategory.HTTP_5XX: "AI服务端错误",
    FailureCategory.HTTP_429: "AI被限流",
    FailureCategory.AUTH: "AI鉴权失败",
    FailureCategory.UNEXPECTED: "AI调用异常",
}


class AiClient:
    """`SummarizerPort` 的生产实现。持有配置与传输，无状态。"""

    def __init__(self, *, config: AiConfig, transport: Transport) -> None:
        self.config = config
        self.transport = transport

    # ── SummarizerPort ────────────────────────────────────────

    def summarize(
        self,
        title: str,
        text: str,
        vocabulary: tuple[tuple[int, str, str], ...] = (),
    ) -> AiOutcome:
        """发一次请求并解析响应。**永远返回一个结果类型，不抛异常。**

        「不抛」是这里的契约：三种形态之外的第四种（异常）会让 worker 那条
        「任何异常收成任务失败」的兜底把 `title_only` 这类正常结论也算成失败，
        `B15`/`B16` 断言的 `attempts=0` 直接不成立。
        """
        # basis 只影响 prompt 里的一句说明，不影响请求形状。按正文长度判一次，
        # 与 worker 侧 `choose_input` 的结果一致（两边共用 prompts 的阈值常量）。
        basis = "content" if len(text.strip()) >= prompts.CONTENT_MIN_CHARS else "title"
        messages = prompts.build_messages(
            title=title, text=text, vocabulary=vocabulary, basis=basis
        )
        body = json.dumps(self._payload(messages), ensure_ascii=False)
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = self.transport.post(self._endpoint(), headers, body, self.config)
        except TransportError as exc:
            category = _KIND_TO_CATEGORY.get(exc.kind, FailureCategory.UNEXPECTED)
            log.warning(
                "AI 调用失败 category=%s kind=%s message=%s",
                category.value,
                exc.kind,
                exc.message,
            )
            return SummarizeFailed(
                category=category,
                message=f"{_CATEGORY_LABEL[category]}：{exc.message}",
                retry_after_sec=exc.retry_after_sec,
            )

        return self._interpret(response, title, body)

    # ── 内部 ──────────────────────────────────────────────────

    def _endpoint(self) -> str:
        """`base_url` + `/chat/completions`。末尾多余斜杠会被去掉——
        `//chat/completions` 在多数网关上是 404，而用户在配置里带尾斜杠是最常见的写法。
        """
        return self.config.base_url.rstrip("/") + "/chat/completions"

    def _payload(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        """OpenAI 兼容的请求体。

        `response_format={"type":"json_object"}` 取 openai-python 的字段口径：
        能把格式错误率降到接近零。但**仍保留解析失败的重试路径**——
        不是所有兼容端点都真的实现了这个参数（设计「复用调研」）。
        """
        return {
            "model": self.config.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.3,
        }

    def _interpret(
        self, response: TransportResponse, title: str, request_body: str
    ) -> AiOutcome:
        """`TransportResponse` → 三种结果之一。

        判定顺序反映「谁先错」：状态码 → 截断 → 解析。反过来的话，
        500 的 HTML 会被报成「不是合法 JSON」，真正原因被盖掉（`B12`）。
        """
        snippet = response.body[:SNIPPET_CHARS]

        if response.truncated:
            # B29：截断的响应即使能解析出 JSON 也是残片，内容不可信。
            log.warning("AI 响应过大 category=TOO_LARGE status=%s", response.status)
            return SummarizeFailed(
                category=FailureCategory.TOO_LARGE,
                message=(
                    f"{_CATEGORY_LABEL[FailureCategory.TOO_LARGE]}："
                    f"响应超过 {self.config.max_response_bytes} 字节已截断"
                ),
                http_status=response.status,
                snippet=snippet,
            )

        if response.status in (401, 403):
            # B26：密钥错属于不可自愈错误，一次判死。重试 3 次只是白等 3 分钟，
            # 且用户看不到真因。
            log.warning("AI 鉴权失败 category=AUTH status=%s", response.status)
            return SummarizeFailed(
                category=FailureCategory.AUTH,
                message=(
                    f"{_CATEGORY_LABEL[FailureCategory.AUTH]}：HTTP {response.status}；"
                    "检查 api_key 是否有效或余额是否充足"
                ),
                http_status=response.status,
                snippet=snippet,
            )

        if response.status == 429:
            return SummarizeFailed(
                category=FailureCategory.HTTP_429,
                message=(
                    f"{_CATEGORY_LABEL[FailureCategory.HTTP_429]}：HTTP 429；"
                    + (
                        f"按 Retry-After 等 {response.retry_after_sec:.0f} 秒"
                        if response.retry_after_sec > 0
                        else "未给出 Retry-After，按指数退避重试"
                    )
                ),
                http_status=response.status,
                retry_after_sec=response.retry_after_sec,
                snippet=snippet,
            )

        if response.status >= 500:
            return SummarizeFailed(
                category=FailureCategory.HTTP_5XX,
                message=(
                    f"{_CATEGORY_LABEL[FailureCategory.HTTP_5XX]}："
                    f"HTTP {response.status}；1/2 分钟后重试"
                ),
                http_status=response.status,
                snippet=snippet,
            )

        if response.status != 200:
            return SummarizeFailed(
                category=FailureCategory.UNEXPECTED,
                message=(
                    f"{_CATEGORY_LABEL[FailureCategory.UNEXPECTED]}："
                    f"HTTP {response.status}"
                ),
                http_status=response.status,
                snippet=snippet,
            )

        # 走到这里才交给解析层。B28（HTML 网关页）与 B1（半截 JSON）
        # 因此走同一条路径，两条验收的错误消息天然一致。
        outcome = parse_summary(response.body, title)
        if isinstance(outcome, SummarizeOk):
            return self._with_usage(outcome, request_body)
        if isinstance(outcome, SummarizeFailed):
            log.warning("AI 输出非法 category=%s", outcome.category.value)
        return outcome

    def _with_usage(self, outcome: SummarizeOk, request_body: str) -> SummarizeOk:
        """响应不带 `usage` 时按「字符数 × 1.0」估算（`D2`）。

        估算必须在**这一层**做：解析层拿不到「实际送了多少字符」，只有这里
        同时拥有请求体与响应。宁可估高（提前停）也不能记 0（永远花不完）。
        """
        if outcome.usage.prompt_tokens or outcome.usage.completion_tokens:
            return outcome
        prompt_tokens = len(request_body)
        completion_tokens = max(len(outcome.summary), 1)
        return SummarizeOk(
            summary=outcome.summary,
            tags=outcome.tags,
            usage=TokenUsage(
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
            ),
        )

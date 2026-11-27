"""AI 层的结果类型：成功 / 跳过 / 失败三种形态。

**为什么不用「布尔 + 异常」**（设计 §4.3）：`title_only`（摘要与标题逐字相同）与
`no_text`（正文标题都空）**不是错误**，它们是「这次调用没有产出可用摘要」的正常
结论。用异常表达会被 `kb/worker/loop.py` 那条「任何异常都收成任务失败」的兜底
吞掉，`attempts` 被 +1，而 `B15`/`B16` 断言的 `attempts=0` 直接不成立。

所以三种形态必须在**类型层面**区分：`isinstance` 就能判，不需要读布尔标志位。

本模块只放**类型**，不放任何行为——它被 `kb/ai/client.py` 产出、
`kb/worker/steps.py` 消费、`tests/` 断言三处引用。放进 `client.py` 会让只测
解析的用例为了拿类型而 import 一个需要 `httpx` 的模块（`B27` 的干净环境里
那条路径必须能明确失败，不该被测试间接依赖）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class FailureCategory(str, Enum):
    """失败分类。每一个对应 `design §4.5` 表里的**一种落地动作**。

    分类的意义不是「好看」，而是「`attempts` 要不要 +1、`run_after` 推多远、
    要不要一次判死」。语言层面用 `str` 混入是为了让日志与 `jobs.error`
    直接可读（`category=AUTH` 比 `category=FailureCategory.AUTH` 短）。
    """

    BAD_RESPONSE = "BAD_RESPONSE"  # 非 JSON / 形状非法 / 长度越界 / tags 非法
    TOO_LARGE = "TOO_LARGE"  # 响应超 256KB 即停读
    TIMEOUT = "TIMEOUT"  # 读超时（连上了但无数据）
    CONNECT = "CONNECT"  # DNS 失败 / 连接被拒 / 建连超时
    HTTP_5XX = "HTTP_5XX"  # 500 / 502 / 503
    HTTP_429 = "HTTP_429"  # 限流
    AUTH = "AUTH"  # 401 / 403，一次判死
    UNEXPECTED = "UNEXPECTED"  # 其他未分类异常


@dataclass(frozen=True)
class TokenUsage:
    """一次调用的 token 用量。响应不带 `usage` 时为 0，由调用方按字符数估算（`D2`）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True)
class SummarizeOk:
    """响应合法且摘要通过四条判据。`tags` 是**模型返回的原始名**，未做词表校验。

    词表校验发生在 `tag` 步骤（隔着一次落库，见设计 §3.1 的 `validate.py` 说明），
    所以这里保留原始名而不是 `tag_id`：`A5`（空数组）与 `B19`（重跑对照）
    都需要「模型当时说了什么」这份证据。
    """

    summary: str
    tags: tuple[str, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)


@dataclass(frozen=True)
class SummarizeSkipped:
    """调用成功但摘要不可用。`reason` 只有两个合法值：`title_only` / `no_text`。"""

    reason: str


@dataclass(frozen=True)
class SummarizeFailed:
    """任何不产出可用摘要的情况：HTTP 失败、非 JSON、形状非法、长度越界。

    `snippet` 是响应前 200 字，写进 `jobs.error` 供排障（`B2`/`B28` 断言它存在）。
    **完整响应体不进日志**（设计 §8.2）：日志会进 `journalctl`，密钥进去等于泄露。
    """

    category: FailureCategory
    message: str
    http_status: int = 0
    retry_after_sec: float = 0.0
    snippet: str = ""


#: 三种结果形态的联合类型。`kb/ai/client.py` 的返回类型与 worker 的分派依据。
AiOutcome = SummarizeOk | SummarizeSkipped | SummarizeFailed


class SummarizerPort(Protocol):
    """worker 眼里「会出摘要的东西」。

    它不知道对面是真模型还是测试里的 `FakeSummarizer`——这是整轮 45 条验收用例
    能被机器验证的前提（设计 §4.2）。签名只有三个入参：标题、正文、
    词表（`(tag_id, name, parent_name)` 三元组序列）。
    """

    def summarize(
        self,
        title: str,
        text: str,
        vocabulary: tuple[tuple[int, str, str], ...] = (),
    ) -> AiOutcome:  # pragma: no cover - 协议无实现
        ...

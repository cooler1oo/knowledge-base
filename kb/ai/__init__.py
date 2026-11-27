"""AI 层的公开入口。

**worker 只 import 这一个模块名**（设计 §3.1）。若让它直接 import 子模块，
将来拆文件会牵连 `kb/worker/` 的所有 import 语句。

## 为什么 `AiClient` / `HttpxTransport` 是**惰性**导出

`import kb.ai.client` 会先执行本文件（包 `__init__`）。若本文件在模块级
拉 `client` / `transport`，那么 `kb/worker/steps.py` 里那句
`from kb.ai import outcomes` 会连带把 `httpx` 拉进整棵依赖树——后果是
干净环境缺 `httpx` 时，`python -m kb.worker` 在**导入 `kb.worker` 阶段**
就抛一个原始 traceback，`ensure_ai_available()` 那段友好提示**永远执行不到**。

实测踩过：先用「`__init__` 里 import client」的写法，`B27` 用例拿到的是
`ImportError: No module named 'httpx'` 的裸 traceback，而不是含
「pip install httpx」的那段说明。

所以：**类型与配置**在模块级（它们不依赖 `httpx`，`E6` 要求的「纯函数单测
不需要装 httpx」由此成立），**真实实现**用模块级 `__getattr__` 惰性导入。
`B27` 的「启动即失败」由 `kb/worker/__main__.ensure_ai_available()` 显式
`import kb.ai.client` 承担——那是**进程启动的第一件事**，仍在写心跳之前。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .config import AiConfig, missing_keys
from .outcomes import (
    AiOutcome,
    FailureCategory,
    SummarizeFailed,
    SummarizeOk,
    SummarizerPort,
    SummarizeSkipped,
    TokenUsage,
)

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from .client import AiClient
    from .transport import HttpxTransport, Transport, TransportError, TransportResponse

__all__ = [
    "AiClient",
    "AiConfig",
    "AiOutcome",
    "FailureCategory",
    "HttpxTransport",
    "SummarizeFailed",
    "SummarizeOk",
    "SummarizeSkipped",
    "SummarizerPort",
    "TokenUsage",
    "Transport",
    "TransportError",
    "TransportResponse",
    "build_client",
    "missing_keys",
]

#: 惰性导出的名字 → 定义它的子模块。加新名字只改这张表。
_LAZY: dict[str, str] = {
    "AiClient": ".client",
    "HttpxTransport": ".transport",
    "Transport": ".transport",
    "TransportError": ".transport",
    "TransportResponse": ".transport",
}


def __getattr__(name: str) -> Any:
    """模块级惰性导出。`from kb.ai import AiClient` 时才真正 import `client`。

    这是 PEP 562 的标准做法，不是 monkeypatch：属性访问走这里，
    `dir(kb.ai)` 与 IDE 的补全由 `__all__` 覆盖。
    """
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value  # 缓存，后续访问不再走 __getattr__
    return value


def build_client(config: AiConfig) -> Any:
    """按配置造一个真实客户端。`kb/worker/__main__.py` 与生产装配走这一条。

    单独留一个工厂而不是让调用方 `AiClient(config, HttpxTransport())`：
    「用哪个 `Transport` 实现」是装配知识，`__main__` 不该知道
    `HttpxTransport` 这个类存在。

    这里显式 `import kb.ai.client` 而不是依赖 `__getattr__`：工厂函数在**调用时**
    就要确认依赖齐全，缺包应当在这一行炸开（带明确消息），而不是等某次属性访问。
    """
    from .client import AiClient
    from .transport import HttpxTransport

    return AiClient(config=config, transport=HttpxTransport())

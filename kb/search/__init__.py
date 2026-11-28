"""检索层的公开入口。

**惰性导出**，沿用 `kb/ai/__init__.py` 的写法与理由：`models` / `config` / `query`
是纯数据类型与纯函数，`client` / `indexer` / `service` 会拉 `httpx`。
若本文件在模块级 import 后三者，`from kb.search import models` 也会把 `httpx`
拉进整棵依赖树——后果是「只想验 schema 的单测」在缺 `httpx` 的环境里 import 失败。

所以：**契约类型与常量**在模块级，**真实实现**用 PEP 562 的模块级 `__getattr__`
惰性导入。这不是 monkeypatch，是标准做法。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .config import DISPLAY_TZ, INDEX_UID, MIN_SCORE
from .models import (
    IndexDoc,
    MeiliDown,
    MeiliRejected,
    MeiliUnauthorized,
    MeiliUnavailable,
    SearchFilter,
    SearchHit,
    SearchMode,
    SearchQuery,
    SearchResult,
)

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from .client import MeiliClient
    from .indexer import Indexer
    from .service import FallbackFn, SearchService

__all__ = [
    "DISPLAY_TZ",
    "INDEX_UID",
    "MIN_SCORE",
    "FallbackFn",
    "IndexDoc",
    "Indexer",
    "MeiliClient",
    "MeiliDown",
    "MeiliRejected",
    "MeiliUnauthorized",
    "MeiliUnavailable",
    "SearchFilter",
    "SearchHit",
    "SearchMode",
    "SearchQuery",
    "SearchResult",
    "SearchService",
]

#: 惰性导出的名字 → 定义它的子模块。加新名字只改这张表。
_LAZY: dict[str, str] = {
    "MeiliClient": ".client",
    "Indexer": ".indexer",
    "SearchService": ".service",
    "FallbackFn": ".service",
}


def __getattr__(name: str) -> Any:
    """模块级惰性导出。`from kb.search import MeiliClient` 时才真正 import `client`。"""
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value  # 缓存，后续访问不再走 __getattr__
    return value

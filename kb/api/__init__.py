"""HTTP 层。只有这一层知道 Starlette 的存在，`kb/store` 不知道有 HTTP。"""

from __future__ import annotations

from .app import AppConfig, create_app

__all__ = ["AppConfig", "create_app"]

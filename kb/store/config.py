"""Postgres 连接参数：环境变量优先，默认值只对「在平板 Termux 里直接跑」成立。

与 proot 容器有关的一个事实：容器内 `getpass.getuser()` 返回 `root`，
而平板上 PG 的 role 是 `u0_a201`。所以容器内运行时必须显式设 `KB_PG_USER`，
否则报 `role "root" does not exist`。
"""

from __future__ import annotations

import getpass
import os
from dataclasses import dataclass, replace

DEFAULT_DATABASE = "kb"
TEST_DATABASE = "kb_test"


def _current_user() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):  # 无 LOGNAME/USER 环境变量时的兜底
        return "postgres"


@dataclass(frozen=True)
class PgConfig:
    host: str
    port: int
    user: str
    database: str
    password: str | None = None

    @classmethod
    def from_env(cls, database: str | None = None) -> PgConfig:
        password = os.environ.get("KB_PG_PASSWORD")
        return cls(
            host=os.environ.get("KB_PG_HOST", "127.0.0.1"),
            port=int(os.environ.get("KB_PG_PORT", "5432")),
            user=os.environ.get("KB_PG_USER") or _current_user(),
            database=database or os.environ.get("KB_PG_DATABASE", DEFAULT_DATABASE),
            password=password or None,
        )

    def with_database(self, database: str) -> PgConfig:
        return replace(self, database=database)

    @property
    def params(self) -> dict[str, object]:
        """pg8000.native.Connection 的关键字参数。trust 认证时不能传 password=None。"""
        params: dict[str, object] = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "database": self.database,
        }
        if self.password:
            params["password"] = self.password
        return params

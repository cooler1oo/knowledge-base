"""共享 fixture。

集成测试的开关是「有没有显式配置数据库」：

- 配了 `KB_PG_*` 就等于承诺能连上，连不上是**失败**，不是跳过。
  原因：2026-11-26 21:54:48 出现过一次「16 个用例在 70ms 内全跳过、
  `pytest -q` 退出码 0、看起来全绿」的事故，真实故障被 `skip` 吞掉了。
- 没配就是「本机没装库」，跳过并在 `-ra` 里显示原因。
"""

from __future__ import annotations

import os

import pytest

from kb.store.config import TEST_DATABASE, PgConfig
from kb.store.db import connect

# 出现任意一个就视为「显式承诺能连库」
_EXPLICIT_ENV = ("KB_PG_HOST", "KB_PG_PORT", "KB_PG_USER", "KB_PG_DATABASE")


def explicit_env() -> list[str]:
    return [name for name in _EXPLICIT_ENV if os.environ.get(name)]


def pg_test_config_or_skip() -> PgConfig:
    """返回指向 kb_test 的配置；未显式配置且连不上时跳过。"""
    cfg = PgConfig.from_env(TEST_DATABASE)
    explicit = explicit_env()
    try:
        conn = connect(cfg.with_database("postgres"))
    except Exception as exc:
        detail = f"Postgres 不可达（{cfg.host}:{cfg.port}，用户 {cfg.user}）：{exc}"
        if explicit:
            pytest.fail(f"已显式设置 {'/'.join(explicit)}，视为承诺连库；{detail}")
        pytest.skip(f"未设置 KB_PG_*，按「本机无库」跳过。{detail}")
    conn.close()
    return cfg


@pytest.fixture(scope="session")
def pg_cfg() -> PgConfig:
    return pg_test_config_or_skip()

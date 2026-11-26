"""共享 fixture。

集成测试的开关是「Postgres 是否连得上」：连不上就 skip 并打印原因，
这样本地 Windows 上跑 pytest 是绿的，在平板上跑就是真的连库测。
"""

from __future__ import annotations

import pytest

from kb.store.config import TEST_DATABASE, PgConfig
from kb.store.db import connect


def pg_test_config_or_skip() -> PgConfig:
    """返回指向 kb_test 的配置；连不上就跳过当前测试。"""
    cfg = PgConfig.from_env(TEST_DATABASE)
    try:
        conn = connect(cfg.with_database("postgres"))
    except Exception as exc:  # 网络不通、服务没起、role 不存在都走这里
        pytest.skip(f"Postgres 不可达（{cfg.host}:{cfg.port}）：{exc}")
    conn.close()
    return cfg


@pytest.fixture(scope="session")
def pg_cfg() -> PgConfig:
    return pg_test_config_or_skip()

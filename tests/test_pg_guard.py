"""连接失败的两条边界：什么时候该跳过、什么时候必须失败。

背景：2026-11-26 21:54:48 有一次 16 个真库用例在 70ms 内全部 skip，
`pytest -q` 退出码 0，看起来全绿；实际是网络瞬断（PG 侧零日志可查）。
这里把「配了 KB_PG_* 就是承诺」这条规则钉成测试，避免同类事故再次被吞。
"""

from __future__ import annotations

import pg8000.exceptions
import pytest

import tests.conftest as conftest_mod
from kb.store.config import TEST_DATABASE, PgConfig
from kb.store.db import PgUnreachable, connect

ENV_NAMES = ("KB_PG_HOST", "KB_PG_PORT", "KB_PG_USER", "KB_PG_DATABASE")


@pytest.fixture(autouse=True)
def _clean_pg_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _raise_unreachable(*_args, **_kwargs):
    raise PgUnreachable(
        PgConfig(host="127.0.0.1", port=5432, user="u", database="postgres"),
        111,
        3.2,
        "can't connect",
    )


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


# ── 跳过 / 失败的判定 ──────────────────────────────────────────


def test_skips_when_not_configured(monkeypatch):
    monkeypatch.setattr(conftest_mod, "connect", _raise_unreachable)
    with pytest.raises(pytest.skip.Exception) as excinfo:
        conftest_mod.pg_test_config_or_skip()
    assert "未设置 KB_PG_*" in str(excinfo.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KB_PG_HOST", "100.124.156.47"),
        ("KB_PG_PORT", "5432"),
        ("KB_PG_USER", "u0_a201"),
        ("KB_PG_DATABASE", "kb_test"),
    ],
)
def test_fails_when_any_env_is_explicitly_set(monkeypatch, name, value):
    """四个变量任一显式设置，都算「承诺能连库」，连不上必须失败。"""
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(conftest_mod, "connect", _raise_unreachable)
    with pytest.raises(pytest.fail.Exception) as excinfo:
        conftest_mod.pg_test_config_or_skip()
    message = str(excinfo.value)
    assert "承诺连库" in message
    assert name in message


def test_returns_test_database_config(monkeypatch):
    fake = _FakeConn()

    def _ok(_cfg):
        return fake

    monkeypatch.setattr(conftest_mod, "connect", _ok)
    cfg = conftest_mod.pg_test_config_or_skip()
    assert cfg.database == TEST_DATABASE
    assert fake.closed, "探活连接用完必须关掉，否则每次连库都漏一条"


def test_explicit_env_lists_only_set_names(monkeypatch):
    assert conftest_mod.explicit_env() == []
    monkeypatch.setenv("KB_PG_USER", "u0_a201")
    assert conftest_mod.explicit_env() == ["KB_PG_USER"]


# ── 连接失败的信息量 ──────────────────────────────────────────


def test_unreachable_carries_errno_hint():
    exc = PgUnreachable(
        PgConfig(host="100.1.2.3", port=5432, user="u", database="kb"),
        10061,
        4.0,
        "raw",
    )
    text = str(exc)
    assert "errno=10061" in text
    assert "端口没有服务在监听" in text
    assert "耗时 4ms" in text


def test_unreachable_unknown_errno_still_reports_code():
    exc = PgUnreachable(
        PgConfig(host="h", port=5432, user="u", database="kb"), 55, 1.0, "raw"
    )
    assert "errno=55" in str(exc)
    assert "错误码未知" in str(exc)


def test_connect_wraps_interface_error_with_errno(monkeypatch):
    """pg8000 把 OSError 包成 InterfaceError，原始 errno 在 __cause__ 上，别丢。"""

    def _boom(**_kwargs):
        wrapped = pg8000.exceptions.InterfaceError(
            "Can't create a connection to host h and port 5432"
        )
        wrapped.__cause__ = OSError(111, "Connection refused")
        raise wrapped

    monkeypatch.setattr(pg8000.native, "Connection", _boom)
    cfg = PgConfig(host="h", port=5432, user="u", database="kb")
    with pytest.raises(PgUnreachable) as excinfo:
        connect(cfg)
    assert excinfo.value.errno == 111
    assert "连接被拒" in str(excinfo.value)

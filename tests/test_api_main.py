"""启动入口的参数解析。

这里的 bug 曾经真实存在且被掩盖了两轮：`main()` 把 `argv=None` 转成 `[]` 再交给
argparse，于是 `python -m kb.api --host 0.0.0.0` 的命令行参数被整份丢弃，
uvicorn 一直绑在默认的 `127.0.0.1` 上。

为什么两轮没发现：R2 的验收是在平板上访问 `127.0.0.1:8000`，回环地址永远能通；
而「经 Tailscale 从电脑/手机访问」从未真正试过，只在交接包里被写成了「可用」。
"""

from __future__ import annotations

import sys

import pytest

from kb.api import __main__ as api_main


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """替换 uvicorn.run，只记录被传入的 host/port，不真的起服务。"""
    captured: dict[str, object] = {}

    def fake_run(app: object, host: str, port: int, **_kw: object) -> None:
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr(api_main.uvicorn, "run", fake_run)
    return captured


def test_build_接受显式参数():
    _app, host, port = api_main.build(["--host", "0.0.0.0", "--port", "9000"])
    assert host == "0.0.0.0"
    assert port == 9000


def test_build_无参数时默认只听本机():
    _app, host, port = api_main.build([])
    assert host == "127.0.0.1"
    assert port == 8000


def test_main_读取真实命令行参数(monkeypatch: pytest.MonkeyPatch):
    """这是那个 bug 的回归测试：argv 为 None 时必须读 sys.argv，不能当成空列表。"""
    captured = _capture_run(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["kb.api", "--host", "0.0.0.0", "--port", "9000"])

    api_main.main()

    assert captured["host"] == "0.0.0.0", "命令行里的 --host 被丢弃了"
    assert captured["port"] == 9000


def test_main_无参数时用默认值(monkeypatch: pytest.MonkeyPatch):
    captured = _capture_run(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["kb.api"])

    api_main.main()

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8000

"""R2 纯逻辑单测：密码、令牌、Cookie、健康聚合。

不连库、不起服务——真链路由 `tests/test_api_pg.py` 在平板上跑。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from kb.api import auth, health


@pytest.fixture(autouse=True)
def _fast_bcrypt(monkeypatch):
    # 4 轮只为跑得快；生产默认 12 轮（ARM 上约 0.3–0.5s）
    monkeypatch.setenv("KB_BCRYPT_ROUNDS", "4")


# ── 密码 ──────────────────────────────────────────────────────


def test_hash_and_verify_roundtrip():
    stored = auth.hash_password("正确的密码123")
    assert stored != "正确的密码123"
    assert stored.startswith("$2")
    assert auth.verify_password("正确的密码123", stored)


def test_verify_rejects_wrong_password():
    stored = auth.hash_password("正确的密码123")
    assert not auth.verify_password("错的密码", stored)


def test_verify_rejects_garbage_hash_without_raising():
    """库里的哈希被人改坏时应该是「登录失败」，不是 500。"""
    assert not auth.verify_password("任意密码", "not-a-bcrypt-hash")
    assert not auth.verify_password("任意密码", "")


def test_password_over_72_bytes_is_rejected_at_entry():
    """bcrypt 只吃前 72 字节，超长必须显式拒绝而不是静默截断。"""
    long_password = "a" * 73
    assert auth.password_too_long(long_password)
    with pytest.raises(ValueError):
        auth.hash_password(long_password)
    stored = auth.hash_password("a" * 72)
    assert not auth.verify_password(long_password, stored)


# ── 会话令牌 ──────────────────────────────────────────────────


def test_session_token_stores_only_hash():
    raw, hashed = auth.new_session_token()
    assert raw != hashed
    assert len(hashed) == 64  # sha256 hex
    assert auth.hash_token(raw) == hashed


def test_two_tokens_differ():
    assert auth.new_session_token()[0] != auth.new_session_token()[0]


# ── Cookie ────────────────────────────────────────────────────


def test_cookie_parsing():
    header = f"a=1; {auth.COOKIE_NAME}=tok123; b=2"
    assert auth.token_from_cookie_header(header) == "tok123"
    assert auth.token_from_cookie_header("a=1") is None
    assert auth.token_from_cookie_header(None) is None
    assert auth.token_from_cookie_header("这不是cookie") is None


def test_set_cookie_attributes():
    cookie = auth.build_set_cookie("tok", secure=False)
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert "Path=/" in cookie
    assert "Max-Age=2592000" in cookie  # 30 天
    assert "Secure" not in cookie


def test_set_cookie_secure_when_enabled():
    assert "Secure" in auth.build_set_cookie("tok", secure=True)


def test_clear_cookie_expires_immediately():
    cookie = auth.build_clear_cookie(secure=False)
    assert "Max-Age=0" in cookie
    assert f"{auth.COOKIE_NAME}=;" in cookie or cookie.startswith(
        f"{auth.COOKIE_NAME}=;"
    )


# ── 健康检查 ──────────────────────────────────────────────────


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


@pytest.mark.parametrize(
    "raw",
    [
        _iso(timedelta(0)),
        {"at": _iso(timedelta(0))},
        _iso(timedelta(0)).replace("+00:00", "Z"),
    ],
)
def test_parse_heartbeat_accepts_string_dict_and_z(raw):
    parsed = health.parse_heartbeat(raw)
    assert parsed is not None
    assert parsed.tzinfo is not None


@pytest.mark.parametrize("raw", [None, "", "not-a-time", {}, {"at": None}])
def test_parse_heartbeat_rejects_junk(raw):
    assert health.parse_heartbeat(raw) is None


class _FakeConn:
    def __init__(self, script):
        self._script = script

    def run(self, sql, **params):
        return self._script(sql, params)


class _FakePool:
    def __init__(self, script):
        self._script = script

    @contextmanager
    def connection(self):
        yield _FakeConn(self._script)


def _script_factory(heartbeat):
    def _script(sql: str, _params):
        if "select 1" in sql:
            return [[1]]
        if "from settings" in sql:
            return [[json.dumps(heartbeat)]] if heartbeat is not None else []
        return []

    return _script


def test_health_check_all_green(monkeypatch, tmp_path):
    monkeypatch.setattr(health, "meili_ok", lambda *_a, **_k: True)
    pool = _FakePool(_script_factory(_iso(timedelta(seconds=-5))))
    report = health.check(pool, "http://127.0.0.1:7700", tmp_path)
    assert report["ok"] is True
    assert report["dbOk"] is True
    assert report["meiliOk"] is True
    assert report["workerOk"] is True
    assert isinstance(report["diskFreeGb"], float)


def test_health_check_worker_stale_is_not_ok(monkeypatch, tmp_path):
    """心跳超过 90s 必须判死。这条是防止有人把 workerOk 写死成 true。"""
    monkeypatch.setattr(health, "meili_ok", lambda *_a, **_k: True)
    pool = _FakePool(_script_factory(_iso(timedelta(seconds=-91))))
    report = health.check(pool, "http://127.0.0.1:7700", tmp_path)
    assert report["workerOk"] is False


def test_health_check_without_worker_heartbeat(monkeypatch, tmp_path):
    """本轮没有 worker：workerHeartbeatAt 必须是 null，workerOk 必须是 false。"""
    monkeypatch.setattr(health, "meili_ok", lambda *_a, **_k: True)
    pool = _FakePool(_script_factory(None))
    report = health.check(pool, "http://127.0.0.1:7700", tmp_path)
    assert report["workerHeartbeatAt"] is None
    assert report["workerOk"] is False


def test_health_check_db_down_makes_overall_false(monkeypatch, tmp_path):
    def _boom(_sql, _params):
        raise RuntimeError("库挂了")

    monkeypatch.setattr(health, "meili_ok", lambda *_a, **_k: True)
    report = health.check(_FakePool(_boom), "http://127.0.0.1:7700", tmp_path)
    assert report["dbOk"] is False
    assert report["ok"] is False


def test_meili_ok_false_on_network_error(monkeypatch):
    def _boom(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(health.urllib.request, "urlopen", _boom)
    assert health.meili_ok("http://127.0.0.1:7700") is False


def test_meili_ok_requires_available_status(monkeypatch):
    class _Resp:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode()

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(
        health.urllib.request,
        "urlopen",
        lambda *_a, **_k: _Resp({"status": "available"}),
    )
    assert health.meili_ok("http://127.0.0.1:7700") is True

    monkeypatch.setattr(
        health.urllib.request,
        "urlopen",
        lambda *_a, **_k: _Resp({"status": "starting"}),
    )
    assert health.meili_ok("http://127.0.0.1:7700") is False


def test_disk_free_gb_handles_missing_path(tmp_path):
    missing = tmp_path / "not-yet-created" / "deep"
    value = health.disk_free_gb(missing)
    assert isinstance(value, float)
    assert value >= 0.0


def test_meili_ok_survives_non_object_json(monkeypatch):
    """返回不是 JSON 对象时不许抛异常，否则健康检查会变成 500。

    2026-11-27 由 mypy 的 warn_return_any 带出来：原实现直接 payload.get(...)，
    返回数组时抛 AttributeError，而 except 里那几种都抓不到它。
    """

    class _Resp:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

    monkeypatch.setattr(
        health.urllib.request, "urlopen", lambda *_a, **_k: _Resp(b'["available"]')
    )
    assert health.meili_ok("http://127.0.0.1:7700") is False


# ── HTTP 层：手写最小 ASGI 驱动（不引 httpx）────────────────────
#
# 为什么要有这层：app.py 的路由与鉴权中间件此前只有 curl 端到端验证，
# 改 handler 逻辑单测不会红。这里直接调 ASGI 接口，零额外依赖。

import asyncio  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from pathlib import Path  # noqa: E402

from kb.api.app import AppConfig, create_app  # noqa: E402


class _Conn:
    def run(self, sql: str, **_params: object) -> list[list[object]]:
        return [[1]] if "select 1" in sql else []

    def close(self) -> None:
        return None


class _Pool:
    @contextmanager
    def connection(self):  # type: ignore[no-untyped-def]
        yield _Conn()

    def close_all(self) -> None:
        return None


def _call(
    app, path: str, method: str = "GET", body: object = None, cookie: str | None = None
):  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        raw = json.dumps(body).encode() if body is not None else b""
        headers = [(b"cookie", cookie.encode())] if cookie else []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("t", 1),
            "server": ("t", 80),
        }
        state = {"sent": False}

        async def receive():  # type: ignore[no-untyped-def]
            if state["sent"]:
                return {"type": "http.disconnect"}
            state["sent"] = True
            return {"type": "http.request", "body": raw, "more_body": False}

        out = {"status": 0, "body": b""}

        async def send(message):  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                out["status"] = message["status"]
            elif message["type"] == "http.response.body":
                out["body"] += message.get("body", b"")

        await app(scope, receive, send)
        return out

    return asyncio.run(go())


def _app():  # type: ignore[no-untyped-def]
    return create_app(_Pool(), AppConfig("http://127.0.0.1:7700", Path(".")))


def test_http_未登录访问首页跳登录页():
    assert _call(_app(), "/")["status"] == 303


def test_http_未登录访问_api_返回_401():
    assert _call(_app(), "/api/items")["status"] == 401


def test_http_health_不需要登录():
    r = _call(_app(), "/api/health")
    assert r["status"] == 200
    assert b"dbOk" in r["body"]


def test_http_登录页可匿名打开():
    r = _call(_app(), "/login")
    assert r["status"] == 200
    assert "个人知识库" in r["body"].decode()

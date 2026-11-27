"""Starlette 应用：路由 + 鉴权中间件。

R2 只实现 §8.1 端点表里的 4 个（bootstrap / login / logout / health）+ 两个页面。
其余端点在后续轮次，这里不占位、不留 `pass`——没实现的路径就该 404。

鉴权口径：除了白名单，一律要求有效 Cookie 会话。
`/api/*` 未登录返回 401 JSON，页面路径未登录 303 跳登录页。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pg8000.native
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from kb.store import accounts
from kb.store.db import Pool

from . import auth, health, pages

PUBLIC_PATHS = frozenset(
    {"/login", "/api/auth/login", "/api/auth/bootstrap", "/api/health", "/favicon.ico"}
)

MIN_PASSWORD_LEN = 8
MAX_USERNAME_LEN = 64


class AppConfig:
    def __init__(
        self, meili_url: str, media_root: Path, cookie_secure: bool = False
    ) -> None:
        self.meili_url = meili_url
        self.media_root = media_root
        self.cookie_secure = cookie_secure

    @classmethod
    def from_env(cls) -> AppConfig:
        return cls(
            meili_url=os.environ.get("KB_MEILI_URL", "http://127.0.0.1:7700"),
            media_root=Path(
                os.environ.get("KB_MEDIA_ROOT", str(Path.home() / "media"))
            ),
            # 本机 http://127.0.0.1 调试时带 Secure 的 Cookie 不会被回传，故默认关
            cookie_secure=os.environ.get("KB_COOKIE_SECURE", "").lower()
            in {"1", "true", "yes"},
        )


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _json_body(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        raise ValueError("请求体必须是 JSON")
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return payload


def _validate_credentials(payload: dict) -> tuple[str, str]:
    username = payload.get("username")
    password = payload.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise ValueError("username 与 password 都必须是字符串")
    username = username.strip()
    if not username or len(username) > MAX_USERNAME_LEN:
        raise ValueError(f"账号长度需在 1–{MAX_USERNAME_LEN} 之间")
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"密码至少 {MIN_PASSWORD_LEN} 位")
    if auth.password_too_long(password):
        raise ValueError(f"密码超过 {auth.MAX_PASSWORD_BYTES} 字节")
    return username, password


def create_app(pool: Pool, config: AppConfig | None = None) -> Starlette:
    cfg = config or AppConfig.from_env()

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        # 启动时清一次过期会话：单用户不会有回收压力，顺手做掉
        try:
            with pool.connection() as conn:
                accounts.purge_expired(conn)
        except Exception:
            pass  # 库还没就绪时不该阻止服务起来，健康检查会如实反映
        yield
        pool.close_all()

    async def login_page(request: Request) -> Response:
        if getattr(request.state, "username", None):
            return RedirectResponse("/", status_code=303)
        try:
            with pool.connection() as conn:
                needs_bootstrap = accounts.count_users(conn) == 0
        except Exception:
            # 库暂时不可用时仍要能打开登录页，让用户看到界面而不是 500
            needs_bootstrap = False
        return HTMLResponse(pages.login_page(bootstrap=needs_bootstrap))

    async def home(request: Request) -> Response:
        username = getattr(request.state, "username", "")
        report = health.check(pool, cfg.meili_url, cfg.media_root)
        return HTMLResponse(pages.home_page(username, report))

    async def api_health(_request: Request) -> Response:
        return JSONResponse(health.check(pool, cfg.meili_url, cfg.media_root))

    async def api_bootstrap(request: Request) -> Response:
        try:
            payload = await _json_body(request)
            username, password = _validate_credentials(payload)
        except ValueError as exc:
            return _error(str(exc))
        with pool.connection() as conn:
            if accounts.count_users(conn) > 0:
                return _error("已有账号，bootstrap 已永久关闭", 410)
            accounts.create_user(conn, username, auth.hash_password(password))
        return JSONResponse({"ok": True, "username": username}, status_code=201)

    async def api_login(request: Request) -> Response:
        try:
            payload = await _json_body(request)
            username, password = _validate_credentials(payload)
        except ValueError as exc:
            return _error(str(exc))
        with pool.connection() as conn:
            user = accounts.find_user(conn, username)
            # 账号不存在也走一次哈希比对，避免用响应时间探测账号是否存在
            stored = user[2] if user else auth.hash_password("不存在的账号占位")
            if not auth.verify_password(password, stored) or user is None:
                return _error("账号或密码不对", 401)
            raw, hashed = auth.new_session_token()
            accounts.create_session(conn, hashed)
        response = JSONResponse({"ok": True, "username": user[1]})
        response.headers["Set-Cookie"] = auth.build_set_cookie(raw, cfg.cookie_secure)
        return response

    async def api_logout(request: Request) -> Response:
        raw = auth.token_from_cookie_header(request.headers.get("cookie"))
        if raw:
            with pool.connection() as conn:
                accounts.delete_session(conn, auth.hash_token(raw))
        response = JSONResponse({"ok": True})
        response.headers["Set-Cookie"] = auth.build_clear_cookie(cfg.cookie_secure)
        return response

    class SessionMiddleware(BaseHTTPMiddleware):
        async def dispatch(
            self, request: Request, call_next: RequestResponseEndpoint
        ) -> Response:
            request.state.username = None
            if request.url.path in PUBLIC_PATHS:
                return await call_next(request)
            raw = auth.token_from_cookie_header(request.headers.get("cookie"))
            if raw:
                try:
                    with pool.connection() as conn:
                        session = accounts.find_session(conn, auth.hash_token(raw))
                        if session:
                            accounts.touch_session(conn, session[0], session[1])
                            request.state.username = _username_of(conn, session[0])
                except Exception:
                    pass  # 库出问题时按未登录处理，由健康检查暴露
            if request.state.username:
                return await call_next(request)
            if request.url.path.startswith("/api/"):
                return _error("未登录", 401)
            return RedirectResponse("/login", status_code=303)

    routes = [
        Route("/", home),
        Route("/login", login_page),
        Route("/api/health", api_health),
        Route("/api/auth/bootstrap", api_bootstrap, methods=["POST"]),
        Route("/api/auth/login", api_login, methods=["POST"]),
        Route("/api/auth/logout", api_logout, methods=["POST"]),
    ]
    return Starlette(
        routes=routes, middleware=[Middleware(SessionMiddleware)], lifespan=lifespan
    )


def _username_of(conn: pg8000.native.Connection, session_id: int) -> str:
    """会话表里没有 username（单用户），直接取库里唯一那个账号。"""
    rows = conn.run("select username from users order by id limit 1")
    return str(rows[0][0]) if rows else ""

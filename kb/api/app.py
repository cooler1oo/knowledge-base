"""Starlette 应用：路由 + 鉴权中间件。

已实现（§8.1 端点表的 1/2/3/4/5/6/28 号）：bootstrap / login / logout /
items 三个 / health，加登录页与首页两个页面。
其余端点在后续轮次，这里不占位、不留 `pass`——没实现的路径就该 404。

鉴权口径：除了白名单，一律要求有效 Cookie 会话。
`/api/*` 未登录返回 401 JSON，页面路径未登录 303 跳登录页。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from kb.store import accounts
from kb.store import items as items_store
from kb.store.db import Pool

from ..media import persist as media_persist
from . import ai as ai_api
from . import auth, health, pages
from . import items as items_api
from . import media as media_api
from . import search as search_api

PUBLIC_PATHS = frozenset(
    {"/login", "/api/auth/login", "/api/auth/bootstrap", "/api/health", "/favicon.ico"}
)

MIN_PASSWORD_LEN = 8
MAX_USERNAME_LEN = 64


class AppConfig:
    def __init__(
        self,
        meili_url: str,
        media_root: Path,
        cookie_secure: bool = False,
        meili_key: str = "",
    ) -> None:
        self.meili_url = meili_url
        self.media_root = media_root
        self.cookie_secure = cookie_secure
        # 检索密钥与 worker 读同一个环境变量名（设计 §10「环境变量」）。
        # 传给 `/api/search` 的 handler 而不是留在全局：搜索路径要带
        # `Authorization` 头，而 `/api/health` 的探针不需要（它只读 /health）。
        self.meili_key = meili_key

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
            meili_key=os.environ.get("KB_MEILI_KEY", ""),
        )


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _json_body(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 启动清理/登录页/会话查找按降级处理，见各分支注释
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
        except Exception:  # noqa: BLE001 启动清理/登录页/会话查找按降级处理，见各分支注释
            pass  # 库还没就绪时不该阻止服务起来，健康检查会如实反映
        # 清 `.incoming/` 里上次被 kill 时留下的 `.part`：它们与正式文件同目录，
        # `stats()` 看不到，会悄悄吃掉磁盘。清理失败不阻止服务起来。
        try:
            media_persist.clean_incoming(cfg.media_root)
        except Exception:  # noqa: BLE001 启动清理失败不影响服务可用性
            pass
        yield
        pool.close_all()

    async def login_page(request: Request) -> Response:
        if getattr(request.state, "username", None):
            return RedirectResponse("/", status_code=303)
        try:
            with pool.connection() as conn:
                needs_bootstrap = accounts.count_users(conn) == 0
        except Exception:  # noqa: BLE001 启动清理/登录页/会话查找按降级处理，见各分支注释
            # 库暂时不可用时仍要能打开登录页，让用户看到界面而不是 500
            needs_bootstrap = False
        return HTMLResponse(pages.login_page(bootstrap=needs_bootstrap))

    async def home(request: Request) -> Response:
        username = getattr(request.state, "username", "")
        report = health.check(pool, cfg.meili_url, cfg.media_root)
        try:
            with pool.connection() as conn:
                recent = items_store.list_items(conn, limit=20, before_id=None)
        except Exception:  # noqa: BLE001 库没起来时页面仍要能打开，问题由健康检查如实反映
            recent = []
        return HTMLResponse(pages.home_page(username, report, recent))

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
                            request.state.username = accounts.first_username(conn)
                except Exception:  # noqa: BLE001 启动清理/登录页/会话查找按降级处理，见各分支注释
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
        # AI 用量（D15/D18）。**刻意不加入 PUBLIC_PATHS**：不在白名单里即自动
        # 走会话中间件，未登录返回 401 而不是 200 空对象——用量属敏感信息。
        Route("/api/ai/usage", ai_api.usage(pool)),
        Route("/api/items", items_api.create_or_list(pool), methods=["POST", "GET"]),
        Route("/api/items/{item_id:int}", items_api.detail(pool)),
        # M3-08 T04：修正类型并记住。**PATCH 单独注册一条路由**而不是给
        # detail 那条加 methods：`detail` 是纯读、不读 body，把写混进去
        # 会让「GET 与 PATCH 的行为差异」藏在一个 handler 的两个分支里。
        # 本轮只支持改 kind（完整编辑界面留给 M5-07）。
        Route(
            "/api/items/{item_id:int}",
            items_api.edit(pool),
            methods=["PATCH"],
        ),
        # 检索（E-18 / E-20 / M6-01…M6-03、M6-05）。
        # **刻意不加入 PUBLIC_PATHS**：搜索会返回库里的标题、摘要与标签，
        # 未登录请求不该看到这些内容——不在白名单里即自动走会话中间件，401。
        #
        # `meili_url` / `meili_key` 从 `cfg` 传进去，不让 handler 自己读环境变量：
        # 那样 `AppConfig.from_env` 会变成「读了一份但没人用」，
        # 「部署时没配 KB_MEILI_KEY」就只能在真机上以 401 的形式暴露
        # （2026-11-28 真机踩过：api 进程没有 key，搜索全部降级成 fallback）。
        Route(
            "/api/search",
            search_api.search(pool, meili_url=cfg.meili_url, meili_key=cfg.meili_key),
        ),
        # 媒体读取（M2-03 / M7-01）。**路由必须写 `{key:path}`**：`storage_key`
        # 按定义带一个 `/`（`ab/cdef….jpg`），写成 `{key}` 的正则是
        # `^/api/media/(?P<key>[^/]+)$`，永远匹配不上——现象是「所有媒体都 404」，
        # 容易误判成数据库没数据（Starlette 1.10.0 实测）。
        # **刻意不加入 PUBLIC_PATHS**：同源 `<img>` 会自动带 Cookie，未登录 401。
        Route("/api/media/{key:path}", media_api.file_response(pool, cfg.media_root)),
        Route("/api/auth/bootstrap", api_bootstrap, methods=["POST"]),
        Route("/api/auth/login", api_login, methods=["POST"]),
        Route("/api/auth/logout", api_logout, methods=["POST"]),
    ]
    return Starlette(
        routes=routes, middleware=[Middleware(SessionMiddleware)], lifespan=lifespan
    )

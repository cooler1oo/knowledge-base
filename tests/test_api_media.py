"""`GET /api/media/{key}`：200 / 404 / 路径穿越 / Cache-Control / 鉴权。

**四步顺序一条不许挪**（设计 §3.10）：

1. `is_valid_key(key)` 不通过 → 404（不是 400）
2. `mime_for_key` 查不到 → 404
3. 路径越出 `media_root` → 404
4. `FileResponse` + `Cache-Control`

## 为什么格式校验要先于查库

先查库会让「格式对但库里没有」与「格式就不对」通过时序差异被区分出来，
端点就成了一个存在性探针。先判格式则两种拒绝完全同形（同样 404）。

## 为什么路由必须写 `{key:path}`

实测 Starlette 1.10.0：

```
Route("/api/media/{key}")       → ^/api/media/(?P<key>[^/]+)$   # key 里的 / 永远匹配不上
Route("/api/media/{key:path}")  → ^/api/media/(?P<key>.*)$
```

而 `storage_key` 按定义带一个 `/`。写成 `{key}` 的现象是「所有媒体都 404」。
本文件有一条用例专门钉住路由形状。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from kb.api import media as media_api

KEY = "ab/" + "c" * 32 + ".jpg"
BODY = b"\xff\xd8\xff\xe0 fake jpeg bytes for the endpoint test"


class _Conn:
    """假连接：只实现 `mime_for_key` 需要的那条读。"""

    def __init__(self, table: dict[str, str]) -> None:
        self._table = table
        self.seen: list[str] = []

    def run(self, sql: str, **params: object) -> list[list[object]]:
        self.seen.append(" ".join(sql.split()).lower())
        key = str(params.get("k", ""))
        return [[self._table[key]]] if key in self._table else []


class _Pool:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    def connection(self) -> object:
        @contextmanager
        def _cm() -> Iterator[object]:
            yield self._conn

        return _cm()


class _Auth(BaseHTTPMiddleware):
    """模拟 `app.py` 的 `SessionMiddleware`：不在白名单且未登录就 401。

    用迷你中间件而不是真实 `create_app`：后者牵扯数据库、bcrypt 与登录页，
    会把「状态码对不对」这件事埋在一堆无关依赖里。
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if self._logged_in:
            return await call_next(request)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "未登录"}, status_code=401)
        return Response(status_code=303)

    def __init__(self, app: object, *, logged_in: bool = True) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._logged_in = logged_in


def _client(
    media_root: Path, *, table: dict[str, str] | None = None, logged_in: bool = True
) -> TestClient:
    conn = _Conn(table if table is not None else {KEY: "image/jpeg"})
    handler = media_api.file_response(_Pool(conn), media_root)
    app = Starlette(
        routes=[Route("/api/media/{key:path}", handler)],
        middleware=[Middleware(_Auth, logged_in=logged_in)],
    )
    client = TestClient(app)
    client.conn = conn  # type: ignore[attr-defined]
    return client


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    """在 `tmp_path` 里放一个真实的媒体文件。"""
    path = tmp_path / KEY
    path.parent.mkdir(parents=True)
    path.write_bytes(BODY)
    return tmp_path


# ── 200 路径 ────────────────────────────────────────────────


def test_存在时返回200与正确ContentType(media_root: Path):
    response = _client(media_root).get(f"/api/media/{KEY}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == BODY


def test_ContentType来自数据库而不是文件后缀(media_root: Path):
    """数据库说 `image/png`，文件后缀是 `.jpg` —— 必须以数据库为准。

    取后缀的后果：一条被人手工塞进来的 `x.html` 会以 `text/html` 返回，
    以我们的源站身份执行脚本。这就是「扩展名只是给人看的」的实际含义。
    """
    response = _client(media_root, table={KEY: "image/png"}).get(f"/api/media/{KEY}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_CacheControl是一年immutable(media_root: Path):
    """`storage_key` 由内容哈希派生，同一 key 的字节永不改变。

    所以可以放心给一年 + `immutable`，浏览器连条件请求都不用发。
    `private` 是必须的：这是私人知识库，不允许任何中间缓存留存。
    """
    response = _client(media_root).get(f"/api/media/{KEY}")

    cache = response.headers["cache-control"]
    assert "max-age=31536000" in cache
    assert "immutable" in cache
    assert "private" in cache, "私人内容不许被共享缓存留存"


def test_支持Range请求(media_root: Path):
    """Starlette 1.10 的 `FileResponse` 自带 Range 支持——将来真播视频时
    拖动进度条要用它。这条只断言机制在，不验字节范围细节。
    """
    response = _client(media_root).get(
        f"/api/media/{KEY}", headers={"Range": "bytes=0-9"}
    )
    assert response.status_code == 206
    assert len(response.content) == 10


# ── 404：三种拒绝必须完全同形 ───────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "abc",  # 格式完全不对
        "ab/" + "c" * 31 + ".jpg",  # 文件名 31 位
        "AB/" + "c" * 32 + ".jpg",  # 大写目录
        "ab/" + "C" * 32 + ".jpg",  # 大写文件名
        "ab/" + "c" * 32 + ".tar.gz",  # 两段扩展名
        "ab/" + "c" * 32,  # 没有扩展名
    ],
)
def test_格式不合法的key返回404而不是400(media_root: Path, key: str):
    """**404 而不是 400。** 400 会告诉探测者「这个 key 形状对但内容不存在」，
    等于把端点变成存在性探针。
    """
    response = _client(media_root).get(f"/api/media/{key}")
    assert response.status_code == 404
    assert response.content == b"" or response.headers["content-type"] != "image/jpeg"


def test_路径穿越被拦截(media_root: Path):
    """`../` 里的点不属于 `[0-9a-f]`，正则过不去。文件系统那一层是兜底。"""
    secret = media_root.parent / "secret.jpg"
    secret.write_bytes(b"must not leak")

    for attack in (
        "../secret.jpg",
        "../../etc/passwd",
        "ab/../../secret.jpg",
        "ab/" + "c" * 32 + ".jpg/../../secret.jpg",
    ):
        response = _client(media_root).get(f"/api/media/{attack}")
        assert response.status_code == 404, f"穿越成功了：{attack}"
        assert b"must not leak" not in response.content


def test_绝对路径被拦截(media_root: Path):
    response = _client(media_root).get("/api/media//etc/passwd")
    assert response.status_code == 404


def test_库里有记录但文件不在返回404(media_root: Path):
    """孤儿记录（磁盘被清、备份恢复不完整）。

    返回 404 而不是 500：对浏览器来说两者都是「这张图没了」，
    而 500 会让前端以为服务端故障并触发整体错误页。
    """
    ghost = "aa/" + "b" * 32 + ".jpg"
    response = _client(media_root, table={ghost: "image/jpeg"}).get(
        f"/api/media/{ghost}"
    )
    assert response.status_code == 404


def test_库里没有记录的合法key返回404(media_root: Path):
    other = "zz/" + "9" * 32 + ".jpg"
    response = _client(media_root, table={}).get(f"/api/media/{other}")
    assert response.status_code == 404


def test_mime为空串时返回404(media_root: Path):
    """`mime_for_key` 返回空串与 `None` 都必须当「没有」。

    空串会让 `FileResponse` 拿不到 media_type，Starlette 按扩展名猜——
    于是「库里没有这条记录」被伪装成「有记录但类型不明」。
    """
    response = _client(media_root, table={KEY: ""}).get(f"/api/media/{KEY}")
    assert response.status_code == 404


def test_三种404的响应体完全一致(media_root: Path):
    """格式错 / 库里没有 / 文件不在 —— 三者对外的响应必须无差别。

    有差别就意味着端点可以被用来探测「哪个 key 曾经存在过」。
    """
    client = _client(media_root, table={KEY: "image/jpeg"})
    bad_format = client.get("/api/media/abc")
    no_record = client.get("/api/media/zz/" + "9" * 32 + ".jpg")
    ghost_record = client.get("/api/media/aa/" + "b" * 32 + ".jpg")

    for name, response in (
        ("bad_format", bad_format),
        ("no_record", no_record),
        ("ghost_record", ghost_record),
    ):
        assert response.status_code == 404, name
        assert response.content == b"", f"{name} 的响应体泄露了信息"


# ── 鉴权 ────────────────────────────────────────────────────


def test_未登录返回401(media_root: Path):
    """同源 `<img>` 会自动带 Cookie，`SameSite=Lax` 对同源 GET 图片请求正常发送。

    `PUBLIC_PATHS` 里没有 `/api/media`，所以请求会进入 `SessionMiddleware`，
    未登录时走到 `startswith("/api/")` 分支返回 401。**不需要写任何新代码。**
    """
    response = _client(media_root, logged_in=False).get(f"/api/media/{KEY}")
    assert response.status_code == 401
    assert response.content != BODY, "未登录拿到了图片字节"


def test_media路径不在PUBLIC_PATHS里():
    """这条是**规格断言**：把 `/api/media` 加进白名单等于把私人图片公开。

    在白名单里则中间件直接放行、请求不经过会话校验。
    """
    from kb.api.app import PUBLIC_PATHS

    assert "/api/media" not in PUBLIC_PATHS
    assert not any(p.startswith("/api/media") for p in PUBLIC_PATHS)


# ── 路由形状 ────────────────────────────────────────────────


def test_路由必须用key_path否则带斜杠的key永远匹配不上():
    """**实测 Starlette 1.10.0**：

    ```
    Route("/api/media/{key}")       → ^/api/media/(?P<key>[^/]+)$
    Route("/api/media/{key:path}")  → ^/api/media/(?P<key>.*)$
    ```

    `storage_key` 按定义带一个 `/`。写成 `{key}` 的现象是「所有媒体都 404」，
    排查时容易误判成数据库没数据。
    """
    import inspect

    from kb.api import app as app_module

    source = inspect.getsource(app_module.create_app)
    assert "/api/media/{key:path}" in source, (
        "路由写成了 {key}，带斜杠的 key 永远匹配不上"
    )


def test_路由注册在两个items路由之后():
    """顺序不是硬要求（两条路径不重叠），但放在 items 之后让路由表读起来
    是按资源类型分组的。这条只钉住「它确实被注册了」。
    """
    import inspect

    from kb.api import app as app_module

    source = inspect.getsource(app_module.create_app)
    assert "media_api.file_response" in source
    assert "cfg.media_root" in source, "media_root 没被注入 handler"


def test_真实app能构造出来且带上媒体路由(tmp_path: Path):
    """端到端：`create_app` 的 Starlette 实例里真的有一条 `/api/media` 路由。

    前面的用例用迷你 app 验 handler 本身；这条验「路由被接进去了」——
    两件事分开，否则「路由漏注册」会被 handler 用例的绿灯掩盖。
    """
    from kb.api.app import AppConfig, create_app

    class _EmptyPool:
        def connection(self) -> object:  # pragma: no cover - 构造期不取连接
            raise AssertionError("构造应用不该取连接")

        def close_all(self) -> None:  # pragma: no cover - 同上
            pass

    app = create_app(
        _EmptyPool(),  # type: ignore[arg-type]
        AppConfig(meili_url="http://127.0.0.1:7700", media_root=tmp_path),
    )
    paths = [getattr(route, "path", "") for route in app.routes]
    assert "/api/media/{key:path}" in paths, f"媒体路由没注册：{paths}"

"""`GET /api/media/{key}`：把磁盘上的附件字节读回来。

## 四步，顺序一条不许挪

1. `layout.is_valid_key(key)` 不通过 → **404**（不是 400，避免成为存在性探针）
2. `attachments.mime_for_key` 查不到 → 404
3. 拼路径并确认仍在 `media_root` 之内 → 不在则 404
4. `FileResponse(path, media_type=mime)` + `Cache-Control`

## 为什么格式校验要先于查库

先查库会让「格式对但库里没有」与「格式就不对」都能通过时序差异被区分出来，
端点就成了一个存在性探针。先判格式则两种拒绝完全同形（同样 404、同样耗时）。

## 路由必须写 `{key:path}`，不能写 `{key}`

实测 Starlette 1.10.0 两条路由的正则：

```
Route("/api/media/{key}")       → ^/api/media/(?P<key>[^/]+)$    # key 里的 / 永远匹配不上
Route("/api/media/{key:path}")  → ^/api/media/(?P<key>.*)$
```

而 `storage_key` 按定义带一个 `/`。写成 `{key}` 的现象是「所有媒体都 404」，
排查时容易误判成数据库没数据。

## 鉴权不需要写新代码

`<img src>` 发不出自定义请求头，但**同源的 `<img>` 会带 Cookie**。`kb/api/auth.py` 的
`build_set_cookie` 是 `HttpOnly; SameSite=Lax; Path=/`，`SameSite=Lax` 对同源 GET
图片请求正常发送。`PUBLIC_PATHS` 里没有 `/api/media`，所以请求会进入
`SessionMiddleware`，未登录走到 `startswith("/api/")` 分支返回 401。
**只要不把这条路径加进白名单，鉴权就是已经具备的。**

## Cache-Control 为什么是 `immutable`

`storage_key` 由内容哈希派生，同一 key 的字节永不改变（内容变了 key 就变了）。
所以可以放心给一年 + `immutable`，浏览器连条件请求都不用发。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, Response

from kb.store import attachments as attachments_store
from kb.store.db import Pool

from ..media import layout

Handler = Callable[[Request], Awaitable[Response]]

#: 内容不可变（key 由内容哈希派生），所以给一年。
CACHE_CONTROL = "private, max-age=31536000, immutable"


def _not_found() -> Response:
    """404 的**唯一**出口。三种拒绝共用它，保证响应形状不泄露拒绝原因。

    不用 `JSONResponse`：`<img>` 拿到 JSON 会在控制台报解析错误，
    而一个空的 404 与「图片不存在」的语义完全一致。
    """
    return Response(status_code=404)


def file_response(pool: Pool, media_root: Path) -> Handler:
    """handler 工厂。与 `kb/api/items.py` 同构（闭包捕获 `pool` 与 `media_root`）。"""

    async def handler(request: Request) -> Response:
        key = str(request.path_params.get("key") or "")

        # ① 格式白名单。正则本身就是路径穿越的充分防线（`..` 里的点不属于
        #    `[0-9a-f]`，`/` 只能出现在第 3 个字符位）。
        if not layout.is_valid_key(key):
            return _not_found()

        with pool.connection() as conn:
            mime = attachments_store.mime_for_key(conn, key)
        # ② 查不到 → 404。**这里必须用数据库的 mime 而不是按后缀猜**：
        #    后缀只由 mime 映射得出，但「按后缀猜」会让一条被人手工塞进来的
        #    `x.html` 以 text/html 返回。
        if not mime:
            return _not_found()

        # ③ 文件系统兜底。即使将来有人放宽了正则，这一层仍然拦得住。
        root = media_root.resolve()
        path = (media_root / key).resolve()
        if not path.is_relative_to(root):
            return _not_found()
        if not path.is_file():
            # 有记录但文件没了（孤儿记录：磁盘被清、备份恢复不完整）。
            # 返回 404 而不是 500：对浏览器来说两者都是「这张图没了」，
            # 而 500 会让前端以为服务端故障并触发整体错误页。
            return _not_found()

        return FileResponse(
            path,
            media_type=mime,
            # ④ 一年的 immutable。key 由内容哈希派生，同 key 字节永不改变。
            headers={"Cache-Control": CACHE_CONTROL},
        )

    return handler

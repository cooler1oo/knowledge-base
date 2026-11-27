"""`/api/items` 三个端点的 handler 工厂。

为什么是工厂函数：`pool` 要在启动时注入，而 Starlette 的路由表是模块级构造的。
闭包捕获 `pool` 与 `app.py` 现有写法一致，不需要引入依赖注入框架。

这一层只做三件事：解析请求、校验、把结果序列化。SQL 全在 `kb/store/`——
架构检查会在提交时拦住任何写在 api 层的写库语句。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from kb.store import items as items_store
from kb.store.db import Pool

Handler = Callable[[Request], Awaitable[Response]]

MAX_URL_LEN = 2048
MAX_TEXT_LEN = 20000
DEFAULT_LIMIT = 30
MAX_LIMIT = 100


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _json_body(request: Request) -> dict[str, Any]:
    """请求体必须是 JSON 对象。非 JSON 与非对象都归为 400，不是 500。"""
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 解析失败一律当「请求体不合法」，原始异常对调用方无意义
        raise ValueError("请求体必须是 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return payload


def _optional_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是字符串")
    return value


@dataclass(frozen=True)
class _NewItem:
    kind: str
    url: str | None
    url_norm: str | None
    title: str
    source_domain: str
    content_text: str | None
    step: str


def _parse_create(payload: dict[str, Any]) -> _NewItem:
    """把请求体收成一个待入库的结构。校验不通过抛 ValueError（由调用方转 400）。"""
    url = _optional_str(payload, "url").strip()
    text = _optional_str(payload, "text")
    raw_text = _optional_str(payload, "raw_text")

    if not url and not text.strip():
        raise ValueError("url 与 text 至少给一个")
    if url:
        if not items_store.is_http_url(url):
            raise ValueError("url 必须是以 http:// 或 https:// 开头的网址")
        if len(url) > MAX_URL_LEN:
            raise ValueError(f"url 太长（上限 {MAX_URL_LEN} 字符）")
    if len(text) > MAX_TEXT_LEN:
        raise ValueError(f"text 太长（上限 {MAX_TEXT_LEN} 字符）")
    if len(raw_text) > MAX_TEXT_LEN:
        raise ValueError(f"raw_text 太长（上限 {MAX_TEXT_LEN} 字符）")

    if url:
        # 有链接就没有可抓的正文；标题先取分享文案首行，抓取层（R3）回来后会覆盖
        title = items_store.title_from_share_text(raw_text) if raw_text else ""
        return _NewItem(
            kind="link",
            url=url,
            url_norm=items_store.normalize_url(url),
            title=title,
            source_domain=items_store.host_of(url),
            content_text=raw_text or None,
            step="fetch",
        )

    return _NewItem(
        kind="text",
        url=None,
        url_norm=None,
        title=items_store.title_from_text(text),
        source_domain="",
        content_text=text,
        step="summarize",
    )


def _json_item(item: items_store.ItemSummary) -> dict[str, object]:
    return {
        "id": item.id,
        "kind": item.kind,
        "url": item.url,
        "title": item.title,
        "sourceDomain": item.source_domain,
        "savedAt": item.saved_at.isoformat(),
        "status": item.status,
        "readStatus": item.read_status,
        "summary": item.summary,
    }


def _int_param(raw: str | None, name: str) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc


async def _create(request: Request, pool: Pool) -> Response:
    try:
        payload = await _json_body(request)
        parsed = _parse_create(payload)
    except ValueError as exc:
        return _error(str(exc))

    with pool.connection() as conn:
        result = items_store.create_item(
            conn,
            kind=parsed.kind,
            url=parsed.url,
            url_norm=parsed.url_norm,
            title=parsed.title,
            source_domain=parsed.source_domain,
            content_text=parsed.content_text,
            step=parsed.step,
        )

    body: dict[str, object] = {
        "id": result.id,
        "kind": result.kind,
        "status": result.status,
    }
    if result.duplicate:
        body["duplicate"] = True
        return JSONResponse(body, status_code=200)
    return JSONResponse(body, status_code=202)


def create_or_list(pool: Pool) -> Handler:
    async def handler(request: Request) -> Response:
        if request.method == "POST":
            return await _create(request, pool)

        try:
            raw_limit = _int_param(request.query_params.get("limit"), "limit")
            before = _int_param(request.query_params.get("before"), "before")
        except ValueError as exc:
            return _error(str(exc))

        # 超出范围夹到边界而不是报错：翻页时前端传 200 不该变成一次失败
        limit = (
            DEFAULT_LIMIT if raw_limit is None else max(1, min(raw_limit, MAX_LIMIT))
        )
        if before is not None and before <= 0:
            return _error("before 必须是正整数")

        with pool.connection() as conn:
            rows = items_store.list_items(conn, limit=limit, before_id=before)

        payload = {
            "items": [_json_item(item) for item in rows],
            "nextBefore": rows[-1].id if rows and len(rows) == limit else None,
        }
        return JSONResponse(payload)

    return handler


def detail(pool: Pool) -> Handler:
    async def handler(request: Request) -> Response:
        try:
            item_id = int(str(request.path_params.get("item_id")))
        except (TypeError, ValueError):
            return _error("id 必须是整数")

        with pool.connection() as conn:
            item = items_store.get_item(conn, item_id)
            if item is None:
                return _error("没有这条", 404)
            job_rows = items_store.item_jobs(conn, item_id)

        return JSONResponse(
            {
                "id": item.id,
                "kind": item.kind,
                "url": item.url,
                "title": item.title,
                "author": item.author,
                "sourceDomain": item.source_domain,
                "summary": item.summary,
                "contentText": item.content_text,
                "readStatus": item.read_status,
                "status": item.status,
                "savedAt": item.saved_at.isoformat(),
                "publishedAt": (
                    None if item.published_at is None else item.published_at.isoformat()
                ),
                "props": item.props,
                "jobs": job_rows,
            }
        )

    return handler

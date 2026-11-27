"""R4 纯逻辑单测：URL 归一化、标题抽取、状态聚合、三个端点的 HTTP 层。

不连库、不起服务。假连接按 SQL 片段分派返回值——`conn.run` 的入参是字符串，
用「这段 SQL 里有哪个唯一标记」判断是哪一次查询，比给每个函数打桩更接近真实调用。

真链路由 `tests/test_items_pg.py` 在平板上跑（连不上自动 skip）。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from kb.api.app import AppConfig, create_app
from kb.store.items import (
    DONE,
    FAILED,
    NO_JOB,
    PENDING,
    RUNNING,
    derive_status,
    is_http_url,
    normalize_url,
    title_from_share_text,
    title_from_text,
)

# ── URL 归一化（技术方案 §6.6）────────────────────────────────


def test_归一化_大小写与查询顺序():
    assert (
        normalize_url("HTTPS://Example.COM/a/?b=2&a=1")
        == "https://example.com/a?a=1&b=2"
    )


def test_归一化_去尾部斜杠与保留片段():
    assert normalize_url("https://a.com/x///") == "https://a.com/x"
    assert normalize_url("https://a.com/x#frag") == "https://a.com/x#frag"


def test_归一化_保留端口():
    assert normalize_url("https://A.com:8443/x/") == "https://a.com:8443/x"


def test_归一化_空串与空白():
    assert normalize_url("") == ""
    assert normalize_url("   ") == ""


def test_归一化_短链与长链不相等():
    """这一条是「归一化能力边界」的说明性断言：短链与长链只能靠 canonical_id 归并，
    不是 url_norm 的职责（技术方案 §6.6 两级去重键）。"""
    assert normalize_url("https://v.douyin.com/abc/") != normalize_url(
        "https://www.douyin.com/video/123"
    )


def test_只认_http_与_https():
    assert is_http_url("http://a.com")
    assert is_http_url("https://a.com/x")
    assert not is_http_url("ftp://a.com/x")
    assert not is_http_url("javascript:alert(1)")
    assert not is_http_url("v.douyin.com/x")
    assert not is_http_url("")


# ── 标题 ──────────────────────────────────────────────────────


def test_标题取首个非空行并压空白():
    assert title_from_text("\n\n  第一行   有   空格 \n第二行") == "第一行 有 空格"


def test_标题超长截断():
    assert len(title_from_text("啊" * 300)) == 120


def test_标题空文本():
    assert title_from_text("   \n  ") == ""


def test_分享文案里的标题要把链接抠掉():
    raw = "7.68 复制打开抖音，看看【老张】的作品 https://v.douyin.com/abc/"
    title = title_from_share_text(raw)
    assert "http" not in title
    assert "老张" in title
    assert len(title) <= 120


# ── 处理状态聚合 ───────────────────────────────────────────────


def test_状态_running_最优先():
    assert derive_status(1, 1, 0, 2) == RUNNING


def test_状态_pending_次之():
    assert derive_status(1, 0, 1, 2) == PENDING


def test_状态_failed_再次():
    assert derive_status(0, 0, 1, 1) == FAILED


def test_状态_全完成():
    assert derive_status(0, 0, 0, 3) == DONE


def test_状态_没有任务():
    assert derive_status(0, 0, 0, 0) == NO_JOB


# ── HTTP 层：手写最小 ASGI 驱动（不引 httpx）────────────────────
#
# 假连接按 SQL 片段分派。分派顺序有意从「最具体」到「最宽泛」：
# 列表 SQL 里也含 `from jobs`（子查询聚合），若先匹配 `from jobs` 会把列表查成计数。

_SESSION_ROW = [1, datetime.now(timezone.utc) + timedelta(days=30)]


class _Script:
    """按需注入的返回值。默认全部空，哪个用例要什么就改什么。"""

    def __init__(self) -> None:
        self.create: list[list[object]] = [[7, "link"]]
        self.existing: list[list[object]] = []
        self.list_rows: list[list[object]] = []
        self.detail_rows: list[list[object]] = []
        self.job_counts: list[list[object]] = [[0, 0, 0, 0]]
        self.job_rows: list[list[object]] = []


class _Conn:
    def __init__(self, script: _Script) -> None:
        self._script = script
        self.seen: list[str] = []

    def run(self, sql: str, **_params: object) -> list[list[object]]:
        text = sql.lower()
        self.seen.append(text)
        if "with new_item" in text:
            return self._script.create
        if "from items where url_norm" in text:
            return self._script.existing
        if "i.content_text" in text:
            return self._script.detail_rows
        if "left join lateral" in text:
            return self._script.list_rows
        if "from jobs" in text and "order by id" in text:
            return self._script.job_rows
        if "from jobs" in text:
            return self._script.job_counts
        if "from sessions" in text:
            return [_SESSION_ROW]
        if "from users" in text:
            return [["testuser"]]
        if "from settings" in text:
            return []
        if "select 1" in text:
            return [[1]]
        return []

    def close(self) -> None:
        return None


class _Pool:
    def __init__(self, script: _Script) -> None:
        self._script = script

    @contextmanager
    def connection(self):  # type: ignore[no-untyped-def]
        yield _Conn(self._script)

    def close_all(self) -> None:
        return None


def _call(
    script: _Script,
    path: str,
    method: str = "GET",
    body: object = None,
    cookie: str | None = "kb_session=testtoken",
) -> dict[str, Any]:
    """直接驱动 ASGI，拿到 (状态码, 响应体)。cookie 默认已登录。"""
    app = create_app(_Pool(script), AppConfig("http://127.0.0.1:7700", Path(".")))

    async def go() -> dict[str, Any]:
        raw = json.dumps(body).encode() if body is not None else b""
        headers = [(b"cookie", cookie.encode())] if cookie else []
        path_only, _, query = path.partition("?")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path_only,
            "raw_path": path_only.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": headers,
            "client": ("t", 1),
            "server": ("t", 80),
        }
        state = {"sent": False}

        async def receive() -> dict[str, Any]:
            if state["sent"]:
                return {"type": "http.disconnect"}
            state["sent"] = True
            return {"type": "http.request", "body": raw, "more_body": False}

        out: dict[str, Any] = {"status": 0, "body": b""}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                out["status"] = message["status"]
            elif message["type"] == "http.response.body":
                out["body"] += message.get("body", b"")

        await app(scope, receive, send)
        return out

    result = asyncio.run(go())
    return {
        "status": result["status"],
        "text": result["body"].decode("utf-8", "replace"),
    }


def _json(result: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(result["text"])
    assert isinstance(payload, dict)
    return payload


# ── POST /api/items ───────────────────────────────────────────


def test_未登录不能提交():
    r = _call(_Script(), "/api/items", "POST", {"url": "https://a.com/x"}, cookie=None)
    assert r["status"] == 401


def test_空请求体被拒且说明原因():
    r = _call(_Script(), "/api/items", "POST", {})
    assert r["status"] == 400
    assert "至少给一个" in _json(r)["error"]


def test_非法协议被拒():
    r = _call(_Script(), "/api/items", "POST", {"url": "ftp://a.com/x"})
    assert r["status"] == 400
    assert "http" in _json(r)["error"]


def test_新建链接返回202与待处理():
    r = _call(_Script(), "/api/items", "POST", {"url": "https://v.douyin.com/abc/"})
    assert r["status"] == 202
    body = _json(r)
    assert body["id"] == 7
    assert body["status"] == PENDING
    assert "duplicate" not in body


def test_重复链接返回200与已有id():
    script = _Script()
    script.create = []
    script.existing = [[7, "link"]]
    script.job_counts = [[1, 1, 0, 0]]
    r = _call(script, "/api/items", "POST", {"url": "https://v.douyin.com/abc/"})
    assert r["status"] == 200
    body = _json(r)
    assert body["id"] == 7
    assert body["duplicate"] is True
    assert body["status"] == PENDING


def test_纯文字走文本类型():
    script = _Script()
    # 响应里的 kind 取自库里的那一行（库是权威），所以假连接要按实际插入的类型返回
    script.create = [[7, "text"]]
    r = _call(script, "/api/items", "POST", {"text": "今天想到一个点子"})
    assert r["status"] == 202
    assert _json(r)["kind"] == "text"


def test_超长文本被拒():
    r = _call(_Script(), "/api/items", "POST", {"text": "啊" * 20001})
    assert r["status"] == 400
    assert "太长" in _json(r)["error"]


# ── GET /api/items ────────────────────────────────────────────


def test_列表返回条目与下一页游标():
    script = _Script()
    now = datetime.now(timezone.utc)
    script.list_rows = [
        [
            9,
            "link",
            "https://a.com/x",
            "标题甲",
            "a.com",
            now,
            "unread",
            None,
            1,
            1,
            0,
            0,
        ],
        [8, "text", None, "标题乙", "", now, "unread", None, 0, 0, 0, 0],
    ]
    r = _call(script, "/api/items?limit=2")
    assert r["status"] == 200
    body = _json(r)
    assert [item["id"] for item in body["items"]] == [9, 8]
    assert body["items"][0]["status"] == PENDING
    assert body["items"][1]["status"] == NO_JOB
    assert body["nextBefore"] == 8


def test_列表不满一页时没有下一页():
    script = _Script()
    now = datetime.now(timezone.utc)
    script.list_rows = [
        [9, "link", "https://a.com/x", "标题", "a.com", now, "unread", None, 1, 0, 0, 0]
    ]
    body = _json(_call(script, "/api/items?limit=2"))
    assert body["nextBefore"] is None
    assert body["items"][0]["status"] == DONE


def test_limit_非整数返回400():
    r = _call(_Script(), "/api/items?limit=abc")
    assert r["status"] == 400
    assert "整数" in _json(r)["error"]


def test_limit_超范围夹到上限而不是报错():
    """翻页时前端传 200 不该变成一次失败，夹到 100 就行。"""
    script = _Script()
    assert _call(script, "/api/items?limit=200")["status"] == 200


# ── GET /api/items/{id} ───────────────────────────────────────


def test_详情查不到返回404():
    r = _call(_Script(), "/api/items/999")
    assert r["status"] == 404


def test_详情返回处理状态与任务明细():
    script = _Script()
    now = datetime.now(timezone.utc)
    script.detail_rows = [
        [
            7,
            "link",
            "https://a.com/x",
            "标题",
            None,
            "a.com",
            None,
            "正文",
            "unread",
            now,
            None,
            {},
        ]
    ]
    script.job_counts = [[1, 1, 0, 0]]
    script.job_rows = [["fetch", "pending", None, 0, now]]
    body = _json(_call(script, "/api/items/7"))
    assert body["status"] == PENDING
    assert body["readStatus"] == "unread"
    assert body["jobs"][0]["step"] == "fetch"


# ── 首页 ──────────────────────────────────────────────────────


def test_首页有提交框与状态徽标():
    script = _Script()
    now = datetime.now(timezone.utc)
    script.list_rows = [
        [
            7,
            "link",
            "https://a.com/x",
            "一条标题",
            "a.com",
            now,
            "unread",
            None,
            1,
            1,
            0,
            0,
        ]
    ]
    r = _call(script, "/")
    assert r["status"] == 200
    html = r["text"]
    assert "存起来" in html
    assert "粘贴链接或一段文字" in html
    assert "待处理" in html
    assert "一条标题" in html


def test_首页空列表给出提示():
    html = _call(_Script(), "/")["text"]
    assert "还没有内容" in html


def test_首页对用户输入做转义():
    """标题来自外部内容，必须转义——否则一条 `<script>` 标题就能在首页执行。"""
    script = _Script()
    now = datetime.now(timezone.utc)
    script.list_rows = [
        [
            7,
            "link",
            "https://a.com/x",
            "<script>alert(1)</script>",
            "a.com",
            now,
            "unread",
            None,
            0,
            0,
            0,
            0,
        ]
    ]
    html = _call(script, "/")["text"]
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize(
    "status_value", ["pending", "running", "done", "failed", "none"]
)
def test_首页每种状态都有中文徽标(status_value: str) -> None:
    """徽标文案表必须是全覆盖的：漏一个就会在页面上露出英文枚举值。"""
    from kb.api.pages import _STATUS_LABELS

    assert status_value in _STATUS_LABELS

"""`GET /api/search` 的契约（设计 §3.7 与 §9 T05 的七条判据）。

## 用迷你 app + 假 client + 假 pool

不挂真实的 `SessionMiddleware`：那会牵扯数据库、bcrypt 与登录页，把「字段对不对」
埋在一堆无关依赖里（与 `tests/test_api_ai_usage.py` 同一个手法）。
鉴权单独用一条 `test_未登录返回401` 验「不在 PUBLIC_PATHS 里」。

不连 Meili：`client` 参数注入一个按剧本返回的假实现。三种模式全部可验。

## 时区守卫用例（设计 §3.9 末段的四条断言）

本文件用**真实的 `parse_date_bound`** 换算 epoch 秒，再用
`from`/`to` 生成的 filter 串断言命中/不命中。设计原文要求「用假 client 断言
传给 Meili 的 filter 串」，正是本节的做法——比连真库跑一遍更快，且失败时
能直接看到 filter 串里哪一段错了。
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from kb.api import search as search_api
from kb.search.models import MeiliUnavailable, SearchMode
from kb.store import items as items_store

#: 11-22 北京时间 **04:30** 的那条（= UTC 11-21 20:30）。
#: 四条守卫断言全部围绕它：它的 `saved_at_ts` 是 1795293000。
#:
#: ⚠️ 这个数**必须**落在「北京 11-22 00:00」之后、「UTC 11-22 00:00」（= 北京
#: 11-22 08:00）之前——那 8 小时窗口正是「按 UTC 切日界会静默漏条」的范围。
TARGET_TS = 1795293000
TARGET_SAVED_AT = "2026-11-22T04:30:00+08:00"
DAY_21 = 1795190400  # 2026-11-21 00:00 +08:00
DAY_22 = 1795276800  # 2026-11-22 00:00 +08:00
DAY_23 = 1795363200  # 2026-11-23 00:00 +08:00


class FakeClient:
    """按剧本返回 Meili 响应体，或抛 `MeiliUnavailable`。"""

    def __init__(self, script: list[object] | None = None) -> None:
        self.script: list[object] = list(script or [])
        self.calls: list[dict[str, object]] = []

    def search(self, index_uid: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(dict(payload))
        if not self.script:
            # 默认：返回空命中（多数用例只关心参数解析，不关心命中内容）
            return {"hits": [], "estimatedTotalHits": 0}
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, dict)
        return item


class FakeConn:
    """假连接：只实现 `known_kinds` 与 `summaries_by_ids` 需要的两条查询。

    `summaries` 的键是 id，值是 `(title, read_status, status)`。
    `status` 是**聚合结果**，这里反推成 jobs 计数再让 `derive_status` 算一遍——
    直接返回 `status` 会让这条假连接绕过一个真实存在的聚合步骤，
    那一步的语义（`running` > `pending` > `failed` > `done`）就验不到了。
    """

    #: 聚合结果 → jobs 计数 `(total, pending, running, failed)`。
    #: 派生顺序与 `items.derive_status` 一致。
    _COUNTS: dict[str, tuple[int, int, int, int]] = {
        "running": (2, 1, 1, 0),
        "pending": (2, 1, 0, 0),
        "failed": (1, 0, 0, 1),
        "done": (1, 0, 0, 0),
        "none": (0, 0, 0, 0),
    }

    def __init__(
        self,
        *,
        kinds: tuple[str, ...] = ("article", "video", "text"),
        summaries: dict[int, tuple[str, str, str]] | None = None,
    ) -> None:
        self.kinds = kinds
        #: `id -> (title, read_status, status)`
        self.summaries = summaries or {}

    def run(self, sql: str, **params: object) -> list[list[object]]:
        low = " ".join(sql.split()).lower()
        if "from item_kinds" in low:
            return [[kind] for kind in self.kinds]
        if "sub.id = any(" in low:
            ids = [int(i) for i in (params.get("ids") or [])]  # type: ignore[union-attr]
            out: list[list[object]] = []
            for item_id in ids:
                row = self.summaries.get(item_id)
                if row is None:
                    continue
                title, read_status, status = row
                total, pending, running, failed = self._COUNTS.get(status, (0, 0, 0, 0))
                # 列顺序必须与 `items._LIST_SQL` 一致（12 列）。
                # `_summary_from_row` 的参数顺序是 (pending, running, failed, total)。
                out.append(
                    [
                        item_id,  # 0 id
                        "article",  # 1 kind
                        None,  # 2 url
                        title,  # 3 title
                        "x.invalid",  # 4 source_domain
                        _saved_at(),  # 5 saved_at
                        read_status,  # 6 read_status
                        "",  # 7 summary
                        total,  # 8 jobs total
                        pending,  # 9 jobs pending
                        running,  # 10 jobs running
                        failed,  # 11 jobs failed
                    ]
                )
            return out
        return []


def _saved_at():
    from datetime import datetime, timezone

    return datetime(2026, 11, 22, 4, 30, tzinfo=timezone.utc)


def _pool(conn: object) -> object:
    class _Pool:
        def __init__(self, c: object) -> None:
            self._conn = c

        def connection(self) -> object:
            from contextlib import contextmanager

            @contextmanager
            def _cm():
                yield self._conn

            return _cm()

    return _Pool(conn)


def _client(
    *,
    meili: object = None,
    conn: object = None,
    logged_in: bool = True,
    min_score: float = 0.3,
) -> TestClient:
    """只挂 `/api/search` 的迷你 app。

    `logged_in=False` 时用一个「一律 401」的中间件模拟 `app.py` 的
    `SessionMiddleware`——本文件只验「搜不到数据」这一条，不重复验认证实现。
    """
    from starlette.middleware.base import (
        BaseHTTPMiddleware,
        RequestResponseEndpoint,
    )
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response

    class _Auth(BaseHTTPMiddleware):
        async def dispatch(
            self, request: Request, call_next: RequestResponseEndpoint
        ) -> Response:
            if logged_in:
                return await call_next(request)
            return JSONResponse({"error": "未登录"}, status_code=401)

    handler = search_api.search(
        _pool(conn or FakeConn()),  # type: ignore[arg-type]
        client=meili if meili is not None else FakeClient(),
        min_score=min_score,
    )
    app = Starlette(
        routes=[Route("/api/search", handler)], middleware=[Middleware(_Auth)]
    )
    return TestClient(app)


def _hit_row(item_id: int, score: float = 0.9) -> dict[str, object]:
    return {
        "id": item_id,
        "kind": "article",
        "title": f"标题{item_id}",
        "summary": "",
        "tags": ["营销"],
        "source_domain": "x.invalid",
        "saved_at": TARGET_SAVED_AT,
        "read_status": "unread",
        "_rankingScore": score,
    }


# ── 三种 searchMode ──────────────────────────────────────────


def test_meili模式返回200与searchMode为meili() -> None:
    meili = FakeClient([{"hits": [_hit_row(1)], "estimatedTotalHits": 1}])
    conn = FakeConn(summaries={1: ("标题1", "unread", "done")})
    response = _client(meili=meili, conn=conn).get("/api/search?q=营销")

    assert response.status_code == 200
    body = response.json()
    assert body["searchMode"] == "meili"
    assert body["degraded"] is False
    assert body["relaxed"] is False
    assert body["estimatedTotal"] == 1
    assert len(body["items"]) == 1


def test_fallback模式返回200与degraded为真且notice非空() -> None:
    """设计 §9 T05 判据：`fallback` 是 200 + `degraded: true` + `notice` 人话。"""
    meili = FakeClient([MeiliUnavailable("连不上")])
    response = _client(meili=meili).get("/api/search?q=营销")

    assert response.status_code == 200
    body = response.json()
    assert body["searchMode"] == "fallback"
    assert body["degraded"] is True
    assert body["notice"], "降级时必须给人话提示"
    assert "fallback" not in body["notice"], "不给用户看内部术语"


def test_unavailable模式返回503() -> None:
    """设计 §9 T05 判据：`unavailable` 返回 **503**。

    不是 200 + 空列表——那会把「搜不了」谎报成「库里没有」。
    """
    meili = FakeClient([MeiliUnavailable("连不上")])

    # 兜底也失败：让 summaries_by_ids 那条查询抛
    class _BadConn(FakeConn):
        def run(self, sql: str, **params: object) -> list[list[object]]:
            low = " ".join(sql.split()).lower()
            if "from item_kinds" in low:
                return [["article"]]
            if "from items i" in low:
                raise RuntimeError("数据库也挂了")
            return []

    response = _client(meili=meili, conn=_BadConn()).get("/api/search?q=营销")

    assert response.status_code == 503
    body = response.json()
    assert body["searchMode"] == "unavailable"
    assert body["degraded"] is True
    assert body["error"]


def test_unavailable的响应含searchMode与degraded两个字段() -> None:
    """503 的 body 也要带这两个字段：前端一个判据处理所有分支。"""
    meili = FakeClient([MeiliUnavailable("挂")])

    class _BadConn(FakeConn):
        def run(self, sql: str, **params: object) -> list[list[object]]:
            low = " ".join(sql.split()).lower()
            if "from item_kinds" in low:
                return [["article"]]
            raise RuntimeError("挂")

    body = _client(meili=meili, conn=_BadConn()).get("/api/search?q=x").json()
    assert set(body) == {"error", "searchMode", "degraded"}


# ── 参数校验 ─────────────────────────────────────────────────


def test_未知kind返回400且消息含该值() -> None:
    """设计 §9 T05 判据原文：错误消息含那个值——用户要知道是哪个值错了。"""
    response = _client().get("/api/search?kind=zz_not_a_kind")
    assert response.status_code == 400
    assert "zz_not_a_kind" in response.json()["error"]


def test_未知status返回400() -> None:
    response = _client().get("/api/search?status=zzz")
    assert response.status_code == 400
    assert "zzz" in response.json()["error"]


def test_合法kind通过校验() -> None:
    meili = FakeClient()
    response = _client(meili=meili).get("/api/search?kind=video")
    assert response.status_code == 200
    assert meili.calls[0]["filter"] == 'kind = "video"'


def test_kind支持重复参数() -> None:
    meili = FakeClient()
    _client(meili=meili).get("/api/search?kind=video&kind=article")
    assert meili.calls[0]["filter"] == '(kind = "video" OR kind = "article")'


def test_kind支持逗号分隔() -> None:
    meili = FakeClient()
    _client(meili=meili).get("/api/search?kind=video,article")
    assert meili.calls[0]["filter"] == '(kind = "video" OR kind = "article")'


def test_kind的两种写法去重() -> None:
    meili = FakeClient()
    _client(meili=meili).get("/api/search?kind=video,article&kind=video")
    assert meili.calls[0]["filter"] == '(kind = "video" OR kind = "article")'


def test_limit超50夹到50() -> None:
    """设计 §9 T05 判据原文：不报错，夹边界（与 `/api/items` 的既有口径一致）。"""
    meili = FakeClient()
    response = _client(meili=meili).get("/api/search?limit=999")
    assert response.status_code == 200
    assert response.json()["limit"] == 50
    assert meili.calls[0]["limit"] == 50


def test_offset超999夹到999() -> None:
    meili = FakeClient()
    response = _client(meili=meili).get("/api/search?offset=5000")
    assert response.status_code == 200
    assert response.json()["offset"] == 999


def test_limit为0时夹到1() -> None:
    meili = FakeClient()
    assert _client(meili=meili).get("/api/search?limit=0").json()["limit"] == 1


def test_负limit夹到1() -> None:
    meili = FakeClient()
    assert _client(meili=meili).get("/api/search?limit=-5").json()["limit"] == 1


def test_负offset夹到0() -> None:
    meili = FakeClient()
    assert _client(meili=meili).get("/api/search?offset=-5").json()["offset"] == 0


def test_limit非整数返回400() -> None:
    response = _client().get("/api/search?limit=abc")
    assert response.status_code == 400
    assert "limit" in response.json()["error"]


def test_limit缺省是20() -> None:
    meili = FakeClient()
    assert _client(meili=meili).get("/api/search").json()["limit"] == 20


# ── 命中查不到对应 item 时跳过 ────────────────────────────────


def test_命中里查不到对应item的id被跳过() -> None:
    """设计 §9 T05 判据原文：不出现空卡片。

    索引里有 1/2/3，库里只有 1/3（2 已删）→ 返回两条，不返回一条空壳。
    """
    meili = FakeClient(
        [{"hits": [_hit_row(1), _hit_row(2), _hit_row(3)], "estimatedTotalHits": 3}]
    )
    conn = FakeConn(
        summaries={
            1: ("标题1", "unread", "done"),
            3: ("标题3", "read", "done"),
        }
    )
    body = _client(meili=meili, conn=conn).get("/api/search?q=x").json()
    assert [item["id"] for item in body["items"]] == [1, 3]


def test_全部命中都查不到时返回空items但不是错误() -> None:
    """索引陈旧（库里全删了）→ 200 + 空 items。这不是故障。"""
    meili = FakeClient([{"hits": [_hit_row(1)], "estimatedTotalHits": 1}])
    body = _client(meili=meili, conn=FakeConn()).get("/api/search?q=x").json()
    assert body["items"] == []
    assert body["searchMode"] == "meili"


# ── 字段名与 /api/items 一致 ──────────────────────────────────


def test_响应字段名是camelCase且与api_items一致() -> None:
    """设计 §9 T05 判据原文：`sourceDomain`/`savedAt`/`readStatus` 等 camelCase。

    `ITEM_FIELDS` 是契约的单一真源——改它等于改前端。
    """
    meili = FakeClient([{"hits": [_hit_row(1)], "estimatedTotalHits": 1}])
    conn = FakeConn(summaries={1: ("标题1", "unread", "done")})
    item = _client(meili=meili, conn=conn).get("/api/search?q=x").json()["items"][0]

    assert set(item) == set(search_api.ITEM_FIELDS)
    assert "sourceDomain" in item
    assert "savedAt" in item
    assert "readStatus" in item
    assert "source_domain" not in item, "索引内的 snake_case 不该漏到 API"


def test_响应顶层字段齐全() -> None:
    meili = FakeClient()
    body = _client(meili=meili).get("/api/search").json()
    assert set(body) == {
        "items",
        "searchMode",
        "degraded",
        "relaxed",
        "notice",
        "limit",
        "offset",
        "estimatedTotal",
    }


def test_status由数据库批量补全而不是索引里带的() -> None:
    """索引里没有 `status`（它由 jobs 聚合现算）。API 补全后应等于库里的值。"""
    meili = FakeClient([{"hits": [_hit_row(1)], "estimatedTotalHits": 1}])
    conn = FakeConn(summaries={1: ("标题1", "unread", "running")})
    item = _client(meili=meili, conn=conn).get("/api/search?q=x").json()["items"][0]

    assert item["status"] == "running"
    assert item["readStatus"] == "unread"


def test_fallback模式的rankingScore是field_hits除以5() -> None:
    """兜底模式没有 `_rankingScore`，store 层给的是 `field_hits / 5`。

    两者都是 0–1 浮点，语义差异由 `searchMode` 标明（设计 §10「分数口径」）。
    """

    class _FallbackConn(FakeConn):
        def run(self, sql: str, **params: object) -> list[list[object]]:
            low = " ".join(sql.split()).lower()
            if "from item_kinds" in low:
                return [["article"]]
            if "field_hits" in low:
                # 列顺序见 `search_store._hit_from_row`（9 列）
                return [
                    [
                        7,
                        "video",
                        "抖音剪辑",
                        "手机端剪映",
                        "v.douyin.com",
                        _saved_at(),
                        "unread",
                        ["视频"],
                        3,
                    ]
                ]
            if "sub.id = any(" in low:
                return [
                    [
                        7,
                        "video",
                        None,
                        "抖音剪辑",
                        "v.douyin.com",
                        _saved_at(),
                        "unread",
                        "手机端剪映",
                        1,
                        0,
                        0,
                        0,
                    ]
                ]
            return []

    meili = FakeClient([MeiliUnavailable("挂")])
    body = _client(meili=meili, conn=_FallbackConn()).get("/api/search?q=抖音").json()

    assert body["searchMode"] == "fallback"
    assert body["items"][0]["rankingScore"] == pytest.approx(3 / 5)


# ── 鉴权 ─────────────────────────────────────────────────────


def test_未登录返回401() -> None:
    """设计 §9 T05 判据原文：**不打进 `PUBLIC_PATHS`**。

    搜索会返回库里的标题、摘要与标签，未登录请求不该看到这些内容。
    """
    response = _client(logged_in=False).get("/api/search?q=营销")
    assert response.status_code == 401
    assert "items" not in response.text


def test_search不在PUBLIC_PATHS里() -> None:
    """直接读 `kb/api/app.py` 的白名单：加进去就等于把库内容公开。

    这条比「用真 create_app 请求一次」更直接——它拦的是**配置**，
    而配置一旦改错，用真 app 的用例也会跟着一起放行（它读的是同一份常量）。
    """
    from kb.api import app as app_module

    assert "/api/search" not in app_module.PUBLIC_PATHS


# ── 时区守卫（设计 §3.9 的四条断言）──────────────────────────


def test_from按上海时区解释_11月22日命中11月22日凌晨的内容() -> None:
    """守卫用例一：`from=2026-11-22` → 命中。

    构造的时刻是 2026-11-21T20:30Z（= 北京时间 11-22 04:30）。
    若按 UTC 解释 `from`，`from_ts` 会是 UTC 11-22 00:00（= 北京 08:00），
    这条 04:30 的内容**整体漏掉**。
    """
    assert search_api.parse_date_bound("2026-11-22", is_upper=False) == DAY_22
    assert DAY_22 <= TARGET_TS, "04:30 的条目必须落在 11-22 00:00 之后"


def test_from为11月23日时不命中11月22日凌晨的内容() -> None:
    """守卫用例二：`from=2026-11-23` → 不命中。"""
    assert search_api.parse_date_bound("2026-11-23", is_upper=False) == DAY_23
    assert not (DAY_23 <= TARGET_TS)


def test_to为11月22日时命中11月22日凌晨的内容() -> None:
    """守卫用例三：`to=2026-11-22` → 命中。

    `to` 取**次日零点**（11-23 00:00）+ `<`，所以 11-22 当天的 23:59:59
    在范围内，而 11-23 00:00:00 那条不在。
    """
    assert search_api.parse_date_bound("2026-11-22", is_upper=True) == DAY_23
    assert TARGET_TS < DAY_23


def test_to为11月21日时不命中11月22日凌晨的内容() -> None:
    """守卫用例四：`to=2026-11-21` → 不命中（目标在 11-22）。"""
    assert search_api.parse_date_bound("2026-11-21", is_upper=True) == DAY_22
    assert not (TARGET_TS < DAY_22)


def test_from生成的是大于等于的filter() -> None:
    meili = FakeClient()
    _client(meili=meili).get("/api/search?from=2026-11-22")
    assert meili.calls[0]["filter"] == f"saved_at_ts >= {DAY_22}"


def test_to生成的是小于的filter且取次日零点() -> None:
    """`to` 一律 `<`，不是 `<=`。设计 §3.9 的半开区间。"""
    meili = FakeClient()
    _client(meili=meili).get("/api/search?to=2026-11-22")
    assert meili.calls[0]["filter"] == f"saved_at_ts < {DAY_23}"


def test_带时间的参数按该时刻解释不取日界() -> None:
    """设计 §3.9：`2026-11-22T10:30` 按 `Asia/Shanghai` 解释该时刻，
    不再取日界。10:30 北京 = 02:30 UTC。"""
    expected = DAY_22 + 10 * 3600 + 30 * 60
    assert search_api.parse_date_bound("2026-11-22T10:30", is_upper=False) == expected
    # `to` 带时间时同样不取次日零点
    assert search_api.parse_date_bound("2026-11-22T10:30", is_upper=True) == expected


def test_带时区偏移的参数尊重用户给的偏移() -> None:
    """用户已经说了时区，不该再套一层上海时区（那会平移 8 小时）。"""
    from datetime import datetime

    raw = "2026-11-22T02:30:00+00:00"
    expected = int(datetime.fromisoformat(raw).timestamp())
    assert search_api.parse_date_bound(raw, is_upper=False) == expected


def test_from与to组合是半开区间() -> None:
    meili = FakeClient()
    _client(meili=meili).get("/api/search?from=2026-11-22&to=2026-11-22")
    assert meili.calls[0]["filter"] == (
        f"saved_at_ts >= {DAY_22} AND saved_at_ts < {DAY_23}"
    )


def test_日期格式错返回400() -> None:
    response = _client().get("/api/search?from=not-a-date")
    assert response.status_code == 400
    assert "日期" in response.json()["error"]


def test_显示时区常量是上海() -> None:
    from kb.search.config import DISPLAY_TZ

    assert DISPLAY_TZ == "Asia/Shanghai"
    assert search_api.DISPLAY_TZ == DISPLAY_TZ


# ── 单字提示与放宽标记 ───────────────────────────────────────


def test_单字查询时notice非空但degraded为假() -> None:
    meili = FakeClient([{"hits": [_hit_row(1)], "estimatedTotalHits": 1}])
    conn = FakeConn(summaries={1: ("标题1", "unread", "done")})
    body = _client(meili=meili, conn=conn).get("/api/search?q=书").json()

    assert body["notice"], "单字查询要带提示"
    assert body["degraded"] is False, "服务没坏，不该点亮降级提示条"


def test_放宽重查时relaxed为真() -> None:
    meili = FakeClient(
        [
            {"hits": [], "estimatedTotalHits": 0},
            {"hits": [_hit_row(1, 0.2)], "estimatedTotalHits": 1},
        ]
    )
    conn = FakeConn(summaries={1: ("标题1", "unread", "done")})
    body = _client(meili=meili, conn=conn).get("/api/search?q=挂耳咖啡").json()

    assert body["relaxed"] is True
    assert body["searchMode"] == "meili"
    assert body["degraded"] is False


# ── 查询规范化 ───────────────────────────────────────────────


def test_查询文本被规范化后再发给meili() -> None:
    """`【三体】` 不该原样送进 Meili：首尾装饰标点会让二字词命中率下降。"""
    meili = FakeClient()
    _client(meili=meili).get("/api/search?q=【三体】")
    assert meili.calls[0]["q"] == "三体"


def test_空q是浏览器模式且带sort() -> None:
    """空查询 = 按 `saved_at_ts` 倒序列全部。Meili 对空 `q` 的默认顺序
    不是「最新优先」。"""
    meili = FakeClient()
    _client(meili=meili).get("/api/search")
    assert meili.calls[0]["q"] == ""
    assert meili.calls[0]["sort"] == ["saved_at_ts:desc"]


def test_tags维度内是AND() -> None:
    meili = FakeClient()
    _client(meili=meili).get("/api/search?tags=营销,运营")
    assert meili.calls[0]["filter"] == 'tags = "营销" AND tags = "运营"'


# ── 索引陈旧时的行为 ─────────────────────────────────────────


def test_estimatedTotal在fallback模式为null() -> None:
    """兜底路径没有可信总数。把「返回了几条」当成「一共有几条」是谎报。"""
    meili = FakeClient([MeiliUnavailable("挂")])
    body = _client(meili=meili).get("/api/search?q=x").json()
    assert body["estimatedTotal"] is None


def test_searchMode是枚举的value字符串() -> None:
    """`SearchMode` 继承 `str`，所以 JSON 里是 `"meili"` 而不是
    `"SearchMode.MEILI"`。这条拦「忘了 `.value`」。"""
    meili = FakeClient()
    body = _client(meili=meili).get("/api/search").json()
    assert body["searchMode"] == SearchMode.MEILI.value == "meili"


# ── summaries_by_ids 被真的用上了 ───────────────────────────


def test_补状态只查一次而不是N加1() -> None:
    """设计 §3.7：**一次查询**补全，不做 N+1。

    20 条命中逐条 `get_item` 就是 20 次往返（每次还要再查 `jobs.counts`，
    实际 40 次）。这条用一个计数假连接验「只发了 1 条 `sub.id = any`」。
    """

    class _CountingConn(FakeConn):
        def __init__(self) -> None:
            super().__init__(
                summaries={i: (f"标题{i}", "unread", "done") for i in range(1, 6)}
            )
            self.select_counts = 0

        def run(self, sql: str, **params: object) -> list[list[object]]:
            if "sub.id = any(" in " ".join(sql.split()).lower():
                self.select_counts += 1
            return super().run(sql, **params)

    conn = _CountingConn()
    meili = FakeClient(
        [{"hits": [_hit_row(i) for i in range(1, 6)], "estimatedTotalHits": 5}]
    )
    body = _client(meili=meili, conn=conn).get("/api/search?q=x").json()

    assert conn.select_counts == 1, f"补状态发了 {conn.select_counts} 次查询"
    assert len(body["items"]) == 5


def test_summaries_by_ids被调用时传入的是命中id列表() -> None:
    """传错 id 列表（例如传成整页 offset）会让补状态永远查不到东西，
    而那表现为「所有条目都被跳过、items 空」——很难从现象倒推。"""
    seen_ids: list[list[int]] = []

    class _SpyConn(FakeConn):
        def run(self, sql: str, **params: object) -> list[list[object]]:
            low = " ".join(sql.split()).lower()
            if "sub.id = any(" in low:
                seen_ids.append([int(i) for i in (params.get("ids") or [])])
            return super().run(sql, **params)

    conn = _SpyConn(summaries={4: ("t4", "unread", "done"), 9: ("t9", "read", "done")})
    meili = FakeClient([{"hits": [_hit_row(4), _hit_row(9)], "estimatedTotalHits": 2}])
    _client(meili=meili, conn=conn).get("/api/search?q=x")
    assert seen_ids == [[4, 9]]


def test_items模块的summaries_by_ids存在() -> None:
    """`kb/api/search.py` 依赖它。缺了会在第一次搜索时 AttributeError。"""
    assert callable(items_store.summaries_by_ids)
    assert callable(items_store.known_kinds)


# ── 配置传递（2026-11-28 真机踩过）──────────────────────────


def test_meili_url与key从AppConfig传进handler() -> None:
    """⚠️ **这条用例为一次真机故障而写。**

    第一版 `kb/api/search.py` 的 handler 自己读环境变量（`meili_url()` /
    `meili_key()`），于是 `AppConfig.from_env` 里那个 `meili_key` 字段成了
    「读了一份但没人用」。后果：平板上的 api 进程环境里没有
    `KB_MEILI_KEY`（supervisor 只配了 `KB_PG_*`），Meili 每次搜索返回 401，
    搜索全部降级成 `fallback`——而 `searchMode: "fallback"` 看起来「能用」，
    不会有人去查为什么不是 `meili`。

    现在 handler 收 `meili_url` / `meili_key` 两个参数，由 `create_app` 从
    `cfg` 传入。这条用例断言「传进去的值真的被用了」：给一个假 key，
    断言 `Authorization` 头里是它。
    """
    seen_headers: list[dict[str, str]] = []

    class _RecordingTransport:
        def request(self, method, url, headers, body):  # type: ignore[no-untyped-def]
            seen_headers.append(dict(headers))
            return 200, '{"hits": [], "estimatedTotalHits": 0}'

    from kb.search.client import MeiliClient

    client = MeiliClient(
        "http://example.invalid:7700", "", transport=_RecordingTransport()
    )
    # 直接构造一个带 key 的客户端，验「key 会被带到请求头」
    client.api_key = "the-key-from-config"
    _client(meili=client).get("/api/search?q=x")
    assert seen_headers[0].get("Authorization") == "Bearer the-key-from-config"


def test_app_config有meili_key字段() -> None:
    """`AppConfig` 必须有这个字段，否则 `create_app` 传不进 handler。"""
    from pathlib import Path

    from kb.api.app import AppConfig

    cfg = AppConfig(
        meili_url="http://127.0.0.1:7700", media_root=Path("/tmp"), meili_key="k"
    )
    assert cfg.meili_key == "k"


def test_app_config从环境读KB_MEILI_KEY(monkeypatch) -> None:
    """worker 与 api 必须读**同一个**环境变量名（设计 §10「环境变量」）。"""
    from kb.api.app import AppConfig

    monkeypatch.setenv("KB_MEILI_KEY", "from-env")
    monkeypatch.setenv("KB_MEILI_URL", "http://example.invalid:9999")
    cfg = AppConfig.from_env()
    assert cfg.meili_key == "from-env"
    assert cfg.meili_url == "http://example.invalid:9999"


def test_环境变量名与search_config一致() -> None:
    """两处各写一份变量名时，「改一处忘了另一处」会让 api 与 worker
    连不同的 Meili——两边都健康，但索引写进去搜不到。"""
    from kb.search import config as search_config

    assert search_config.MEILI_KEY_ENV == "KB_MEILI_KEY"
    assert search_config.MEILI_URL_ENV == "KB_MEILI_URL"


def test_未登录时也返回401即使Meili配置正确() -> None:
    """鉴权在检索之前：配置对了也不能让未登录请求拿到内容。"""
    response = _client(logged_in=False).get("/api/search?q=营销")
    assert response.status_code == 401

"""T03 的纯逻辑判据：规范化、filter 串、枚举一致性、三级降级、熔断。

**全组不连库、不联网**：假客户端抛/返回剧本，假兜底返回固定列表，假时钟让
熔断的 30 秒在毫秒内走完。这是 `kb/search/` 不 import `kb.store` 的直接收益
（设计 §1.4 理由二）。

## 这个文件为什么把 query 与 service 放在一起

两者的边界情况互相咬合：`build_filter` 拼出的串是 `service` 发给客户端的，
`min_score=0` 的放宽路径直接改 `build_search_payload` 的产物。分两个文件会让
「放宽时 filter 还在不在」这类断言要找两处。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kb.search.config import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_OFFSET,
    MAX_QUERY_LEN,
    MAX_TERMS,
    MIN_SCORE,
)
from kb.search.models import (
    MeiliRejected,
    MeiliUnauthorized,
    MeiliUnavailable,
    SearchFilter,
    SearchHit,
    SearchMode,
    SearchQuery,
)
from kb.search.query import (
    build_filter,
    build_search_payload,
    build_terms,
    looks_like_single_char,
    normalize_query,
    order_terms,
    quote,
    terms_param_names,
)
from kb.search.service import (
    NOTICE_FALLBACK,
    NOTICE_RELAXED,
    NOTICE_SINGLE_CHAR,
    NOTICE_UNAVAILABLE,
    SearchService,
)
from kb.store.search import READ_STATUSES

MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "kb"
    / "store"
    / "migrations"
    / "0001_schema.sql"
)


# ── normalize_query ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 空与纯空白 → 空串 = 浏览器模式
        ("", ""),
        ("   ", ""),
        ("\t\n  ", ""),
        # 首尾空白
        ("  三体  ", "三体"),
        # 连续空白压成一个：多一个空格会让 Meili 多切一个词
        ("运算  营", "运算 营"),
        ("抖音   剪辑", "抖音 剪辑"),
        # 全角空格 → 半角
        ("矩阵\u3000营销", "矩阵 营销"),
        # 首尾装饰标点（分享文案残留）
        ("【三体】", "三体"),
        ("·营销·", "营销"),
        ("「矩阵营销」", "矩阵营销"),
        # 多层装饰要一次剥干净
        ("【·三体·】", "三体"),
        # 中间的字符不动：中间空格是词边界，删了会变成更长的词
        ("三体 问题", "三体 问题"),
        # 全角冒号转半角，但不清除（它是内容的一部分）
        ("比例：一比十五", "比例:一比十五"),
    ],
)
def test_规范化给出确定输出(raw: str, expected: str) -> None:
    assert normalize_query(raw) == expected


def test_超长查询截到64字符() -> None:
    """Meili 只看前 10 个词，超长部分是噪音（设计 §5.3）。"""
    long = "三" * 200
    assert len(normalize_query(long)) == MAX_QUERY_LEN


def test_恰好64字符不被截() -> None:
    exact = "三" * MAX_QUERY_LEN
    assert normalize_query(exact) == exact


def test_规范化是幂等的() -> None:
    """`normalize(normalize(x)) == normalize(x)`。

    幂等不成立的话「用规范化后的结果再看一眼」这类二次处理会继续改文本，
    而两处各改一次必然分叉。
    """
    for raw in ["【· 三体 ·】", "  抖音   剪辑 ", "矩阵\u3000营销", ""]:
        once = normalize_query(raw)
        assert normalize_query(once) == once, raw


# ── build_terms ────────────────────────────────────────────────


def test_切词按空白且去重() -> None:
    assert build_terms("抖音 剪辑") == ("抖音", "剪辑")
    assert build_terms("营销 营销") == ("营销",), "重复词要去重，否则 SQL 多一组 ilike"


def test_切词上限是5个() -> None:
    """每个词展开成 5 个 ILIKE，5 个词就是 25 个条件；
    第 6 个词的边际收益接近 0，而 Seq Scan 的表达式求值成本会线性上升。"""
    terms = build_terms("一 二 三 四 五 六 七")
    assert len(terms) == MAX_TERMS
    assert terms == ("一", "二", "三", "四", "五")


def test_空查询切出空元组() -> None:
    """空元组 = 浏览器模式，store 层据此走「按 saved_at 倒序列全部」。"""
    assert build_terms("") == ()
    assert build_terms("   ") == ()


def test_单字查询保留() -> None:
    """中文单字是有效查询（「书」「图」）。不因为「结果杂」而在切词层丢掉它。"""
    assert build_terms("书") == ("书",)


def test_切词前会先规范化() -> None:
    """全角空格与首尾标点都要在切词之前处理掉，否则会切出 `【三体】` 这种块。

    `"【三体】　问题"` 整体规范化后首尾只剩 `【`——中间那个 `】` 贴着空格
    留在文本里，天真地按空格切会得到 `["三体】", "问题"]`，兜底 SQL 拿
    `%三体】%` 去匹配，一条都命中不到。所以词块要各自再剥一次。
    """
    assert build_terms("【三体】\u3000问题") == ("三体", "问题")
    assert build_terms("【三体】") == ("三体",)


# ── quote 与转义 ───────────────────────────────────────────────


def test_标签名里含双引号时被正确转义() -> None:
    """设计 §9 T03 的判据原文：`tags = "he said \\"hi\\""`。

    不转义会得到 `tags = "he said "hi""`，Meili 的解析器在第二个引号处截断，
    剩下 `hi""` 被当成语法错误 → 400 → 搜索路径上不降级 → 抛 500。
    """
    assert quote('he said "hi"') == '"he said \\"hi\\""'


def test_build_filter转义标签里的双引号() -> None:
    expression = build_filter(SearchFilter(tags=('he said "hi"',)))
    assert expression == 'tags = "he said \\"hi\\""'


def test_反斜杠必须转义成两个() -> None:
    """⚠️ **这条用例的断言被我写反过，是 QA 抓出来的。**

    第一版这里断言「反斜杠不转义」，理由写在 docstring 里：「Meili 只定义了
    双引号的转义，反斜杠按字面处理」。**那句话是错的。**

    真机复验（平板 Meili 1.58.0，每种跑 5 次）：

    | filter 值 | 结果 |
    |---|---|
    | `tags = "C:\\Users"`（1 个反斜杠） | **400 `Malformed value`**，5/5 稳定 |
    | `tags = "C:\\\\Users"`（2 个反斜杠） | 200，命中该标签 |

    用户影响：库里有含反斜杠的标签（粘贴进来的 Windows 路径）时，按它筛选
    会产生 400，而 400 在搜索路径上**不降级**（设计 §6.1）→ 整个搜索页 500，
    不是「筛不出来」。

    我最初为什么会写反：早期实验里我以为在测反斜杠，实际构造的是正斜杠
    （`"C:/Users"`），repr 显示成 `'C://Users'` 让我误以为已经发生了转义。
    改用 `chr(92)` 逐字节对照后才看清。
    """
    assert quote("C:\\Users") == '"C:\\\\Users"'
    # 单个反斜杠也要转
    assert quote("C:\\") == '"C:\\\\"'


def test_反斜杠与双引号相邻时两个都要转且顺序正确() -> None:
    """`\\` 与 `"` 同时出现时，**必须先把反斜杠转成两个，再转双引号**。

    顺序反了会出错：若先做 `"` → `\\"`，那个新产生的反斜杠会被第二步
    再转一遍变成 `\\\\"`，引号又暴露出来了。

    这类输入真的会出现：标签名里粘贴一段带引号的 Windows 路径。
    """
    # 输入：C:\"x  （反斜杠 + 双引号相邻）
    assert quote('C:\\"x') == '"C:\\\\\\"x"'


# ── build_filter ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        # 空 → 空串（Meili 不接受空 filter）
        (SearchFilter(), ""),
        # 单值三维
        (SearchFilter(kinds=("video",)), 'kind = "video"'),
        (SearchFilter(tags=("营销",)), 'tags = "营销"'),
        (SearchFilter(read_statuses=("unread",)), 'read_status = "unread"'),
        # 维度内 OR（kinds / read_statuses）
        (
            SearchFilter(kinds=("video", "article")),
            '(kind = "video" OR kind = "article")',
        ),
        (
            SearchFilter(read_statuses=("unread", "reading")),
            '(read_status = "unread" OR read_status = "reading")',
        ),
        # 维度内 AND（tags）：每个标签单独一个条件
        (SearchFilter(tags=("营销", "运营")), 'tags = "营销" AND tags = "运营"'),
        # 时间：from 含、to 不含
        (SearchFilter(saved_from_ts=100), "saved_at_ts >= 100"),
        (SearchFilter(saved_to_ts=200), "saved_at_ts < 200"),
        (
            SearchFilter(saved_from_ts=100, saved_to_ts=200),
            "saved_at_ts >= 100 AND saved_at_ts < 200",
        ),
        # 维度之间 AND
        (
            SearchFilter(kinds=("video",), tags=("营销",), saved_from_ts=1),
            'kind = "video" AND tags = "营销" AND saved_at_ts >= 1',
        ),
        # 全维度
        (
            SearchFilter(
                kinds=("video",),
                tags=("营销", "运营"),
                read_statuses=("unread",),
                saved_from_ts=10,
                saved_to_ts=20,
            ),
            'kind = "video" AND tags = "营销" AND tags = "运营"'
            ' AND read_status = "unread" AND saved_at_ts >= 10 AND saved_at_ts < 20',
        ),
    ],
)
def test_filter串在各维度组合下正确(condition: SearchFilter, expected: str) -> None:
    assert build_filter(condition) == expected


def test_to用小于号不是小于等于() -> None:
    """半开区间。`to=2026-11-22` 被换算成「11-23 00:00 的 epoch 秒」，
    用 `<` 才能包含 11-22 23:59:59 且不把 11-23 00:00:00 那条装进来。"""
    expression = build_filter(SearchFilter(saved_from_ts=1, saved_to_ts=2))
    assert "saved_at_ts < 2" in expression
    assert "saved_at_ts <= 2" not in expression


def test_is_empty与空filter串等价() -> None:
    """`is_empty()` 与 `build_filter() == ""` 必须一致。不一致时
    「要不要发 filter 字段」的判据会分叉（一处判空一处判串）。"""
    assert SearchFilter().is_empty()
    assert build_filter(SearchFilter()) == ""
    non_empty = SearchFilter(kinds=("video",))
    assert not non_empty.is_empty()
    assert build_filter(non_empty) != ""


# ── READ_STATUSES 与 schema 一致 ───────────────────────────────


def test_READ_STATUSES与0001_schema的CHECK一致() -> None:
    """镜像枚举的唯一守卫用例（设计 §3.8 末段）。

    从 `0001_schema.sql` 的正则抠出 `status` 列 CHECK 里的四个值，
    与本常量比对。两处不一致就红——这正是「枚举只有一个真源，
    镜像必须被守住」的落地方式。
    """
    sql = MIGRATIONS.read_text(encoding="utf-8")
    match = re.search(
        r"status\s+text\s+not\s+null\s+default\s+'unread'\s*\n?\s*"
        r"check\s*\(\s*status\s+in\s*\(([^)]*)\)\s*\)",
        sql,
    )
    assert match, "没在 0001_schema.sql 里找到 items.status 的 CHECK 约束"
    values = tuple(
        part.strip().strip("'") for part in match.group(1).split(",") if part.strip()
    )
    assert values == READ_STATUSES, (
        f"schema 里的枚举 {values} 与 READ_STATUSES {READ_STATUSES} 不一致"
    )


def test_READ_STATUSES是四元组且顺序与schema一致() -> None:
    assert READ_STATUSES == ("unread", "reading", "read", "archived")


# ── build_search_payload ───────────────────────────────────────


def test_payload带showRankingScore与阈值() -> None:
    """不带 `showRankingScore` 时响应里没有 `_rankingScore`，
    API 的 `rankingScore` 永远是 0；不带阈值时低分噪音全进结果。"""
    payload = build_search_payload(
        text="三体", limit=20, offset=0, condition=SearchFilter(), min_score=0.3
    )
    assert payload["showRankingScore"] is True
    assert payload["rankingScoreThreshold"] == 0.3
    assert "filter" not in payload


def test_阈值0时不发rankingScoreThreshold() -> None:
    """`min_score=0` 的语义是「不截断」。发 `rankingScoreThreshold=0` 在
    Meili 上等价，但少一个参数让宽松路径的请求体与「从未设阈值」一致。"""
    payload = build_search_payload(
        text="三体", limit=20, offset=0, condition=SearchFilter(), min_score=0.0
    )
    assert "rankingScoreThreshold" not in payload


def test_有空查询时按saved_at_ts倒序() -> None:
    """Meili 对空 `q` 是 placeholder search，默认按内部文档顺序返回——
    那不是「最新优先」。浏览器模式必须显式按 `saved_at_ts` 倒序。"""
    payload = build_search_payload(
        text="", limit=20, offset=0, condition=SearchFilter(), min_score=0.3
    )
    assert payload["sort"] == ["saved_at_ts:desc"]


def test_有查询词时不加sort() -> None:
    """有查询词时排序必须交给相关度。加 `sort` 会让最相关的排到后面。"""
    payload = build_search_payload(
        text="三体", limit=20, offset=0, condition=SearchFilter(), min_score=0.3
    )
    assert "sort" not in payload


def test_payload带filter当筛选非空() -> None:
    payload = build_search_payload(
        text="x",
        limit=5,
        offset=10,
        condition=SearchFilter(kinds=("video",)),
        min_score=0.3,
    )
    assert payload["filter"] == 'kind = "video"'
    assert payload["limit"] == 5
    assert payload["offset"] == 10


# ── 小工具 ─────────────────────────────────────────────────────


def test_单字判定() -> None:
    assert looks_like_single_char("书")
    assert not looks_like_single_char("书本")
    assert not looks_like_single_char("")


def test_参数名映射() -> None:
    assert terms_param_names(0) == ()
    assert terms_param_names(3) == ("t0", "t1", "t2")


def test_参数名上限与MAX_TERMS一致() -> None:
    """`build_terms` 的上限与 SQL 参数名数量必须由同一个常量控制。
    两处分头写死成 5 与 4 时，「第 5 个词永远不参与查询」且不报错。"""
    assert len(terms_param_names(MAX_TERMS)) == MAX_TERMS


def test_order_terms按长度降序() -> None:
    assert order_terms(("一", "三体问题", "营销")) == ("三体问题", "营销", "一")


# ── SearchService：三级降级 ────────────────────────────────────


class FakeClient:
    """按剧本返回响应体或抛异常。`calls` 记每次的 `(index_uid, payload)`。"""

    def __init__(self, script: list[object] | None = None) -> None:
        self.script: list[object] = list(script or [])
        self.calls: list[tuple[str, dict[str, object]]] = []

    def search(self, index_uid: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((index_uid, dict(payload)))
        if not self.script:
            raise AssertionError(f"剧本用光了（第 {len(self.calls)} 次调用）")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, dict)
        return item


def _hit(item_id: int, score: float = 0.9) -> SearchHit:
    return SearchHit(
        id=item_id,
        kind="article",
        title=f"标题{item_id}",
        summary="",
        tags=(),
        source_domain="",
        saved_at="2026-11-22T10:00:00+08:00",
        read_status="unread",
        status="",
        ranking_score=score,
    )


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _fallback_returning(hits: list[SearchHit]) -> object:
    calls: list[SearchQuery] = []

    def _fn(query: SearchQuery) -> list[SearchHit]:
        calls.append(query)
        return hits

    _fn.calls = calls  # type: ignore[attr-defined]
    return _fn


def _service(
    client: object,
    fallback: object,
    *,
    clock: FakeClock | None = None,
    min_score: float = MIN_SCORE,
) -> SearchService:
    return SearchService(  # type: ignore[arg-type]
        client,
        fallback,  # type: ignore[arg-type]
        clock=clock or FakeClock(),
        min_score=min_score,
    )


def _ok(hits: list[dict[str, object]], total: int = 1) -> dict[str, object]:
    return {"hits": hits, "estimatedTotalHits": total}


def _meili_row(item_id: int, score: float = 0.9) -> dict[str, object]:
    return {
        "id": item_id,
        "kind": "article",
        "title": f"标题{item_id}",
        "summary": "",
        "tags": [],
        "source_domain": "",
        "saved_at": "2026-11-22T10:00:00+08:00",
        "read_status": "unread",
        "_rankingScore": score,
    }


def test_meili正常时mode为meili且带estimatedTotal() -> None:
    client = FakeClient([_ok([_meili_row(1)], total=42)])
    fallback = _fallback_returning([])
    result = _service(client, fallback).run(SearchQuery(text="三体"))

    assert result.mode is SearchMode.MEILI
    assert [h.id for h in result.hits] == [1]
    assert result.estimated_total == 42
    assert result.relaxed is False
    assert result.notice is None
    assert result.degraded is False
    assert not fallback.calls  # type: ignore[attr-defined]


def test_meili挂时降级到fallback() -> None:
    client = FakeClient([MeiliUnavailable("连不上")])
    fallback = _fallback_returning([_hit(7)])
    result = _service(client, fallback).run(SearchQuery(text="三体"))

    assert result.mode is SearchMode.FALLBACK
    assert [h.id for h in result.hits] == [7]
    assert result.notice == NOTICE_FALLBACK
    assert result.degraded is True


def test_fallback没有可信总数给None() -> None:
    """把「返回了几条」当成「一共有几条」是谎报（设计 §3.6 末段）。"""
    client = FakeClient([MeiliUnavailable("连不上")])
    result = _service(client, _fallback_returning([_hit(1), _hit(2)])).run(
        SearchQuery(text="x")
    )
    assert result.estimated_total is None


def test_fallback也挂时mode为unavailable() -> None:
    """兜底也抛 → 503。与「200 + 空列表」的区别是：前者说「搜不了」，
    后者说「库里没有」。混成一种是谎报（设计 §6.3）。"""
    client = FakeClient([MeiliUnavailable("连不上")])

    def _boom(_query: SearchQuery) -> list[SearchHit]:
        raise RuntimeError("数据库也挂了")

    result = _service(client, _boom).run(SearchQuery(text="x"))
    assert result.mode is SearchMode.UNAVAILABLE
    assert result.notice == NOTICE_UNAVAILABLE
    assert result.hits == ()
    assert result.degraded is True


def test_熔断生效后第二次调用不再碰client() -> None:
    """熔断的唯一价值：挂掉之后不要再每搜一次白等 3 秒超时。

    这条用例同时断言 `circuit_trips`：它是「今天挂过几次」的唯一观测点。
    """
    clock = FakeClock()
    client = FakeClient([MeiliUnavailable("连不上")])
    fallback = _fallback_returning([])
    service = _service(client, fallback, clock=clock)

    service.run(SearchQuery(text="a"))
    assert len(client.calls) == 1
    assert service.circuit_trips == 1

    # 冷却期内：直接走兜底，一次都不碰 client
    service.run(SearchQuery(text="b"))
    service.run(SearchQuery(text="c"))
    assert len(client.calls) == 1, f"熔断没生效，client 被调了 {len(client.calls)} 次"
    assert len(fallback.calls) == 3  # type: ignore[attr-defined]


def test_熔断30秒后恢复尝试() -> None:
    clock = FakeClock()
    client = FakeClient([MeiliUnavailable("连不上"), _ok([_meili_row(1)])])
    service = _service(client, _fallback_returning([]), clock=clock)

    assert service.run(SearchQuery(text="a")).mode is SearchMode.FALLBACK
    clock.advance(31.0)
    result = service.run(SearchQuery(text="b"))
    assert result.mode is SearchMode.MEILI, "冷却结束后必须重新尝试主路径"
    assert len(client.calls) == 2


def test_冷却期内不推进_down_until() -> None:
    """冷却期内走兜底时**不重置**熔断截止时刻。

    重置的话，用户连续搜索会让熔断永远不解除——每次兜底都把 30 秒重新算一遍，
    服务恢复了也永远用不上。
    """
    clock = FakeClock()
    client = FakeClient([MeiliUnavailable("挂")])
    service = _service(client, _fallback_returning([]), clock=clock)
    service.run(SearchQuery(text="a"))
    down_until = service._down_until  # noqa: SLF001

    clock.advance(10.0)
    service.run(SearchQuery(text="b"))
    assert service._down_until == down_until  # noqa: SLF001


def test_401在搜索路径上按不可用处理并熔断() -> None:
    """设计 §6.1：**搜索路径上 401/403 也判「不可用」并熔断**。

    用户只关心「结果拿不到」——key 错与 Meili 没启动对他都是「等修」，
    区分它们只会让 API 层多一个分支却无可告知的信息。

    与索引路径的差异是刻意的：索引路径上 401 落 `MeiliUnauthorized` 并
    defer 600 秒 + 写「检查 KB_MEILI_KEY」，因为那条路径上用户能改配置、
    而搜索结果页里没有任何地方能承载「去改密钥」这句话。
    """
    clock = FakeClock()
    client = FakeClient([MeiliUnauthorized("401"), _ok([_meili_row(1)])])
    service = _service(client, _fallback_returning([]), clock=clock)

    first = service.run(SearchQuery(text="a"))
    assert first.mode is SearchMode.FALLBACK
    assert service.circuit_trips == 1, "搜索路径上的 401 要熔断"

    # 冷却期内不再碰 client —— 与 MeiliUnavailable 同一条路径
    second = service.run(SearchQuery(text="b"))
    assert second.mode is SearchMode.FALLBACK
    assert len(client.calls) == 1


def test_filter语法错时异常继续向上抛() -> None:
    """400/422 是**我们的 bug**。降级到 PG 会把它伪装成「搜索服务挂了」，
    日志里只剩「降级」，排查方向走偏（设计 §6.1 最后一行）。"""
    client = FakeClient([MeiliRejected("filter 语法错")])
    with pytest.raises(MeiliRejected):
        _service(client, _fallback_returning([])).run(SearchQuery(text="x"))


def test_MeiliRejected不触发熔断() -> None:
    client = FakeClient([MeiliRejected("400")])
    service = _service(client, _fallback_returning([]))
    with pytest.raises(MeiliRejected):
        service.run(SearchQuery(text="x"))
    assert service.circuit_trips == 0


# ── 0 命中时的放宽重查 ─────────────────────────────────────────


def test_0命中时用min_score为0重查一次且relaxed为真() -> None:
    client = FakeClient(
        [
            _ok([], total=0),  # 严格查询 0 条
            _ok([_meili_row(1, 0.2)], total=1),  # 放宽后 1 条
        ]
    )
    result = _service(client, _fallback_returning([])).run(SearchQuery(text="挂耳咖啡"))

    assert result.mode is SearchMode.MEILI
    assert result.relaxed is True
    assert [h.id for h in result.hits] == [1]
    assert len(client.calls) == 2
    # 第二次请求**不带**阈值
    assert "rankingScoreThreshold" not in client.calls[1][1]
    # 第一次请求带阈值
    assert client.calls[0][1]["rankingScoreThreshold"] == MIN_SCORE


def test_有命中时不重查() -> None:
    client = FakeClient([_ok([_meili_row(1)])])
    _service(client, _fallback_returning([])).run(SearchQuery(text="三体"))
    assert len(client.calls) == 1


def test_放宽后仍0条时relaxed为假() -> None:
    """「放宽过但没结果」与「放宽后才有结果」对用户是两件事，后者才要提示。"""
    client = FakeClient([_ok([], total=0), _ok([], total=0)])
    result = _service(client, _fallback_returning([])).run(SearchQuery(text="量子纠缠"))

    assert result.mode is SearchMode.MEILI
    assert result.relaxed is False
    assert result.hits == ()
    assert result.estimated_total == 0


def test_min_score为0时不重查() -> None:
    """已经不限阈值了，再放宽一次是同一个请求打两遍。"""
    client = FakeClient([_ok([], total=0)])
    result = _service(client, _fallback_returning([]), min_score=0.0).run(
        SearchQuery(text="x")
    )
    assert len(client.calls) == 1
    assert result.relaxed is False


def test_空查询不触发放宽() -> None:
    """浏览器模式 0 条说明库里真的没内容。放宽阈值救不了它，
    而重查一次会把一次搜索变成两次往返。"""
    client = FakeClient([_ok([], total=0)])
    _service(client, _fallback_returning([])).run(SearchQuery(text=""))
    assert len(client.calls) == 1


def test_放宽重查时保留filter() -> None:
    """宽松路径**只放宽分数**，用户的筛选条件必须还在。
    丢 filter 会让「筛了 video 却是全库结果」——用户不知道筛失效了。"""
    client = FakeClient([_ok([], total=0), _ok([_meili_row(1)])])
    _service(client, _fallback_returning([])).run(
        SearchQuery(text="x", filter=SearchFilter(kinds=("video",)))
    )
    assert client.calls[1][1]["filter"] == 'kind = "video"'


def test_放宽重查时若挂了走兜底() -> None:
    """第一次查询成功、重查挂：说明服务刚好在这一刻死了。
    处理应与「一开始就挂」一致——给用户一份能用的结果。"""
    client = FakeClient([_ok([], total=0), MeiliUnavailable("刚好挂了")])
    fallback = _fallback_returning([_hit(9)])
    result = _service(client, fallback).run(SearchQuery(text="x"))

    assert result.mode is SearchMode.FALLBACK
    assert [h.id for h in result.hits] == [9]


# ── 单字提示 ───────────────────────────────────────────────────


def test_单字查询带提示但degraded仍为假() -> None:
    """服务没坏，只是查询本身模糊。把提示塞进 `degraded` 会让前端点亮
    「搜索服务异常」的提示条——那是假警报（设计 §5.3）。"""
    client = FakeClient([_ok([_meili_row(1)])])
    result = _service(client, _fallback_returning([])).run(SearchQuery(text="书"))

    assert result.notice == NOTICE_SINGLE_CHAR
    assert result.degraded is False


def test_单字提示不覆盖放宽提示() -> None:
    """放宽与单字是两件独立的事实，可能同时成立，**两条提示都要给**。

    只给一条会让另一条消失，而用户看到的「结果很少且很杂」需要两句一起解释。
    """
    client = FakeClient([_ok([], total=0), _ok([_meili_row(1, 0.1)])])
    result = _service(client, _fallback_returning([])).run(SearchQuery(text="书"))

    assert result.relaxed is True
    assert result.notice is not None
    assert NOTICE_RELAXED in result.notice
    assert NOTICE_SINGLE_CHAR in result.notice
    assert result.degraded is False, "提示是真话，但服务没坏"


# ── 命中解析的边界 ─────────────────────────────────────────────


def test_命中字段为null时收成空值() -> None:
    """`summary` 在库里是 NULL 时会以 `null` 出现在响应里。
    `str(None)` 会变成 `"None"` 这四个字符——那是数据污染。"""
    client = FakeClient(
        [
            _ok(
                [
                    {
                        "id": 1,
                        "kind": "article",
                        "title": "t",
                        "summary": None,
                        "tags": None,
                        "author": None,
                        "read_status": None,
                        "saved_at": None,
                        "source_domain": None,
                    }
                ]
            )
        ]
    )
    result = _service(client, _fallback_returning([])).run(SearchQuery(text="x"))
    hit = result.hits[0]
    assert hit.summary == ""
    assert hit.tags == ()
    assert hit.title == "t"
    assert "None" not in (hit.summary + hit.source_domain + hit.saved_at)


def test_缺rankingScore时给0而不是None() -> None:
    client = FakeClient([_ok([{"id": 1, "title": "t"}])])
    result = _service(client, _fallback_returning([])).run(SearchQuery(text="x"))
    assert result.hits[0].ranking_score == 0.0


def test_estimatedTotalHits缺失时给None() -> None:
    """`None` 而不是 0：0 的意思是「一条都没有」，缺失的意思是「不知道」。"""
    client = FakeClient([{"hits": [_meili_row(1)]}])
    result = _service(client, _fallback_returning([])).run(SearchQuery(text="x"))
    assert result.estimated_total is None


def test_hits里有非字典项时跳过() -> None:
    client = FakeClient([{"hits": [_meili_row(1), "垃圾", None]}])
    result = _service(client, _fallback_returning([])).run(SearchQuery(text="x"))
    assert len(result.hits) == 1


def test_hits不是数组时按空处理() -> None:
    """`hits: null` 在真机上出现过（索引刚建、还没灌数据时）。

    ⚠️ 用 `min_score=0` 的查询避免触发放宽重查：本用例验的是「怎么解析
    一个畸形响应」，多给一次剧本会让断言里混进「重查了几次」这件事。
    """
    client = FakeClient([{"hits": None, "estimatedTotalHits": 0}])
    result = _service(client, _fallback_returning([]), min_score=0.0).run(
        SearchQuery(text="x")
    )
    assert result.hits == ()


# ── 索引 uid 与常量 ────────────────────────────────────────────


def test_service默认用config里的索引uid() -> None:
    from kb.search.config import INDEX_UID

    client = FakeClient([_ok([])])
    service = _service(client, _fallback_returning([]), min_score=0.0)
    service.run(SearchQuery(text="x"))
    assert client.calls[0][0] == INDEX_UID


def test_服务级min_score比查询级更宽松时以服务级为准() -> None:
    """`SearchQuery.min_score` 的 dataclass 默认值是 `config.MIN_SCORE`（0.30），
    所以调用方不传就会拿到 0.30。若直接用它，`SearchService(min_score=0.0)`
    会被静默忽略——那个构造参数看起来是「这个服务的默认阈值」，实际不起作用。

    `_effective_min_score` 取两者较小，两边的诉求都能表达：
    服务级配置能放宽、查询级 `minScore` 也能放宽。
    """
    client = FakeClient([_ok([_meili_row(1)])])
    _service(client, _fallback_returning([]), min_score=0.0).run(SearchQuery(text="x"))
    assert "rankingScoreThreshold" not in client.calls[0][1]


def test_查询级minScore比服务级更宽松时以查询级为准() -> None:
    """设计 §10 明写「查询参数 `minScore` 可覆盖，`0` 表示不截断」。"""
    client = FakeClient([_ok([_meili_row(1)])])
    _service(client, _fallback_returning([]), min_score=0.5).run(
        SearchQuery(text="x", min_score=0.0)
    )
    assert "rankingScoreThreshold" not in client.calls[0][1]


def test_分页常量符合契约() -> None:
    """`/api/search` 的约束：limit 1–50、offset 0–950。

    `MAX_OFFSET` 从 999 降到 950 是**与 `MAX_TOTAL_HITS` 联动**的结果
    （950 + 50 = 1000），不是独立调整。理由见 `config.MAX_OFFSET` 的注释：
    999 + 50 = 1049 会让最后 49 条被 Meili 静默截掉。
    """
    assert (DEFAULT_LIMIT, MAX_LIMIT, MAX_OFFSET) == (20, 50, 950)


def test_offset上限加limit上限不超过maxTotalHits() -> None:
    """三个分页常量是**一组**，必须互相校验（QA 抓到的问题）。

    `offset=999&limit=50` 要求 Meili 返回第 1000–1049 条。第一版
    `MAX_TOTAL_HITS = 1000`，越界部分被 Meili **静默截掉**——用户看到
    「翻到 1000 条之后就没有了」，且没有任何提示说是被上限切断的。

    这不是「当前会不会触发」的问题（个人库远低于这个量级），而是
    **两个常量各自定义、没有互相校验**。改任何一个而不同步其余就会红。
    """
    from kb.search.config import MAX_TOTAL_HITS

    assert MAX_OFFSET + MAX_LIMIT <= MAX_TOTAL_HITS, (
        f"offset 上限 {MAX_OFFSET} + limit 上限 {MAX_LIMIT} = "
        f"{MAX_OFFSET + MAX_LIMIT} 超过 maxTotalHits={MAX_TOTAL_HITS}，"
        f"最后 {MAX_OFFSET + MAX_LIMIT - MAX_TOTAL_HITS} 条请求会静默返回空"
    )

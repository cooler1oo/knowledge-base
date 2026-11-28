"""兜底 SQL 在真库的行为（`pg` 标记，本机无库时跳过，平板真库上跑）。

## 为什么必须有这一组

假连接永远碰不到真实 SQL 语义。本文件要验的四条全部只存在于真库里：

| 断言 | 假连接为什么验不了 |
|---|---|
| `ILIKE '%营销%'` 命中 title / summary / content_text / ocr_text / tags 五路 | 要 PG 真的执行 `ilike` 与 `exists` 子查询 |
| `field_hits` 的算术（命中 3 个字段 → 3） | 表达式由 PG 求值 |
| 软删条目（`deleted_at` 非空）不进结果 | `where` 谓词的真实效果 |
| `index_source_page` 的两页不重叠不遗漏 | 游标语义 `id > after_id` 由 PG 执行 |

2026-11-28 的教训正是这一类：本机 1021 passed 全绿，到平板真库才炸出 3 条红
（`mediaTruncated` 残留键）。只存在于 SQL 语义里的缺陷必须靠这一层抓。
"""

from __future__ import annotations

import pytest

from kb.search.models import SearchFilter, SearchQuery
from kb.store import jobs as jobs_store
from kb.store import search as search_store

NOW_EPOCH = 1795312800  # 2026-11-22 10:00 +08:00


@pytest.fixture
def conn(pg_cfg):
    """每个用例一个事务，结束回滚。"""
    from kb.store.db import connect

    c = connect(pg_cfg)
    c.run("begin")
    try:
        yield c
    finally:
        c.run("rollback")
        c.close()


def _kind(conn, kind: str = "article") -> str:
    """确保 `item_kinds` 里有这个类型（`items.kind` 有外键）。

    **不硬编码类型枚举**：查 `item_kinds` 表是真源，这里只是为了让用例
    能在任何库状态下跑——真实的 `item_kinds` 里已有 article/video/text 等预设，
    但一个干净的 `kb_test` 可能没有。用 rank=1 / sort_order=-99 保证
    这条夹具数据排在预设之后，不改动任何已有行的展示顺序。
    """
    conn.run(
        "insert into item_kinds (kind, display_name, is_preset, sort_order) "
        "values (:k, :k, false, -999) on conflict (kind) do nothing",
        k=kind,
    )
    return kind


def _item(
    conn,
    *,
    title: str = "",
    summary: str | None = None,
    content_text: str | None = None,
    ocr_text: str | None = None,
    kind: str = "article",
    status: str = "unread",
    saved_at_sql: str = "now()",
    deleted: bool = False,
) -> int:
    item_id = conn.run(
        "insert into items (kind, title, summary, content_text, ocr_text, status,"
        f" saved_at, deleted_at) values (:k, :t, :s, :c, :o, :st, {saved_at_sql},"
        " case when :gone then now() else null end) returning id",
        k=_kind(conn, kind),
        t=title,
        s=summary,
        c=content_text,
        o=ocr_text,
        st=status,
        # 参数名不能叫 `del`：那是 Python 关键字，`del=deleted` 是语法错误。
        gone=deleted,
    )[0][0]
    return int(str(item_id))


def _tag(conn, name: str, item_id: int) -> None:
    """给某条内容贴一个标签。标签名在 `tags` 表上唯一（`tags_sibling_name_uniq`），
    所以同一名字复用时不能盲 insert——`on conflict do nothing` + 回查。

    `kb_test` 库在多个用例之间共享（每个用例是独立事务但不 truncate），
    盲 insert 会在第二个用到同名标签的用例上撞唯一索引。
    """
    conn.run(
        "insert into tags (name) values (:n) "
        "on conflict (coalesce(parent_id, 0), lower(btrim(name))) do nothing",
        n=name,
    )
    tag_id = conn.run(
        "select id from tags where coalesce(parent_id, 0) = 0"
        " and lower(btrim(name)) = lower(btrim(:n))",
        n=name,
    )[0][0]
    conn.run(
        "insert into item_tags (item_id, tag_id, origin) values (:i, :t, 'user')"
        " on conflict do nothing",
        i=item_id,
        t=int(str(tag_id)),
    )


def _search(conn, *terms: str, **kwargs: object):
    return search_store.fallback_search(
        conn,
        terms=tuple(terms),
        limit=int(kwargs.get("limit", 20)),  # type: ignore[arg-type]
        offset=int(kwargs.get("offset", 0)),  # type: ignore[arg-type]
        filter=kwargs.get("filter", SearchFilter()),  # type: ignore[arg-type]
    )


# ── 五路字段都能被兜底命中 ─────────────────────────────────────


@pytest.mark.pg
def test_标题命中(conn) -> None:
    item_id = _item(conn, title="矩阵营销的三个误区")
    hits = _search(conn, "营销")
    assert [h.id for h in hits] == [item_id]


@pytest.mark.pg
def test_只命中摘要(conn) -> None:
    item_id = _item(conn, title="无关标题", summary="讲私域流量池怎么建")
    hits = _search(conn, "私域流量")
    assert [h.id for h in hits] == [item_id]


@pytest.mark.pg
def test_只命中正文(conn) -> None:
    item_id = _item(
        conn, title="随手记一条", content_text="今天试了下挂耳咖啡的冲泡比例"
    )
    hits = _search(conn, "挂耳咖啡")
    assert [h.id for h in hits] == [item_id]


@pytest.mark.pg
def test_只命中ocr文字(conn) -> None:
    item_id = _item(conn, title="无关标题", ocr_text="剪映卡点教学")
    hits = _search(conn, "剪映卡点")
    assert [h.id for h in hits] == [item_id]


@pytest.mark.pg
def test_只命中标签名(conn) -> None:
    item_id = _item(conn, title="无关标题")
    _tag(conn, "科普", item_id)
    hits = _search(conn, "科普")
    assert [h.id for h in hits] == [item_id]


# ── field_hits 的算术与排序 ───────────────────────────────────


@pytest.mark.pg
def test_field_hits是命中字段数(conn) -> None:
    """命中 title + summary + content_text 三个字段 → `field_hits = 3`。"""
    item_id = _item(
        conn,
        title="三体问题",
        summary="三体问题的物理设定",
        content_text="三体问题本质是三个天体的运动",
    )
    hits = _search(conn, "三体")
    assert len(hits) == 1
    assert hits[0].ranking_score == pytest.approx(3 / 5), (
        f"命中三个字段应得 3/5，实际 {hits[0].ranking_score}"
    )
    assert hits[0].id == item_id


@pytest.mark.pg
def test_命中字段多的排前面(conn) -> None:
    many = _item(conn, title="营销", summary="营销", content_text="营销")
    one = _item(conn, title="营销入门")
    hits = _search(conn, "营销")
    assert [h.id for h in hits] == [many, one], "field_hits 多的应排前面"


@pytest.mark.pg
def test_同一命中字段数时按saved_at倒序(conn) -> None:
    older = _item(conn, title="营销一", saved_at_sql="now() - interval '1 day'")
    newer = _item(conn, title="营销二")
    hits = _search(conn, "营销")
    assert [h.id for h in hits] == [newer, older]


# ── 多词 AND ──────────────────────────────────────────────────


@pytest.mark.pg
def test_多词之间是AND(conn) -> None:
    """「抖音 剪辑」要求两个词都出现在**某个**字段里（不要求同一字段）。"""
    both = _item(conn, title="抖音剪辑技巧")
    only_video = _item(conn, title="抖音直播带货")
    only_edit = _item(conn, title="剪辑软件对比")

    hits = _search(conn, "抖音", "剪辑")
    assert [h.id for h in hits] == [both], (
        f"多词应为 AND；只含一个词的 {only_video}/{only_edit} 不该出现"
    )


@pytest.mark.pg
def test_两个词分别命中不同字段也算(conn) -> None:
    """「抖音」在 title、「剪辑」在 content_text —— 这是 AND 的正常用法。"""
    item_id = _item(conn, title="短视频入门", content_text="抖音上的剪辑套路很多")
    hits = _search(conn, "抖音", "剪辑")
    assert [h.id for h in hits] == [item_id]


# ── 反向：不存在的词 ──────────────────────────────────────────


@pytest.mark.pg
def test_不存在的词返回空列表_R1(conn) -> None:
    """R1 反向测试在兜底路径上的对应断言。

    它是唯一能抓住「`q` 参数被丢掉、退化成列表接口」这类 bug 的用例——
    `q` 被丢时其它用例大多还是绿的（列表里也有那些条目）。
    """
    _item(conn, title="矩阵营销的三个误区")
    assert _search(conn, "量子纠缠") == []


@pytest.mark.pg
def test_空查询返回全部(conn) -> None:
    """空查询 = 浏览器模式。**不能返回空列表**：那样「点开搜索框不输入」
    会看到空页面。"""
    a = _item(conn, title="甲")
    b = _item(conn, title="乙")
    hits = _search(conn)
    assert {h.id for h in hits} == {a, b}


# ── 软删不进结果 ──────────────────────────────────────────────


@pytest.mark.pg
def test_软删条目不出现在兜底结果里(conn) -> None:
    alive = _item(conn, title="营销入门")
    _item(conn, title="营销进阶", deleted=True)
    hits = _search(conn, "营销")
    assert [h.id for h in hits] == [alive]


# ── LIKE 元字符转义 ──────────────────────────────────────────


@pytest.mark.pg
def test_百分号按字面匹配(conn) -> None:
    """不转义时 `%100%%` 会命中「1000」——那句话是「按字面匹配」的承诺就假了。"""
    target = _item(conn, title="涨了 100% 的方法")
    _item(conn, title="涨了 1000 的方法")
    hits = _search(conn, "100%")
    assert [h.id for h in hits] == [target]


@pytest.mark.pg
def test_下划线按字面匹配(conn) -> None:
    target = _item(conn, title="文件 a_b 的用法")
    _item(conn, title="文件 axb 的用法")
    hits = _search(conn, "a_b")
    assert [h.id for h in hits] == [target]


# ── 筛选维度 ──────────────────────────────────────────────────


@pytest.mark.pg
def test_按kind筛选(conn) -> None:
    video = _item(conn, title="营销视频", kind="video")
    _item(conn, title="营销文章", kind="article")
    hits = _search(conn, "营销", filter=SearchFilter(kinds=("video",)))
    assert [h.id for h in hits] == [video]


@pytest.mark.pg
def test_kind多值时是OR(conn) -> None:
    video = _item(conn, title="营销视频", kind="video")
    article = _item(conn, title="营销文章", kind="article")
    _item(conn, title="营销纯文字", kind="text")
    hits = _search(conn, "营销", filter=SearchFilter(kinds=("video", "article")))
    assert {h.id for h in hits} == {video, article}


@pytest.mark.pg
def test_标签多值时是AND(conn) -> None:
    """勾两个标签 = 「同时打了这两个标签」。改 OR 会筛出全库一半内容。"""
    both = _item(conn, title="营销一")
    _tag(conn, "营销", both)
    _tag(conn, "运营", both)
    only_one = _item(conn, title="营销二")
    _tag(conn, "营销", only_one)

    hits = _search(conn, "营销", filter=SearchFilter(tags=("营销", "运营")))
    assert [h.id for h in hits] == [both]


@pytest.mark.pg
def test_按read_status筛选(conn) -> None:
    unread = _item(conn, title="营销一", status="unread")
    _item(conn, title="营销二", status="read")
    hits = _search(conn, "营销", filter=SearchFilter(read_statuses=("unread",)))
    assert [h.id for h in hits] == [unread]


@pytest.mark.pg
def test_时间范围是半开区间(conn) -> None:
    """设计 §3.9 的核心判据在兜底路径上的对应断言。

    `from` 含、`to` **不含**。构造「11-22 23:59:59」与「11-23 00:00:00」
    两条，`to` 取 11-23 零点 → 前者命中、后者不命中。
    """
    last_second = _item(
        conn,
        title="营销一",
        saved_at_sql="timestamptz '2026-11-22 23:59:59+08'",
    )
    next_midnight = _item(
        conn,
        title="营销二",
        saved_at_sql="timestamptz '2026-11-23 00:00:00+08'",
    )
    day_start = 1795276800  # 2026-11-22 00:00 +08:00
    day_end = 1795363200  # 2026-11-23 00:00 +08:00

    hits = _search(
        conn,
        "营销",
        filter=SearchFilter(saved_from_ts=day_start, saved_to_ts=day_end),
    )
    ids = {h.id for h in hits}
    assert last_second in ids, "23:59:59 那条必须命中（from 含、to 不含）"
    assert next_midnight not in ids, "次日 00:00:00 那条不得命中（半开区间）"


@pytest.mark.pg
def test_时间筛选用绝对时刻不受会话时区影响(conn) -> None:
    """epoch 秒 → `to_timestamp` 给的是绝对时刻，与 `timestamptz` 比较时
    不受会话时区影响。这条用例本身在 UTC 与 PRC 会话下都该成立。"""
    item_id = _item(
        conn, title="营销一", saved_at_sql="timestamptz '2026-11-22 10:00:00+08'"
    )
    hits = _search(
        conn,
        "营销",
        filter=SearchFilter(saved_from_ts=NOW_EPOCH - 1, saved_to_ts=NOW_EPOCH + 1),
    )
    assert [h.id for h in hits] == [item_id]


# ── 分页 ──────────────────────────────────────────────────────


@pytest.mark.pg
def test_分页limit与offset生效(conn) -> None:
    first = _item(conn, title="营销一", saved_at_sql="now() - interval '3 day'")
    second = _item(conn, title="营销二", saved_at_sql="now() - interval '2 day'")
    third = _item(conn, title="营销三", saved_at_sql="now() - interval '1 day'")

    page1 = _search(conn, "营销", limit=2, offset=0)
    page2 = _search(conn, "营销", limit=2, offset=2)
    assert [h.id for h in page1] == [third, second]
    assert [h.id for h in page1 + page2] == [third, second, first]


# ── index_source_row ─────────────────────────────────────────


@pytest.mark.pg
def test_index_source_row返回完整文档(conn) -> None:
    item_id = _item(
        conn,
        title="矩阵营销的三个误区",
        summary="讲私域流量池怎么建",
        content_text="很多人做矩阵号第一件事是买量",
        ocr_text="",
        kind="article",
        status="unread",
        saved_at_sql="timestamptz '2026-11-22 10:00:00+08'",
    )
    _tag(conn, "营销", item_id)
    _tag(conn, "运营", item_id)

    doc = search_store.index_source_row(conn, item_id)
    assert doc is not None
    assert doc.id == item_id
    assert doc.title == "矩阵营销的三个误区"
    assert doc.summary == "讲私域流量池怎么建"
    assert doc.content_text == "很多人做矩阵号第一件事是买量"
    assert doc.ocr_text == ""
    assert doc.kind == "article"
    assert doc.read_status == "unread"
    assert doc.saved_at_ts == NOW_EPOCH
    # 标签名按 name 升序（`array_agg(... order by g.name)`）。
    # 排序口径是 PG 的 `C.UTF-8` collation = UTF-8 字节序，对 BMP 字符
    # 等价于 Unicode 码点序：营 U+8425 < 运 U+8FD0。
    assert doc.tags == ("营销", "运营"), f"标签顺序应由 order by 决定：{doc.tags}"


@pytest.mark.pg
def test_标签名顺序稳定(conn) -> None:
    """没有 `order by g.name` 时 `array_agg` 的顺序由执行计划决定，
    同一条内容的 `tags` 在两次 rebuild 之间可能不同——对账会误报不一致。"""
    item_id = _item(conn, title="t")
    for name in ("丙", "甲", "乙"):
        _tag(conn, name, item_id)

    first = search_store.index_source_row(conn, item_id)
    second = search_store.index_source_row(conn, item_id)
    assert first is not None and second is not None
    assert first.tags == second.tags
    assert first.tags == tuple(sorted(first.tags))


@pytest.mark.pg
def test_index_source_row对不存在的id返回None(conn) -> None:
    assert search_store.index_source_row(conn, 999_999_999) is None


@pytest.mark.pg
def test_index_source_row对软删条目返回None(conn) -> None:
    """软删条目留在索引里会让卡片点进去 404（`/api/items/{id}` 也带
    同一个谓词）。"""
    item_id = _item(conn, title="已删的", deleted=True)
    assert search_store.index_source_row(conn, item_id) is None


@pytest.mark.pg
def test_纯文字条目也能装配(conn) -> None:
    """`url` 为空的条目**也是内容**，也要能被搜到。设计 §4.4 明写
    `run_summarize` 跳过（`no_text`）也要入队 index，正是为这类条目。"""
    item_id = _item(conn, title="纯文字的标题", content_text="一段正文")
    doc = search_store.index_source_row(conn, item_id)
    assert doc is not None
    assert doc.title == "纯文字的标题"


@pytest.mark.pg
def test_没有标签时tags是空元组(conn) -> None:
    """`coalesce(t.names, '{}')` 不能省：没有标签时 `array_agg` 返回 NULL，
    不 coalesce 会让 `tuple(None)` 抛 TypeError。"""
    item_id = _item(conn, title="无标签")
    doc = search_store.index_source_row(conn, item_id)
    assert doc is not None
    assert doc.tags == ()


# ── index_source_page ────────────────────────────────────────


@pytest.mark.pg
def test_index_source_page按游标分页不重叠不遗漏(conn) -> None:
    """设计 §9 T03 的判据原文：3 条数据取 `limit=2` 验两页。"""
    ids = [_item(conn, title=f"第 {i} 条") for i in range(3)]

    page1 = search_store.index_source_page(conn, after_id=0, limit=2)
    assert len(page1) == 2
    page2 = search_store.index_source_page(conn, after_id=page1[-1].id, limit=2)
    assert len(page2) == 1
    page3 = search_store.index_source_page(conn, after_id=page2[-1].id, limit=2)
    assert page3 == []

    got = [d.id for d in page1] + [d.id for d in page2]
    assert got == sorted(ids), f"两页拼起来应等于全部 id 且有序，实际 {got}"


@pytest.mark.pg
def test_index_source_page跳过软删条目(conn) -> None:
    alive = _item(conn, title="在的")
    _item(conn, title="删了", deleted=True)
    page = search_store.index_source_page(conn, after_id=0, limit=10)
    assert [d.id for d in page] == [alive]


@pytest.mark.pg
def test_index_source_page按id升序(conn) -> None:
    ids = [_item(conn, title=f"第 {i} 条") for i in range(4)]
    page = search_store.index_source_page(conn, after_id=0, limit=10)
    assert [d.id for d in page] == sorted(ids)


# ── jobs.blocking_upstream ────────────────────────────────────


@pytest.mark.pg
def test_blocking_upstream的running上游被算作阻塞(conn) -> None:
    """设计 §4.2：`state='running'` 也算阻塞——上游正在跑时索引会写出半成品。"""
    item_id = _item(conn, title="t")
    conn.run(
        "insert into jobs (item_id, step, state) values (:i, 'summarize', 'running')",
        i=item_id,
    )
    index_job = conn.run(
        "insert into jobs (item_id, step, state) values (:i, 'index', 'running')"
        " returning id",
        i=item_id,
    )[0][0]

    blocked = jobs_store.blocking_upstream(
        conn,
        item_id,
        int(str(index_job)),
        steps=("fetch", "summarize", "tag"),
        soon_seconds=90.0,
    )
    assert blocked == "summarize"


@pytest.mark.pg
def test_blocking_upstream的90秒内到期的pending上游被算作阻塞(conn) -> None:
    item_id = _item(conn, title="t")
    conn.run(
        "insert into jobs (item_id, step, state, run_after) "
        "values (:i, 'tag', 'pending', now() + interval '30 seconds')",
        i=item_id,
    )
    index_job = conn.run(
        "insert into jobs (item_id, step, state) values (:i, 'index', 'running')"
        " returning id",
        i=item_id,
    )[0][0]

    blocked = jobs_store.blocking_upstream(
        conn,
        item_id,
        int(str(index_job)),
        steps=("fetch", "summarize", "tag"),
        soon_seconds=90.0,
    )
    assert blocked == "tag"


@pytest.mark.pg
def test_blocking_upstream的30分钟后到期的上游不阻塞(conn) -> None:
    """这是设计 §4.2 对 §6.4 的实质修正：AI 未配置时 `summarize` 被 defer 到
    30 分钟后，状态仍是 `pending`。字面执行「任何 pending 上游都等」的话，
    新内容半小时内搜不到——用户对这一现象的唯一解释是「搜索坏了」。"""
    item_id = _item(conn, title="t")
    conn.run(
        "insert into jobs (item_id, step, state, run_after) "
        "values (:i, 'summarize', 'pending', now() + interval '1800 seconds')",
        i=item_id,
    )
    index_job = conn.run(
        "insert into jobs (item_id, step, state) values (:i, 'index', 'running')"
        " returning id",
        i=item_id,
    )[0][0]

    blocked = jobs_store.blocking_upstream(
        conn,
        item_id,
        int(str(index_job)),
        steps=("fetch", "summarize", "tag"),
        soon_seconds=90.0,
    )
    assert blocked is None, "被 defer 到 30 分钟后的上游不该阻塞索引"


@pytest.mark.pg
def test_blocking_upstream不把自己算成阻塞(conn) -> None:
    """`id <> :j` 不能省：自己当前是 `running`，会把自己判成阻塞上游，
    无限推迟。"""
    item_id = _item(conn, title="t")
    index_job = conn.run(
        "insert into jobs (item_id, step, state) values (:i, 'index', 'running')"
        " returning id",
        i=item_id,
    )[0][0]

    blocked = jobs_store.blocking_upstream(
        conn,
        item_id,
        int(str(index_job)),
        steps=("fetch", "summarize", "tag", "index"),
        soon_seconds=90.0,
    )
    assert blocked is None


@pytest.mark.pg
def test_blocking_upstream只看给定步骤集合(conn) -> None:
    """`ocr` 与 `index` 不在 `steps` 里时不该阻塞。"""
    item_id = _item(conn, title="t")
    conn.run(
        "insert into jobs (item_id, step, state) values (:i, 'ocr', 'running')",
        i=item_id,
    )
    index_job = conn.run(
        "insert into jobs (item_id, step, state) values (:i, 'index', 'running')"
        " returning id",
        i=item_id,
    )[0][0]

    blocked = jobs_store.blocking_upstream(
        conn,
        item_id,
        int(str(index_job)),
        steps=("fetch", "summarize", "tag"),
        soon_seconds=90.0,
    )
    assert blocked is None


# ── summaries_by_ids / known_kinds ───────────────────────────


@pytest.mark.pg
def test_summaries_by_ids一次批量补状态(conn) -> None:
    """API 层要用一次 `where id = any(...)` 补全 status，不做 N+1。"""
    from kb.store import items as items_store

    item_id = _item(conn, title="t")
    conn.run(
        "insert into jobs (item_id, step, state) values (:i, 'fetch', 'done')",
        i=item_id,
    )
    found = items_store.summaries_by_ids(conn, [item_id, 999_999_999])
    assert len(found) == 1, "库里已删或不存在的那条不该出现在结果里"
    assert found[0].id == item_id
    assert found[0].status == "done", "status 由 jobs 聚合现算"


@pytest.mark.pg
def test_summaries_by_ids对软删条目不返回(conn) -> None:
    item_id = _item(conn, title="删了", deleted=True)
    from kb.store import items as items_store

    assert items_store.summaries_by_ids(conn, [item_id]) == []


@pytest.mark.pg
def test_summaries_by_ids空列表零查询结果(conn) -> None:
    from kb.store import items as items_store

    assert items_store.summaries_by_ids(conn, []) == []


@pytest.mark.pg
def test_known_kinds来自item_kinds表(conn) -> None:
    """`kind` 校验必须走这张表，不许硬编码类型枚举（`items.kind` 有外键，
    真实类型集合由数据决定）。"""
    from kb.store import items as items_store

    kinds = items_store.known_kinds(conn)
    assert "article" in kinds or _kind(conn, "article") in items_store.known_kinds(conn)
    assert isinstance(kinds, tuple)
    assert all(isinstance(k, str) for k in kinds)


@pytest.mark.pg
def test_known_kinds包含预设类型(conn) -> None:
    """迁移会写入预设类型（article / video / text 等）。这条断言的是
    「表里有什么就返回什么」，不假设具体有哪些。"""
    from kb.store import items as items_store

    conn.run(
        "insert into item_kinds (kind, display_name, is_preset) "
        "values ('zz_probe', '探针', false) on conflict do nothing"
    )
    assert "zz_probe" in items_store.known_kinds(conn)


# ── SearchQuery 端到端串起来 ─────────────────────────────────


@pytest.mark.pg
def test_fallback_for把SearchQuery翻成SQL参数(conn) -> None:
    """`fallback_for` 是装配层的适配器：把单个 `SearchQuery` 摊成
    `fallback_search` 的关键字参数。这条验「切词规则只有一处」。"""
    item_id = _item(conn, title="抖音剪辑技巧")
    run = search_store.fallback_for(conn)
    hits = run(SearchQuery(text="抖音 剪辑", limit=10, offset=0))
    assert [h.id for h in hits] == [item_id]


@pytest.mark.pg
def test_fallback_for带筛选(conn) -> None:
    video = _item(conn, title="抖音剪辑", kind="video")
    _item(conn, title="抖音剪辑", kind="article")
    run = search_store.fallback_for(conn)
    hits = run(
        SearchQuery(
            text="抖音",
            limit=10,
            offset=0,
            filter=SearchFilter(kinds=("video",)),
        )
    )
    assert [h.id for h in hits] == [video]

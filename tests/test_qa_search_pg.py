"""QA 独立验收的**真库探测组**：`pg` 标记，本机跳过，平板真库上跑。

## 与 `tests/test_store_search_pg.py` 的分工

那一组验「兜底 SQL 能按五路字段命中」以及 `blocking_upstream` 的三条**宽泛**
判据（30 秒阻塞、1800 秒不阻塞）。本组只打**边界的那一厘米**：

| 本组断言 | 既有用例的缺口 |
|---|---|
| `run_after = now() + 89s` 阻塞、`+91s` 不阻塞 | 既有用例用 30s / 1800s 两个远端点，测不出阈值是 90 还是 900 |
| 连续 `defer` 3 次后 `attempts` **仍为 0**（读真行） | 既有用例断言「SQL 文本里不出现 attempts」——那是**文本**判据，`attempts = attempts` 这种写法照样能过 |
| `from=to=同一天` 的四个边界时刻（含 23:59:59 / 不含次日 00:00:00） | 既有 API 用例只断言「filter 串里是 `>=` 和 `<`」，没验 PG 侧真的这样筛 |
| 闰日 2028-02-29、跨年 2026-12-31 | 一处都没有 |
| `from > to`（反着给） | 一处都没有 |
| `LIKE` 元字符 `%` `_` 在真库的转义效果 | 一处都没有（只在纯函数层验了 `_like_pattern` 的字符串形状） |

## 为什么这些只能真库验

`interval '1 second' * :soon` 的比较、`to_timestamp()` 与 `timestamptz` 的比较、
`ilike ... escape` 的默认转义符、`date.fromordinal` 的跨月跨年——全部是 PG 或
标准库的语义，假连接一律照我们的假设返回。
"""

from __future__ import annotations

import pytest

from kb.api.search import parse_date_bound
from kb.search.models import SearchFilter
from kb.store import jobs as jobs_store
from kb.store import search as search_store


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
    content_text: str | None = None,
    kind: str = "article",
    saved_at_sql: str = "now()",
) -> int:
    item_id = conn.run(
        "insert into items (kind, title, content_text, status, saved_at)"
        f" values (:k, :t, :c, 'unread', {saved_at_sql}) returning id",
        k=_kind(conn, kind),
        t=title,
        c=content_text,
    )[0][0]
    return int(str(item_id))


def _search(conn, *terms: str, **kwargs: object):
    return search_store.fallback_search(
        conn,
        terms=tuple(terms),
        limit=int(kwargs.get("limit", 50)),  # type: ignore[arg-type]
        offset=int(kwargs.get("offset", 0)),  # type: ignore[arg-type]
        filter=kwargs.get("filter", SearchFilter()),  # type: ignore[arg-type]
    )


def _filter_for(conn, *, raw_from: str | None, raw_to: str | None) -> SearchFilter:
    """用**API 真实的**换算函数构造 filter，再交给真实的兜底 SQL。

    不手写 epoch：手写等于把被测的换算逻辑复制进用例，换算出错时两边一起错。
    """
    return SearchFilter(
        saved_from_ts=(
            None if not raw_from else parse_date_bound(raw_from, is_upper=False)
        ),
        saved_to_ts=None if not raw_to else parse_date_bound(raw_to, is_upper=True),
    )


# ══════════════════════════════════════════════════════════════
# 1. from / to 的日界（设计 §3.9）
# ══════════════════════════════════════════════════════════════


@pytest.mark.pg
def test_from与to都是11月22日时只命中22日当天(conn) -> None:
    """`from=to=2026-11-22` 必须构成「11-22 全天」，四个边界时刻各一条。

    这是设计 §3.9 那四条守卫断言的**真库版**。既有 API 用例只验到 filter 串的
    形状（`>=` / `<`），没验 PG 拿到这两个数之后真的这样筛。
    """
    before = _item(
        conn,
        title="前一天最后一秒",
        saved_at_sql="'2026-11-21 23:59:59+08'::timestamptz",
    )
    day_start = _item(
        conn, title="当天第一秒", saved_at_sql="'2026-11-22 00:00:00+08'::timestamptz"
    )
    day_end = _item(
        conn, title="当天最后一秒", saved_at_sql="'2026-11-22 23:59:59+08'::timestamptz"
    )
    after = _item(
        conn, title="次日第一秒", saved_at_sql="'2026-11-23 00:00:00+08'::timestamptz"
    )

    hits = _search(
        conn, filter=_filter_for(conn, raw_from="2026-11-22", raw_to="2026-11-22")
    )
    ids = {h.id for h in hits}

    assert day_start in ids, "11-22 00:00:00 属于 11-22，`from` 必须是 >=（含）"
    assert day_end in ids, "11-22 23:59:59 属于 11-22，`to` 取次日零点的理由就在这条"
    assert before not in ids, "11-21 23:59:59 不属于 11-22"
    assert after not in ids, "11-23 00:00:00 不属于 11-22，半开区间不能收进来"


@pytest.mark.pg
def test_只给to时是这一天之前(conn) -> None:
    """只给 `to` = 「到这一天结束为止」，不限下界。"""
    old = _item(
        conn, title="很早", saved_at_sql="'2020-01-01 00:00:00+08'::timestamptz"
    )
    same_day = _item(
        conn, title="当天", saved_at_sql="'2026-11-22 12:00:00+08'::timestamptz"
    )
    later = _item(
        conn, title="之后", saved_at_sql="'2026-11-25 12:00:00+08'::timestamptz"
    )

    hits = _search(conn, filter=_filter_for(conn, raw_from=None, raw_to="2026-11-22"))
    ids = {h.id for h in hits}

    assert old in ids, "只给 to 时不该有下界"
    assert same_day in ids
    assert later not in ids


@pytest.mark.pg
def test_只给from时是这一天之后(conn) -> None:
    """只给 `from` = 「从这一天开始」，不限上界。"""
    earlier = _item(
        conn, title="之前", saved_at_sql="'2026-11-21 12:00:00+08'::timestamptz"
    )
    same_day = _item(
        conn, title="当天", saved_at_sql="'2026-11-22 00:00:00+08'::timestamptz"
    )
    late = _item(
        conn, title="很久以后", saved_at_sql="'2030-01-01 00:00:00+08'::timestamptz"
    )

    hits = _search(conn, filter=_filter_for(conn, raw_from="2026-11-22", raw_to=None))
    ids = {h.id for h in hits}

    assert earlier not in ids, "11-21 不属于「11-22 之后」"
    assert same_day in ids, "当天 00:00:00 属于「11-22 之后」（下界含）"
    assert late in ids, "只给 from 时不该有上界"


@pytest.mark.pg
def test_闰日2028年2月29日构成一整天(conn) -> None:
    """闰日是 `date.fromordinal(+1)` 最容易走样的地方。

    2028 是闰年，2-29 的次日是 3-1 而不是 2-30。若 `to` 的次日算术写成
    「day+1 然后非法就落到月初」，会静默把 3-1 也算进来。
    """
    leap_start = _item(
        conn, title="闰日第一秒", saved_at_sql="'2028-02-29 00:00:00+08'::timestamptz"
    )
    leap_end = _item(
        conn, title="闰日最后一秒", saved_at_sql="'2028-02-29 23:59:59+08'::timestamptz"
    )
    march = _item(
        conn, title="3月1日", saved_at_sql="'2028-03-01 00:00:00+08'::timestamptz"
    )
    feb28 = _item(
        conn, title="2月28日", saved_at_sql="'2028-02-28 23:59:59+08'::timestamptz"
    )

    hits = _search(
        conn, filter=_filter_for(conn, raw_from="2028-02-29", raw_to="2028-02-29")
    )
    ids = {h.id for h in hits}

    assert leap_start in ids
    assert leap_end in ids
    assert march not in ids, "2028-02-29 的次日是 03-01，不能把它算进 2-29"
    assert feb28 not in ids


@pytest.mark.pg
def test_跨年to为12月31日时收进当天最后一秒(conn) -> None:
    """跨年：`to=2026-12-31` 的次日是 2027-01-01。"""
    last_second = _item(
        conn,
        title="跨年前最后一秒",
        saved_at_sql="'2026-12-31 23:59:59+08'::timestamptz",
    )
    new_year = _item(
        conn, title="元旦第一秒", saved_at_sql="'2027-01-01 00:00:00+08'::timestamptz"
    )

    hits = _search(
        conn, filter=_filter_for(conn, raw_from="2026-12-31", raw_to="2026-12-31")
    )
    ids = {h.id for h in hits}

    assert last_second in ids
    assert new_year not in ids, "跨年时 to 的次日必须落到 2027-01-01"


@pytest.mark.pg
def test_from大于to时返回0条而不是报错(conn) -> None:
    """反着给（`from=11-25&to=11-22`）：返回空集，不是 500。

    设计没写这条。当前行为是 `saved_at >= A and saved_at < B` 且 A > B → 恒假
    → 0 条。**这个结果对用户是可接受的**（他看到空结果），比报错好；但
    「空结果」与「参数写反了」在界面上不可区分。记录为设计缺口，不是实现 bug。
    """
    _item(conn, title="某条", saved_at_sql="'2026-11-22 12:00:00+08'::timestamptz")

    hits = _search(
        conn, filter=_filter_for(conn, raw_from="2026-11-25", raw_to="2026-11-22")
    )

    assert hits == [], "from > to 应当是空集（当前实现），不是异常"


# ══════════════════════════════════════════════════════════════
# 2. blocking_upstream 的 90 秒边界（真 SQL）
# ══════════════════════════════════════════════════════════════


def _index_job(conn, item_id: int) -> int:
    job_id = conn.run(
        "insert into jobs (item_id, step, state) values (:i, 'index', 'running')"
        " returning id",
        i=item_id,
    )[0][0]
    return int(str(job_id))


@pytest.mark.pg
def test_上游在89秒后到期算阻塞(conn) -> None:
    """`run_after = now() + 89s` 落在 90 秒窗口内 → 阻塞。

    既有用例用 30 秒（远在窗口内）与 1800 秒（远在窗口外）两个端点，
    测不出阈值究竟是 90 还是 900——两端都满足。
    """
    item_id = _item(conn, title="t")
    conn.run(
        "insert into jobs (item_id, step, state, run_after) "
        "values (:i, 'summarize', 'pending', now() + interval '89 seconds')",
        i=item_id,
    )

    blocked = jobs_store.blocking_upstream(
        conn,
        item_id,
        _index_job(conn, item_id),
        steps=("fetch", "summarize", "tag"),
        soon_seconds=90.0,
    )

    assert blocked == "summarize"


@pytest.mark.pg
def test_上游在91秒后到期不阻塞(conn) -> None:
    """`run_after = now() + 91s` 落在窗口外 → 不阻塞。

    与上一条成对：它们一起把阈值夹在 (89, 91] 里，也就是 90。
    """
    item_id = _item(conn, title="t")
    conn.run(
        "insert into jobs (item_id, step, state, run_after) "
        "values (:i, 'summarize', 'pending', now() + interval '91 seconds')",
        i=item_id,
    )

    blocked = jobs_store.blocking_upstream(
        conn,
        item_id,
        _index_job(conn, item_id),
        steps=("fetch", "summarize", "tag"),
        soon_seconds=90.0,
    )

    assert blocked is None, "91 秒后到期的上游不该阻塞索引"


@pytest.mark.pg
def test_连续推迟三次后attempts仍为0(conn) -> None:
    """设计 §10：「推迟动作一律 `jobs.defer`，`attempts` 不变」。

    既有用例 `test_推迟的SQL里不出现attempts` 是**文本**判据——把
    `attempts = attempts` 或 `attempts = 0` 写进 SQL 它照样绿，而后者会把
    「推迟了 3 次」谎报成「试了 0 次」，用户界面上看不出这条内容被反复推迟。

    本用例读**真行**：推 3 次，`attempts` 必须还是 0，`state` 必须是 pending。
    """
    item_id = _item(conn, title="t")
    job_id = conn.run(
        "insert into jobs (item_id, step, state) values (:i, 'index', 'running')"
        " returning id",
        i=item_id,
    )[0][0]
    job_id = int(str(job_id))

    for _ in range(3):
        jobs_store.defer(conn, job_id, 30.0, "已推迟：等待 summarize 完成")

    row = conn.run(
        "select state, attempts, run_after > now() from jobs where id = :j", j=job_id
    )[0]

    assert int(str(row[1])) == 0, f"推迟 3 次后 attempts 应仍为 0，实际 {row[1]}"
    assert str(row[0]) == "pending", f"推迟后状态必须是 pending，实际 {row[0]}"
    assert row[2] is True, "推迟后 run_after 必须在将来"


@pytest.mark.pg
def test_推迟不会把job标成failed(conn) -> None:
    """反向保护：推迟 5 次也不该让 job 变成 failed（那会让条目显示「处理失败」）。"""
    item_id = _item(conn, title="t")
    job_id = conn.run(
        "insert into jobs (item_id, step, state) values (:i, 'index', 'running')"
        " returning id",
        i=item_id,
    )[0][0]
    job_id = int(str(job_id))

    for _ in range(5):
        jobs_store.defer(conn, job_id, 30.0, "已推迟：索引服务不可达")

    state = conn.run("select state from jobs where id = :j", j=job_id)[0][0]
    assert str(state) == "pending", f"推迟不该判死，实际 {state}"


# ══════════════════════════════════════════════════════════════
# 3. LIKE 元字符在真库的转义（兜底路径的「按字面匹配」承诺）
# ══════════════════════════════════════════════════════════════


@pytest.mark.pg
def test_百分号被转义后不匹配任意字符串(conn) -> None:
    """`_like_pattern` 把 `%` 转成 `\\%`，但没写 `escape` 子句。

    PG 的 LIKE 默认转义符**就是**反斜杠，所以应该成立——但这是假设，
    必须真库验。不成立时搜「100%」会命中「1000」，而 `notice` 的原话是
    「结果按字面匹配」。
    """
    literal = _item(conn, title="转化率 100% 的做法")
    decoy = _item(conn, title="月薪 1000 的方案")

    hits = _search(conn, "100%")
    ids = {h.id for h in hits}

    assert literal in ids, "含字面「100%」的条目应被命中"
    assert decoy not in ids, "「100%」被当成通配符了：命中了「1000」"


@pytest.mark.pg
def test_下划线被转义后不匹配任意单字符(conn) -> None:
    literal = _item(conn, title="命名规范 a_b")
    decoy = _item(conn, title="命名规范 axb")

    hits = _search(conn, "a_b")
    ids = {h.id for h in hits}

    assert literal in ids
    assert decoy not in ids, "「a_b」里的下划线被当成通配符了"


@pytest.mark.pg
def test_反斜杠被转义后能按字面匹配(conn) -> None:
    literal = _item(conn, title="路径 C:\\Users\\kb")

    hits = _search(conn, "C:\\Users")
    ids = {h.id for h in hits}

    assert literal in ids, "反斜杠既不该被吃掉也不该被当成转义符"


# ══════════════════════════════════════════════════════════════
# 4. 浏览器模式的 rankingScore（`_hit_from_row` 的 docstring 与实现的出入）
# ══════════════════════════════════════════════════════════════


@pytest.mark.pg
def test_空查询时fallback的rankingScore是0(conn) -> None:
    """`_hit_from_row` 的 docstring 写「不存在 0——能出现在结果里说明至少命中
    一个字段」。浏览器模式（空 `q`）下这句话不成立：`patterns` 是空数组，
    `field_hits` 恒为 0，于是每条的 `rankingScore` 都是 0.0。

    后果很轻（浏览器模式本来就不该展示分数），但它让「0 分 = 不可能出现」
    这条读代码时的直觉失效。记录用，不判失败——断言的是**当前实际行为**。
    """
    _item(conn, title="某条内容")

    hits = _search(conn)  # 无 terms = 浏览器模式

    assert hits, "浏览器模式要返回条目，不能是空列表"
    assert all(h.ranking_score == 0.0 for h in hits), (
        "空查询下 field_hits 恒为 0，rankingScore 应为 0.0"
    )


@pytest.mark.pg
def test_软删条目不出现在兜底结果里(conn) -> None:
    """软删条目（`deleted_at` 非空）不该被搜到：点进去 404。"""
    alive = _item(conn, title="营销入门")
    gone = _item(conn, title="营销进阶")
    conn.run("update items set deleted_at = now() where id = :i", i=gone)

    hits = _search(conn, "营销")
    ids = {h.id for h in hits}

    assert alive in ids
    assert gone not in ids, "软删条目仍在兜底结果里"

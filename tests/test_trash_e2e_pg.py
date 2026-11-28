"""M4-03 真库端到端：软删除 + 回收站（技术方案判据 1–6）。

连不上 Postgres 时整个模块 skip。平板上跑：

    KB_PG_HOST=127.0.0.1 KB_PG_USER=u0_a201 KB_PG_DATABASE=kb_test \
    python -m pytest tests/test_trash_e2e_pg.py -q

## 为什么这些必须在真库跑

假连接（`tests/test_trash_store.py`）验的是判定顺序与 SQL 文本，它**永远
不会**发现这几类错：

1. `on conflict (item_id, step) where state in (...)` 需要**部分唯一索引**
   `jobs_pending_uniq` 存在——没有它 PG 报 `there is no unique or exclusion
   constraint matching the ON CONFLICT`
2. `items_url_norm_active_uniq` 是**部分唯一索引**（`where deleted_at is null`），
   恢复时的 23505 只在真库上才会真的发生
3. `make_interval(days => cast(:days as int))` 的类型推断
4. `(i.deleted_at, i.id) < (cast(:before_ts as timestamptz), cast(:before_id as bigint))`
   这种行值比较的语法
5. `delete from items ... returning id` 在 pg8000 上返回什么形状

2026-11-28 的检索层就是「本机全绿、真库炸出 3 条红」之后才补的这一层。
"""

from __future__ import annotations

import pytest

from kb.store import items as items_store
from kb.store.db import connect
from kb.store.migrate import discover, migrate


@pytest.fixture(scope="session")
def fresh_db(pg_cfg):
    admin = connect(pg_cfg.with_database("postgres"))
    admin.run(f'drop database if exists "{pg_cfg.database}" with (force)')
    admin.run(f'create database "{pg_cfg.database}"')
    admin.close()

    conn = connect(pg_cfg)
    try:
        migrate(conn, discover())
    finally:
        conn.close()
    return pg_cfg


@pytest.fixture
def conn(fresh_db):
    """每个用例一个事务，结束回滚。"""
    c = connect(fresh_db)
    c.run("begin")
    try:
        yield c
    finally:
        c.run("rollback")
        c.close()


def _create(conn, url: str = "https://v.douyin.com/abc/", *, kind: str = "link") -> int:
    """建一条 URL 条目，返回 id。"""
    result = items_store.create_item(
        conn,
        kind=kind,
        url=url,
        url_norm=items_store.normalize_url(url),
        title="一条内容",
        source_domain=items_store.host_of(url),
        content_text=None,
        step="fetch",
    )
    return result.id


# ── 判据 1：软删真的盖了戳 ──────────────────────────────────


@pytest.mark.pg
def test_软删后deleted_at非空且入队index(conn) -> None:
    item_id = _create(conn)

    result = items_store.soft_delete(conn, item_id)

    assert result.status is items_store.DeleteStatus.DELETED
    rows = conn.run("select deleted_at from items where id = :i", i=item_id)
    assert rows[0][0] is not None, "deleted_at 没有被盖上"
    # CTE 里顺手入队的 index job 必须真的存在
    jobs = conn.run(
        "select step, state from jobs where item_id = :i and step = 'index'",
        i=item_id,
    )
    assert jobs, "软删没有入队 index"


# ── 判据 2：连删两次不续期 ──────────────────────────────────


@pytest.mark.pg
def test_连删两次deleted_at逐字相同(conn) -> None:
    """**判据 2**：第二次删除不能把戳写成新的 `now()`。

    SQL 里的 `and deleted_at is null` 是这件事的根。没有它，用户连点两次
    删除就把保留期重新计时——现象是「回收站里有一条三个月前删的东西一直不清」，
    从任何日志里都看不出来。

    断言**逐字相同**而不是「相差很小」：`now()` 在同一事务里是固定值
    （`transaction_timestamp`），所以两次调用本就会得到同一个值——那意味着
    只看「值相近」验不出守卫缺失。真正的判据是「第二次有没有执行 UPDATE」，
    用 `status is ALREADY_DELETED` + 时间戳相等一起断言。
    """
    item_id = _create(conn)

    first = items_store.soft_delete(conn, item_id)
    assert first.status is items_store.DeleteStatus.DELETED

    # 在同一事务里 `now()` 不变，所以为了让「续期」可观测，直接改一下戳
    # 模拟「这条一个月前删的」。
    conn.run(
        "update items set deleted_at = now() - interval '30 days' where id = :i",
        i=item_id,
    )
    old_rows = conn.run("select deleted_at from items where id = :i", i=item_id)
    old_ts = old_rows[0][0]

    second = items_store.soft_delete(conn, item_id)

    assert second.status is items_store.DeleteStatus.ALREADY_DELETED
    new_rows = conn.run("select deleted_at from items where id = :i", i=item_id)
    assert new_rows[0][0] == old_ts, (
        f"deleted_at 被续期了：{old_ts} → {new_rows[0][0]}。"
        "说明 SQL 里少了 `and deleted_at is null`"
    )
    assert second.deleted_at == old_ts


@pytest.mark.pg
def test_连删两次不重复入队index(conn) -> None:
    item_id = _create(conn)
    items_store.soft_delete(conn, item_id)
    items_store.soft_delete(conn, item_id)

    jobs = conn.run(
        "select count(*) from jobs where item_id = :i and step = 'index'",
        i=item_id,
    )
    assert int(str(jobs[0][0])) == 1, "第二次删除不该再加一个 index job"


# ── 判据 3：恢复冲突 409 ────────────────────────────────────


@pytest.mark.pg
def test_删A存同URL得B后恢复A冲突并带B的id(conn) -> None:
    """**判据 3**：部分唯一索引 `items_url_norm_active_uniq` 只在真库上生效。

    A 软删 → 存同 URL 得到 B（合法，因为 A 不在索引范围里）→ 恢复 A 必须
    被挡住并报出 B 的 id。
    """
    a = _create(conn, "https://example.com/same")
    assert items_store.soft_delete(conn, a).status is items_store.DeleteStatus.DELETED

    b = _create(conn, "https://example.com/same")
    assert b != a

    result = items_store.restore_item(conn, a)

    assert result.status is items_store.RestoreStatus.CONFLICT
    assert result.conflict_item_id == b, (
        f"冲突 id 是 {result.conflict_item_id}，应为 {b}"
    )
    # A 仍应在回收站里
    rows = conn.run("select deleted_at from items where id = :i", i=a)
    assert rows[0][0] is not None


@pytest.mark.pg
def test_恢复成功后可被列表查到且入队index(conn) -> None:
    item_id = _create(conn)
    items_store.soft_delete(conn, item_id)

    result = items_store.restore_item(conn, item_id)

    assert result.status is items_store.RestoreStatus.RESTORED
    rows = conn.run("select deleted_at from items where id = :i", i=item_id)
    assert rows[0][0] is None
    jobs = conn.run(
        "select count(*) from jobs where item_id = :i and step = 'index'",
        i=item_id,
    )
    assert int(str(jobs[0][0])) >= 1


@pytest.mark.pg
def test_两条同URL都删后恢复其中一条成功(conn) -> None:
    """冲突只针对**活条目**。两条都在回收站里时恢复一条是合法的。"""
    a = _create(conn, "https://example.com/x")
    items_store.soft_delete(conn, a)
    b = _create(conn, "https://example.com/x")
    items_store.soft_delete(conn, b)

    result = items_store.restore_item(conn, a)
    assert result.status is items_store.RestoreStatus.RESTORED


# ── 判据 4：搜索索引真的不返回这条（真机验，这里验库侧）────


@pytest.mark.pg
def test_软删后index_source_row返回None(conn) -> None:
    """`index_source_row` 的 `where deleted_at is null` 是索引侧的唯一开关。

    它的返回值决定 `run_index` 走 upsert 还是 delete_ids（T03 改的那条）。
    这条在真库上验，是因为它是「软删 → 搜索不到」整条链路的**库侧终点**：
    库这一层不返回，后面 Meili 才可能不返回。
    """
    from kb.store import search as search_store

    item_id = _create(conn)
    assert search_store.index_source_row(conn, item_id) is not None

    items_store.soft_delete(conn, item_id)

    assert search_store.index_source_row(conn, item_id) is None, (
        "软删后 index_source_row 仍返回文档，说明它的 deleted_at 过滤没生效"
    )


# ── 判据 5：回收站列表与游标 ────────────────────────────────


@pytest.mark.pg
def test_回收站列表按删除时间倒序且days_left非负(conn) -> None:
    a = _create(conn, "https://example.com/1")
    b = _create(conn, "https://example.com/2")
    items_store.soft_delete(conn, a)
    items_store.soft_delete(conn, b)
    # 让 b 的删除时刻晚一点，保证排序可判。
    # ⚠️ **往后推 1 秒是我写错过的地方**：`deleted_at` 变成未来时刻后，
    # `purge_at - now()` 就是 30 天零 1 秒，`days_left` 向上取整成 31，
    # 超出保留期上限。真库当场抓到（假连接给固定值 30，掩盖了这一点）。
    # 正确做法是两个都推到过去，只让相对先后不同。
    conn.run(
        "update items set deleted_at = now() - interval '1 minute' where id = :i",
        i=a,
    )
    conn.run(
        "update items set deleted_at = now() - interval '1 second' where id = :i",
        i=b,
    )

    entries = items_store.list_trash(
        conn, limit=30, before_deleted_at=None, before_id=None
    )

    assert [e.id for e in entries] == [b, a]
    assert all(e.days_left >= 0 for e in entries)
    assert all(e.days_left <= items_store.TRASH_RETENTION_DAYS for e in entries)


@pytest.mark.pg
def test_回收站列表的游标能翻页且不漏条(conn) -> None:
    """复合游标的行值比较 `(deleted_at, id) < (...)` 只在真库上验得了语法。"""
    ids = []
    for i in range(4):
        item_id = _create(conn, f"https://example.com/{i}")
        items_store.soft_delete(conn, item_id)
        conn.run(
            "update items set deleted_at = now() + interval ':n second' "
            "where id = :i".replace(":n", str(i)),
            i=item_id,
        )
        ids.append(item_id)

    page1 = items_store.list_trash(
        conn, limit=2, before_deleted_at=None, before_id=None
    )
    assert len(page1) == 2

    last = page1[-1]
    page2 = items_store.list_trash(
        conn, limit=2, before_deleted_at=last.deleted_at, before_id=last.id
    )

    seen = {e.id for e in page1} | {e.id for e in page2}
    assert len(seen) == 4, f"翻页漏条或重复：{seen}"
    assert page1[-1].id not in {e.id for e in page2}, "第二页重复了第一页的最后一条"


@pytest.mark.pg
def test_count_trash只数已删的(conn) -> None:
    a = _create(conn, "https://example.com/1")
    _create(conn, "https://example.com/2")

    assert items_store.count_trash(conn) == 0
    items_store.soft_delete(conn, a)
    assert items_store.count_trash(conn) == 1


@pytest.mark.pg
def test_冲突标记提前标出(conn) -> None:
    """`conflictsWithActive` 让用户在点恢复**之前**就知道会 409。"""
    a = _create(conn, "https://example.com/dup")
    items_store.soft_delete(conn, a)
    _create(conn, "https://example.com/dup")

    entries = items_store.list_trash(
        conn, limit=30, before_deleted_at=None, before_id=None
    )
    assert len(entries) == 1
    assert entries[0].conflicts_with_active is True


# ── purge ───────────────────────────────────────────────────


@pytest.mark.pg
def test_purge_item只能删回收站里的(conn) -> None:
    live = _create(conn, "https://example.com/live")
    dead = _create(conn, "https://example.com/dead")
    items_store.soft_delete(conn, dead)

    assert items_store.purge_item(conn, live) is False, "活条目不该被彻底删"
    assert items_store.purge_item(conn, dead) is True

    rows = conn.run("select count(*) from items where id = :i", i=dead)
    assert int(str(rows[0][0])) == 0


@pytest.mark.pg
def test_purge_all只清回收站(conn) -> None:
    live = _create(conn, "https://example.com/live")
    dead1 = _create(conn, "https://example.com/d1")
    dead2 = _create(conn, "https://example.com/d2")
    items_store.soft_delete(conn, dead1)
    items_store.soft_delete(conn, dead2)

    ids = items_store.purge_all(conn)

    assert sorted(ids) == sorted([dead1, dead2])
    rows = conn.run("select count(*) from items where id = :i", i=live)
    assert int(str(rows[0][0])) == 1, "活条目不该被清空"


@pytest.mark.pg
def test_purge_expired只删过期的(conn) -> None:
    """保留期判断在 SQL 里用 `now() - make_interval(...)`，只在真库上验得了。"""
    fresh = _create(conn, "https://example.com/fresh")
    old = _create(conn, "https://example.com/old")
    items_store.soft_delete(conn, fresh)
    items_store.soft_delete(conn, old)
    # 把 old 的删除时刻推到 31 天前
    conn.run(
        "update items set deleted_at = now() - interval '31 days' where id = :i",
        i=old,
    )

    ids = items_store.purge_expired(conn, older_than_days=30, limit=200)

    assert ids == [old], f"应只删过期的 {old}，实际 {ids}"
    rows = conn.run("select count(*) from items where id = :i", i=fresh)
    assert int(str(rows[0][0])) == 1, "30 天内删的不该被删"


@pytest.mark.pg
def test_purge_expired跳过有running任务的条目(conn) -> None:
    """**这条护栏没有它，故障是静默的**。

    正在抓取的条目被删行，worker 拿着已不存在的 `item_id` 继续写——
    `update` 与 `mark_done` 都命中 0 行，全程不报错，而那条内容的
    fetch 结果是空的。
    """
    item_id = _create(conn, "https://example.com/busy")
    items_store.soft_delete(conn, item_id)
    conn.run(
        "update items set deleted_at = now() - interval '31 days' where id = :i",
        i=item_id,
    )
    # 造一个 running 的 job
    conn.run(
        "update jobs set state = 'running' where item_id = :i and step = 'fetch'",
        i=item_id,
    )

    ids = items_store.purge_expired(conn, older_than_days=30, limit=200)

    assert ids == [], "有 running 任务的条目不该被删"
    rows = conn.run("select count(*) from items where id = :i", i=item_id)
    assert int(str(rows[0][0])) == 1


@pytest.mark.pg
def test_purge_expired尊重limit(conn) -> None:
    for i in range(5):
        item_id = _create(conn, f"https://example.com/{i}")
        items_store.soft_delete(conn, item_id)
        conn.run(
            "update items set deleted_at = now() - interval '31 days' where id = :i",
            i=item_id,
        )

    ids = items_store.purge_expired(conn, older_than_days=30, limit=3)
    assert len(ids) == 3, "limit 没生效"


@pytest.mark.pg
def test_删掉条目后它的jobs被级联删除(conn) -> None:
    """`jobs.item_id` 是 `on delete cascade`。

    这条是「purge 不自己删索引」的结构性依据（设计 §4.2）：条目行一没，
    它名下的 index job 跟着消失——让「删行」入队一个「清索引」的任务
    在结构上做不到。
    """
    item_id = _create(conn)
    items_store.soft_delete(conn, item_id)
    before = conn.run("select count(*) from jobs where item_id = :i", i=item_id)
    assert int(str(before[0][0])) >= 1

    items_store.purge_item(conn, item_id)

    after = conn.run("select count(*) from jobs where item_id = :i", i=item_id)
    assert int(str(after[0][0])) == 0, "jobs 没有被级联删除"

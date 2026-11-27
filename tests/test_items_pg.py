"""R4 真库集成测试：入库 + 入队是否真的按设计落地。

连不上 Postgres 时整个模块 skip（见 tests/conftest.py）。在平板上跑：
    KB_PG_HOST=100.124.156.47 KB_PG_USER=u0_a201 python -m pytest tests/test_items_pg.py -q
本机没库时自动跳过，不假绿。

每个用例从空的 kb_test 开始（session 级 fixture 重建库），函数级用事务回滚隔离。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from kb.store import items as items_store
from kb.store import jobs as jobs_store
from kb.store.db import connect
from kb.store.migrate import discover, migrate


@pytest.fixture(scope="session")
def fresh_db(pg_cfg):
    """重建 kb_test 并跑一遍迁移。"""
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


def _create_url(
    conn, url: str = "https://v.douyin.com/abc/"
) -> items_store.CreateResult:
    return items_store.create_item(
        conn,
        kind="link",
        url=url,
        url_norm=items_store.normalize_url(url),
        title="一条抖音",
        source_domain=items_store.host_of(url),
        content_text=None,
        step="fetch",
    )


# ── 入库 + 入队 ────────────────────────────────────────────────


def test_新建链接同时入队_fetch(conn):
    result = _create_url(conn)
    assert result.duplicate is False
    assert result.status == items_store.PENDING

    row = conn.run(
        "select status, url_norm, kind from items where id = :i", i=result.id
    )[0]
    assert str(row[0]) == "unread"  # 阅读状态，不是处理状态
    assert str(row[1]) == "https://v.douyin.com/abc"  # 已归一化（去尾斜杠）
    assert str(row[2]) == "link"

    job = conn.run("select step, state from jobs where item_id = :i", i=result.id)[0]
    assert str(job[0]) == "fetch"
    assert str(job[1]) == "pending"


def test_同一链接重复提交只有一条与一个任务(conn):
    first = _create_url(conn)
    second = _create_url(conn)

    assert second.duplicate is True
    assert second.id == first.id
    assert second.status == items_store.PENDING

    item_count = conn.run("select count(*) from items")[0][0]
    job_count = conn.run("select count(*) from jobs")[0][0]
    assert int(item_count) == 1
    assert int(job_count) == 1


def test_归一化等价的两个写法视为同一条(conn):
    first = _create_url(conn, "https://a.com/x/")
    second = _create_url(conn, "https://a.com/x")
    assert second.duplicate is True
    assert second.id == first.id


def test_纯文字条目入队的是_summarize(conn):
    result = items_store.create_item(
        conn,
        kind="text",
        url=None,
        url_norm=None,
        title="今天想到一个点子",
        source_domain="",
        content_text="今天想到一个点子",
        step="summarize",
    )
    row = conn.run("select url, kind from items where id = :i", i=result.id)[0]
    assert row[0] is None
    assert str(row[1]) == "text"

    job = conn.run("select step from jobs where item_id = :i", i=result.id)[0]
    assert str(job[0]) == "summarize"


def test_入队函数对同一步骤幂等(conn):
    result = _create_url(conn)
    assert jobs_store.enqueue(conn, result.id, "fetch") is False  # 已有 pending
    assert jobs_store.enqueue(conn, result.id, "summarize") is True  # 新步骤
    total = conn.run("select count(*) from jobs where item_id = :i", i=result.id)[0][0]
    assert int(total) == 2


# ── 列表与翻页 ─────────────────────────────────────────────────


def test_列表按_id_倒序且游标能翻页(conn):
    ids = [_create_url(conn, f"https://a.com/{n}").id for n in range(3)]

    page1 = items_store.list_items(conn, limit=2, before_id=None)
    assert [item.id for item in page1] == [ids[2], ids[1]]

    page2 = items_store.list_items(conn, limit=2, before_id=page1[-1].id)
    assert [item.id for item in page2] == [ids[0]]


def test_软删的条目不进列表(conn):
    keep = _create_url(conn, "https://a.com/keep")
    gone = _create_url(conn, "https://a.com/gone")
    conn.run("update items set deleted_at = now() where id = :i", i=gone.id)

    listed = items_store.list_items(conn, limit=10, before_id=None)
    assert [item.id for item in listed] == [keep.id]


def test_详情取处理状态与任务明细(conn):
    result = _create_url(conn)
    detail = items_store.get_item(conn, result.id)
    assert detail is not None
    assert detail.status == items_store.PENDING
    assert detail.read_status == "unread"
    assert detail.props == {}
    assert isinstance(detail.saved_at, datetime)

    job_rows = items_store.item_jobs(conn, result.id)
    assert [str(row["step"]) for row in job_rows] == ["fetch"]
    assert str(job_rows[0]["state"]) == "pending"


def test_详情查不到已软删的条目(conn):
    result = _create_url(conn)
    conn.run("update items set deleted_at = now() where id = :i", i=result.id)
    assert items_store.get_item(conn, result.id) is None


def test_没有任务的条目状态是_none(conn):
    """软删再恢复、或 fetch 被手动清掉时会出现这种条目，状态要有明确取值。"""
    result = _create_url(conn)
    conn.run("delete from jobs where item_id = :i", i=result.id)
    detail = items_store.get_item(conn, result.id)
    assert detail is not None
    assert detail.status == items_store.NO_JOB

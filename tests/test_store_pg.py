"""真库集成测试：12 张表是否真的建出来、8 类约束是否真的生效。

连不上 Postgres 时整个模块 skip（见 tests/conftest.py）。
在平板上跑：KB_PG_HOST=127.0.0.1 KB_PG_USER=u0_a201 python -m pytest tests/test_store_pg.py -q
"""

from __future__ import annotations

import pg8000.exceptions
import pytest

from kb.store.db import connect
from kb.store.migrate import TABLES, discover, migrate, status


@pytest.fixture(scope="session")
def fresh_db(pg_cfg):
    """重建 kb_test 并跑一遍迁移。返回本次应用的迁移列表。"""
    admin = connect(pg_cfg.with_database("postgres"))
    admin.run(f'drop database if exists "{pg_cfg.database}" with (force)')
    admin.run(f'create database "{pg_cfg.database}"')
    admin.close()

    conn = connect(pg_cfg)
    try:
        return migrate(conn, discover())
    finally:
        conn.close()


@pytest.fixture
def conn(pg_cfg, fresh_db):
    """每个测试一个事务，结束回滚，测试之间互不污染。"""
    c = connect(pg_cfg)
    c.run("begin")
    try:
        yield c
    finally:
        c.run("rollback")
        c.close()


def rejects(conn, sql: str, **params) -> None:
    """断言这条 SQL 会被数据库拒绝，并回滚到 savepoint 以免污染后续语句。"""
    conn.run("savepoint sp")
    try:
        conn.run(sql, **params)
    except pg8000.exceptions.DatabaseError:
        conn.run("rollback to savepoint sp")
        return
    raise AssertionError(f"期望被拒绝，但执行成功了：{sql}")


def _new_item(conn, kind: str = "article") -> int:
    return conn.run(
        "insert into items (kind, title) values (:k, 'x') returning id", k=kind
    )[0][0]


# ── 表与版本 ────────────────────────────────────────────────────


def test_first_run_applies_every_version(fresh_db):
    assert [m.version for m in fresh_db] == [1, 2, 3, 4]


def test_twelve_tables_exist(pg_cfg, fresh_db):
    conn = connect(pg_cfg)
    try:
        rows = conn.run("select tablename from pg_tables where schemaname = 'public'")
    finally:
        conn.close()
    names = {row[0] for row in rows}
    assert set(TABLES) <= names, f"缺表：{sorted(set(TABLES) - names)}"


def test_second_run_changes_nothing(pg_cfg, fresh_db):
    """技术方案 §6.8 的 T16 判据。"""
    conn = connect(pg_cfg)
    try:
        before = conn.run("select count(*) from schema_migrations")[0][0]
        again = migrate(conn, discover())
        after = conn.run("select count(*) from schema_migrations")[0][0]
    finally:
        conn.close()
    assert again == []
    assert before == after == 4


def test_status_reports_complete(pg_cfg, fresh_db):
    conn = connect(pg_cfg)
    try:
        result = status(conn, discover())
    finally:
        conn.close()
    assert result == {
        "schema_version": 4,
        "applied": 4,
        "pending": 0,
        "tables": 12,
    }


def test_eight_preset_kinds(conn):
    rows = conn.run("select kind from item_kinds where is_preset order by sort_order")
    assert [r[0] for r in rows] == [
        "article",
        "image_text",
        "video",
        "image",
        "file",
        "text",
        "project",
        "link",
    ]


# ── 8 类约束 ────────────────────────────────────────────────────


def test_unknown_kind_rejected_by_foreign_key(conn):
    rejects(conn, "insert into items (kind, title) values ('not_a_kind', 'x')")


def test_invalid_status_rejected(conn):
    rejects(conn, "insert into items (kind, status) values ('article', 'foo')")


def test_props_must_be_object(conn):
    rejects(conn, "insert into items (kind, props) values ('article', '[]'::jsonb)")


def test_attachment_mime_rules(conn):
    item = _new_item(conn)
    rejects(
        conn,
        "insert into attachments (item_id, role, storage_key, mime, sha256)"
        " values (:i, 'doc', 'k1', 'application/x-evil', 'x')",
        i=item,
    )
    conn.run(
        "insert into attachments (item_id, role, storage_key, mime, sha256)"
        " values (:i, 'doc', 'k2', 'application/pdf', 'x')",
        i=item,
    )


def test_attachment_role_rules(conn):
    item = _new_item(conn)
    rejects(
        conn,
        "insert into attachments (item_id, role, storage_key, mime, sha256)"
        " values (:i, 'evil', 'k3', 'image/png', 'x')",
        i=item,
    )


def test_tags_max_two_levels(conn):
    top = conn.run("insert into tags (name) values ('阅读') returning id")[0][0]
    mid = conn.run(
        "insert into tags (name, parent_id) values ('小说', :p) returning id", p=top
    )[0][0]
    rejects(
        conn,
        "insert into tags (name, parent_id) values ('科幻', :p)",
        p=mid,
    )


def test_tags_sibling_name_is_normalized(conn):
    """表达式唯一索引 `(coalesce(parent_id,0), lower(btrim(name)))` 的效果。

    这里用「读书」而不是「AI相关」：0004 已经预置了 8 个一级标签，
    拿其中一个当例子的第一个 insert 就会撞上已有行，测不到「重复被拒」。
    """
    conn.run("insert into tags (name) values ('读书笔记')")
    rejects(conn, "insert into tags (name) values ('读书笔记')")
    rejects(conn, "insert into tags (name) values ('  读书笔记  ')")
    rejects(conn, "insert into tags (name) values ('读书笔记 ')")
    # 大小写差异也拦（中文没大小写，用一个中文名 + 拉丁字母的组合验证）
    conn.run("insert into tags (name) values ('Skill插件')")
    rejects(conn, "insert into tags (name) values ('skill插件')")


# ── 0004 预置标签（AI-03 / F6）──────────────────────────────────


def test_0004_seeds_eight_preset_tags(fresh_db, pg_cfg):
    """AI-03 / F6：迁移跑完，8 个预置一级标签都在库。

    这条必须在真库上验：`on conflict do nothing` 与表达式唯一索引的配合
    只有 PG 自己判得了，静态读 SQL 文件看不出「报没有匹配的约束」。
    """
    from kb.store.db import connect as _connect

    c = _connect(pg_cfg)
    try:
        rows = c.run(
            "select name from tags where is_preset and parent_id is null order by id"
        )
    finally:
        c.close()
    assert [r[0] for r in rows] == [
        "AI相关",
        "工作赚钱",
        "学点东西",
        "生活过日子",
        "健康",
        "好看的东西",
        "好玩放松",
        "先存着以后用",
    ]


def test_0004_rerun_keeps_eight_rows(fresh_db, pg_cfg):
    """F6：迁移再跑一次，预置标签仍是 8 行（不是 16 行）。"""
    from kb.store.db import connect as _connect
    from kb.store.migrate import discover as _discover
    from kb.store.migrate import migrate as _migrate

    c = _connect(pg_cfg)
    try:
        again = _migrate(c, _discover())
        rows = c.run("select count(*) from tags where is_preset")
    finally:
        c.close()
    assert again == [], "第二次迁移应当无事可做"
    assert int(rows[0][0]) == 8


def test_url_norm_unique_allows_after_soft_delete(conn):
    conn.run("insert into items (kind, url_norm) values ('article', 'https://a.com/x')")
    rejects(
        conn,
        "insert into items (kind, url_norm) values ('article', 'https://a.com/x')",
    )
    conn.run("update items set deleted_at = now() where url_norm = 'https://a.com/x'")
    conn.run("insert into items (kind, url_norm) values ('article', 'https://a.com/x')")


def test_jobs_one_pending_per_item_step(conn):
    item = _new_item(conn)
    conn.run("insert into jobs (item_id, step) values (:i, 'fetch')", i=item)
    rejects(conn, "insert into jobs (item_id, step) values (:i, 'fetch')", i=item)
    conn.run("update jobs set state = 'done' where item_id = :i", i=item)
    conn.run("insert into jobs (item_id, step) values (:i, 'fetch')", i=item)


def test_duration_generated_from_props(conn):
    value = conn.run(
        "insert into items (kind, props) values ('video', '{\"duration_sec\":\"90\"}'::jsonb)"
        " returning duration_sec"
    )[0][0]
    assert value == 90

    blank = conn.run(
        "insert into items (kind, props) values ('video', '{\"duration_sec\":\"\"}'::jsonb)"
        " returning duration_sec"
    )[0][0]
    assert blank is None


def test_updated_at_trigger_overwrites_stale_value(conn):
    item = conn.run(
        "insert into items (kind, title, updated_at)"
        " values ('article', 'a', timestamptz '2020-01-01') returning id"
    )[0][0]
    conn.run("update items set title = 'b' where id = :i", i=item)
    updated = conn.run("select updated_at from items where id = :i", i=item)[0][0]
    assert updated.year > 2020

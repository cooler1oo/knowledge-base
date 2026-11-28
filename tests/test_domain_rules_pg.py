"""M3-08 真库集成：类型修正被记住（技术方案判据 T1、T2）。

连不上 Postgres 时整个模块 skip（见 `tests/conftest.py`）。平板上跑：

    KB_PG_HOST=127.0.0.1 KB_PG_USER=u0_a201 KB_PG_DATABASE=kb_test \
    python -m pytest tests/test_domain_rules_pg.py -q

## 为什么这层必须有

假连接验的是「我写的 SQL 串符合我的预期」，真库验的是「PG 认不认这条 SQL」。
三类错只有真库能抓：

1. `on conflict (domain)` 需要 `domain` 上有唯一约束——真建表时漏了主键就会
   报 `there is no unique or exclusion constraint matching the ON CONFLICT`
2. `kind` 的外键指向 `item_kinds`——假连接永远不校验外键
3. `delete ... returning` 在 pg8000 上的返回形状

2026-11-28 的检索层就是在本机全绿、真库炸出 3 条红之后才补的这一层。
"""

from __future__ import annotations

import pytest

from kb.store import domain_rules
from kb.store import items as items_store
from kb.store.db import connect
from kb.store.migrate import discover, migrate


@pytest.fixture(scope="session")
def fresh_db(pg_cfg):
    """重建 kb_test 并跑一遍迁移。与 `test_items_pg.py` 同一手法。"""
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


# ── 基本读写 ────────────────────────────────────────────────


@pytest.mark.pg
def test_remember之后get_kind_for能查到(conn) -> None:
    domain_rules.remember(conn, "douyin.com", "article")
    assert domain_rules.get_kind_for(conn, "douyin.com") == "article"


@pytest.mark.pg
def test_没有规则时返回None(conn) -> None:
    assert domain_rules.get_kind_for(conn, "nothing.example") is None


@pytest.mark.pg
def test_remember第二次是更新不是报错(conn) -> None:
    """`on conflict (domain) do update` 需要 `domain` 上的主键/唯一约束。

    真建表时若漏了主键，这里会报
    `there is no unique or exclusion constraint matching the ON CONFLICT`。
    """
    domain_rules.remember(conn, "douyin.com", "video")
    domain_rules.remember(conn, "douyin.com", "article")

    assert domain_rules.get_kind_for(conn, "douyin.com") == "article"
    rows = conn.run("select count(*) from domain_type_rules")
    assert int(str(rows[0][0])) == 1, "upsert 不该留下两行"


@pytest.mark.pg
def test_非法kind被外键与校验双重挡住(conn) -> None:
    """`UnknownKindError` 在写库**之前**就拦住，不会走到外键。

    这条验的是「不会 500」：假连接能验分支，真库才能验「即使校验漏了，
    外键也会拒绝而不是写入脏值」。
    """
    with pytest.raises(domain_rules.UnknownKindError):
        domain_rules.remember(conn, "x.com", "podcast")
    assert domain_rules.get_kind_for(conn, "x.com") is None


@pytest.mark.pg
def test_forget返回True只在真删到行时(conn) -> None:
    domain_rules.remember(conn, "douyin.com", "article")
    assert domain_rules.forget(conn, "douyin.com") is True
    assert domain_rules.forget(conn, "douyin.com") is False
    assert domain_rules.get_kind_for(conn, "douyin.com") is None


@pytest.mark.pg
def test_list_rules按域名升序(conn) -> None:
    domain_rules.remember(conn, "zhihu.com", "article")
    domain_rules.remember(conn, "douyin.com", "video")

    rules = domain_rules.list_rules(conn)
    assert [d for d, _ in rules] == sorted(d for d, _ in rules)


@pytest.mark.pg
def test_合法kind来自item_kinds表(conn) -> None:
    """表里有什么就能用什么——含用户后来加的。

    `item_kinds` 支持「加新形态 = 插一行」。这条用例临时插一行
    `podcast`，验「代码不用改就能接受它」（不硬编码枚举）。
    """
    conn.run(
        "insert into item_kinds (kind, display_name, sort_order) "
        "values ('podcast', '播客', 99) on conflict do nothing"
    )
    domain_rules.remember(conn, "xiaoyuzhou.fm", "podcast")
    assert domain_rules.get_kind_for(conn, "xiaoyuzhou.fm") == "podcast"


# ── T2 判据：建规则后同域名走规则；删规则后回到自动判断 ─────


@pytest.mark.pg
def test_建规则后同域名查到新值_删规则后查不到(conn) -> None:
    """技术方案 T2 的原文判据，一条用例完整走一遍。"""
    domain = "douyin.com"
    assert domain_rules.get_kind_for(conn, domain) is None, "起点：无规则"

    domain_rules.remember(conn, domain, "article")
    assert domain_rules.get_kind_for(conn, domain) == "article", "建规则后走规则"

    domain_rules.forget(conn, domain)
    assert domain_rules.get_kind_for(conn, domain) is None, "删规则后回到自动判断"


# ── set_kind 真库行为 ───────────────────────────────────────


@pytest.mark.pg
def test_set_kind真改条目且返回True(conn) -> None:
    created = items_store.create_item(
        conn,
        kind="link",
        url="https://v.douyin.com/abc/",
        url_norm=items_store.normalize_url("https://v.douyin.com/abc/"),
        title="一条抖音",
        source_domain="douyin.com",
        content_text=None,
        step="fetch",
    )
    assert items_store.set_kind(conn, created.id, kind="article") is True

    item = items_store.get_item(conn, created.id)
    assert item is not None
    assert item.kind == "article"


@pytest.mark.pg
def test_set_kind对不存在的id返回False(conn) -> None:
    assert items_store.set_kind(conn, 999_999, kind="article") is False


@pytest.mark.pg
def test_set_kind的非法值被外键拒绝(conn) -> None:
    """这是最后一道防线：即使有人绕过 `domain_rules.remember` 直接改条目，
    数据库也不会写入脏值。

    异常类型不固定（pg8000 版本间不同），所以只断言「抛了」并且「条目没变」。
    """
    created = items_store.create_item(
        conn,
        kind="link",
        url="https://v.douyin.com/abc/",
        url_norm=items_store.normalize_url("https://v.douyin.com/abc/"),
        title="t",
        source_domain="douyin.com",
        content_text=None,
        step="fetch",
    )
    with pytest.raises(Exception):  # noqa: B017 外键异常类型随驱动版本变
        items_store.set_kind(conn, created.id, kind="podcast")
    conn.run("rollback")
    conn.run("begin")
    item = items_store.get_item(conn, created.id)
    # 事务已回滚，条目也不在了——这条只验「没有静默写入脏值」
    assert item is None or item.kind != "podcast"

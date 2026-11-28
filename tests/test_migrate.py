"""迁移器与 SQL 文件的本地单元测试。不需要 Postgres。

这里守的是「改坏了自己会报」：文件名、版本连续性、SQL 里的危险语句、
以及迁移器「已应用的跳过、失败的整份回滚」这两条行为。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kb.store import db
from kb.store.migrate import (
    TABLES,
    Migration,
    MigrationError,
    apply_one,
    discover,
    migrate,
    plan,
)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "kb" / "store" / "migrations"


# ── 版本发现 ────────────────────────────────────────────────────


def test_discover_real_migrations_are_continuous():
    found = discover(MIGRATIONS_DIR)
    assert [m.version for m in found] == list(range(1, len(found) + 1))
    assert len(found) >= 3


def test_README的编号提示与实际最大版本一致():
    """README 里「现有最大 NNNN → NNNN+1」这句必须跟实际文件对得上。

    为什么要守一个注释：这句是「加新迁移」那一步唯一的指路牌，而它漂移时
    没有任何症状——照抄的人会 `touch 0004_add_xxx.sql`，撞上已存在的 0004，
    或者更糟：写成 `0004_xxx.sql` 覆盖掉 `0004_seed_ai_tags.sql` 的编号，
    迁移器报「版本重号」而那时人已经在改别的东西了。

    2026-11-28 实测漂移过两个版本：`0004_seed_ai_tags.sql` 已存在时
    README 还写着「现有最大 0003 → 0004」。当时无任何用例覆盖。
    """
    found = discover(MIGRATIONS_DIR)
    largest = max(m.version for m in found)
    readme = (MIGRATIONS_DIR / "README.md").read_text(encoding="utf-8")

    hint = re.search(r"现有最大\s*(\d{4})\s*→\s*(\d{4})", readme)
    assert hint is not None, "README 里找不到「现有最大 NNNN → NNNN」提示句"

    stated_largest = int(hint.group(1))
    stated_next = int(hint.group(2))
    assert stated_largest == largest, (
        f"README 说现有最大是 {stated_largest:04d}，实际最大是 {largest:04d}。"
        f"改 README 的那句提示。"
    )
    assert stated_next == largest + 1, (
        f"README 说下一个编号是 {stated_next:04d}，应为 {largest + 1:04d}"
    )


def _write(directory: Path, name: str, sql: str = "select 1") -> None:
    (directory / name).write_text(sql, encoding="utf-8")


def test_discover_rejects_bad_filename(tmp_path):
    _write(tmp_path, "1_schema.sql")
    with pytest.raises(MigrationError, match="文件名不合规"):
        discover(tmp_path)


def test_discover_rejects_duplicate_version(tmp_path):
    _write(tmp_path, "0001_a.sql")
    _write(tmp_path, "0001_b.sql")
    with pytest.raises(MigrationError, match="版本号重复"):
        discover(tmp_path)


def test_discover_rejects_gap(tmp_path):
    _write(tmp_path, "0001_a.sql")
    _write(tmp_path, "0003_c.sql")
    with pytest.raises(MigrationError, match="必须从 1 连续"):
        discover(tmp_path)


def test_discover_rejects_empty_directory(tmp_path):
    with pytest.raises(MigrationError, match="没有找到任何"):
        discover(tmp_path)


def test_plan_skips_applied():
    migrations = [
        Migration(1, "0001_a.sql", Path("a")),
        Migration(2, "0002_b.sql", Path("b")),
        Migration(3, "0003_c.sql", Path("c")),
    ]
    assert [m.version for m in plan(migrations, {1, 2})] == [3]
    assert plan(migrations, {1, 2, 3}) == []


# ── SQL 文件静态规则（AGENTS.md §8：能机器查的就不写文档）────────

_DANGEROUS = [
    (re.compile(r"\bdrop\s+table\b", re.I), "drop table"),
    (re.compile(r"\btruncate\b", re.I), "truncate"),
    (re.compile(r"\bdelete\s+from\b", re.I), "delete from"),
    (re.compile(r"^\s*(begin|commit|rollback)\s*;", re.I | re.M), "事务控制语句"),
]


def _sql_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def test_every_sql_file_is_non_empty():
    for path in _sql_files():
        assert path.read_text(encoding="utf-8").strip(), f"{path.name} 是空的"


@pytest.mark.parametrize("pattern,label", _DANGEROUS)
def test_sql_has_no_dangerous_statement(pattern, label):
    for path in _sql_files():
        text = path.read_text(encoding="utf-8")
        match = pattern.search(text)
        assert not match, f"{path.name} 出现禁止的{label}：{match.group(0)!r}"


def test_created_tables_are_within_whitelist():
    pattern = re.compile(r"create\s+table\s+if\s+not\s+exists\s+([a-z_]+)", re.I)
    for path in _sql_files():
        for name in pattern.findall(path.read_text(encoding="utf-8")):
            assert name in TABLES, f"{path.name} 建了白名单外的表：{name}"


def test_all_twelve_tables_are_created_somewhere():
    pattern = re.compile(r"create\s+table\s+if\s+not\s+exists\s+([a-z_]+)", re.I)
    created: set[str] = set()
    for path in _sql_files():
        created |= set(pattern.findall(path.read_text(encoding="utf-8")))
    missing = set(TABLES) - created
    assert not missing, f"这些表没有任何迁移文件创建：{sorted(missing)}"


def test_seed_covers_eight_preset_kinds():
    seed = (MIGRATIONS_DIR / "0003_seed.sql").read_text(encoding="utf-8")
    kinds = re.findall(r"^\s*\('([a-z_]+)',", seed, re.M)
    assert len(kinds) == 8
    assert len(set(kinds)) == 8


# ── 0004：AI 标签种子（AI-03 / F6）────────────────────────────
#
# 为什么必须是独立的 0004 而不是改 0003：`migrate.py` 的 `plan()` 只执行
# 「版本号未记录」的文件。平板真库的 `schema_migrations` 里已有版本 3，
# 改 0003 在那个库上**永远不会执行**，8 个预置标签一行都进不去，
# AI-02 的「只能从词表挑」在功能上退化成「AI 永不打标签」。


AI_TAG_NAMES = (
    "AI相关",
    "工作赚钱",
    "学点东西",
    "生活过日子",
    "健康",
    "好看的东西",
    "好玩放松",
    "先存着以后用",
)


def _ai_tag_seed() -> str:
    path = MIGRATIONS_DIR / "0004_seed_ai_tags.sql"
    assert path.is_file(), (
        "必须新开 0004_seed_ai_tags.sql（改 0003 在已部署库上不执行）"
    )
    return path.read_text(encoding="utf-8")


def _strip_comments(sql: str) -> str:
    """去掉 `--` 行注释，只留可执行语句。

    为什么需要它：0004 的注释里**故意**写了 `on conflict (name)` 与
    `parent_id` 作为反面教材。不断言在注释上，测试会把自己讲道理的段落
    当成违规。
    """
    return "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )


def _ai_tag_sql() -> str:
    """可执行部分（已去注释、已小写），供静态断言使用。"""
    return _strip_comments(_ai_tag_seed()).lower()


def test_0004_种子插8个一级标签_重复执行仍为8行_F6():
    """F6：迁移跑两次，预置标签仍是 8 行。

    断言两条：① SQL 里正好 8 个名字；② 必须写 `on conflict do nothing`
    （幂等），否则第二次执行主键冲突。
    """
    sql = _strip_comments(_ai_tag_seed())
    names = re.findall(r"^\s*\('([^']+)',", sql, re.M)
    assert len(names) == 8, f"应当是 8 个一级标签，实际 {len(names)}：{names}"
    assert tuple(names) == AI_TAG_NAMES, f"标签名与 AI-03 不一致：{names}"
    assert "on conflict do nothing" in sql.lower(), "缺幂等子句，第二次执行会冲突"


def test_0004_conflict_子句必须是裸写法不是列名():
    """`tags` 的唯一索引是表达式索引 `on tags (coalesce(parent_id, 0), lower(btrim(name)))`。

    PG 不允许表达式索引用作 `on conflict (cols)` 的仲裁索引，写
    `on conflict (name) do nothing` 会直接报「没有匹配的唯一约束」。
    这条断言把「必须裸写」钉在测试里，避免后来的人「顺手补个列名」。
    """
    sql = _ai_tag_sql()
    assert "on conflict do nothing" in sql
    assert "on conflict (" not in sql, "表达式唯一索引不能被列为仲裁目标，必须裸写"


def test_0004_种子标签_is_preset_为真且_parent_为空_AI_03():
    """AI-03：8 个都是 `is_preset=true` 的**一级**标签，用户可改可删。"""
    sql = _ai_tag_sql()
    assert "is_preset" in sql, "没写 is_preset 列"
    # 每行都显式给 true，不能靠列默认值（默认是 false）
    assert len(re.findall(r"\btrue\b", sql)) == 8, "8 行都得显式 is_preset=true"
    assert "parent_id" not in sql, "一级标签不该出现 parent_id 赋值"


# ── 迁移器行为（fake 连接）─────────────────────────────────────


class FakeConn:
    """只模拟迁移器用到的几条语句，够验证控制流。"""

    def __init__(self, applied: set[int] | None = None) -> None:
        self.applied = set(applied or ())
        self.calls: list[str] = []
        self.raise_on: str | None = None

    def run(self, sql: str, **params):
        head = " ".join(sql.split())[:70]
        self.calls.append(head)
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError("boom")
        low = sql.strip().lower()
        if low.startswith("select version from schema_migrations"):
            return [[v] for v in sorted(self.applied)]
        if low.startswith("insert into schema_migrations"):
            self.applied.add(int(params["v"]))
        return []


def _three_migrations(tmp_path: Path) -> list[Migration]:
    for i, name in enumerate(["0001_a.sql", "0002_b.sql", "0003_c.sql"], start=1):
        _write(tmp_path, name, f"-- {name}")
    return discover(tmp_path)


def test_migrate_applies_all_then_nothing(tmp_path):
    migrations = _three_migrations(tmp_path)
    conn = FakeConn()

    first = migrate(conn, migrations)  # type: ignore[arg-type]
    assert [m.version for m in first] == [1, 2, 3]
    assert conn.applied == {1, 2, 3}

    conn.calls.clear()
    second = migrate(conn, migrations)  # type: ignore[arg-type]
    assert second == []
    assert not any("insert into schema_migrations" in c for c in conn.calls)


def test_migrate_takes_advisory_lock_and_releases_it(tmp_path):
    migrations = _three_migrations(tmp_path)[:1]
    conn = FakeConn()
    migrate(conn, migrations)  # type: ignore[arg-type]
    joined = " | ".join(conn.calls)
    assert "pg_advisory_lock" in joined
    assert "pg_advisory_unlock" in joined


def test_apply_one_wraps_in_transaction(tmp_path):
    _write(tmp_path, "0001_a.sql", "-- 0001")
    migration = discover(tmp_path)[0]
    conn = FakeConn()

    apply_one(conn, migration)  # type: ignore[arg-type]

    assert conn.calls[0] == "begin"
    assert conn.calls[-2:] == [
        "insert into schema_migrations (version) values (:v)",
        "commit",
    ]


def test_apply_one_rolls_back_and_keeps_version_unwritten(tmp_path):
    _write(tmp_path, "0001_a.sql", "select boom")
    migration = discover(tmp_path)[0]
    conn = FakeConn()
    conn.raise_on = "select boom"

    with pytest.raises(MigrationError, match="已回滚"):
        apply_one(conn, migration)  # type: ignore[arg-type]

    assert "rollback" in conn.calls
    assert conn.applied == set()


# ── 连接池 ──────────────────────────────────────────────────────


class _FakePgConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_pool_reuses_connection(monkeypatch):
    made: list[_FakePgConn] = []

    def fake_connect(cfg):
        made.append(_FakePgConn())
        return made[-1]

    monkeypatch.setattr(db, "connect", fake_connect)
    pool = db.Pool(db.PgConfig(host="h", port=1, user="u", database="kb"))

    with pool.connection() as first:
        pass
    with pool.connection() as second:
        pass

    assert first is second
    assert len(made) == 1


def test_pool_discards_connection_after_error(monkeypatch):
    made: list[_FakePgConn] = []

    def fake_connect(cfg):
        made.append(_FakePgConn())
        return made[-1]

    monkeypatch.setattr(db, "connect", fake_connect)
    pool = db.Pool(db.PgConfig(host="h", port=1, user="u", database="kb"))

    with pytest.raises(RuntimeError):
        with pool.connection():
            raise RuntimeError("炸了")

    assert made[0].closed is True
    with pool.connection() as fresh:
        assert fresh is not made[0]
    assert len(made) == 2

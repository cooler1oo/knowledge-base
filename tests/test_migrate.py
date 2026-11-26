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

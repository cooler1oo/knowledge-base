"""编号迁移器：把 migrations/*.sql 按版本号顺序应用到目标库。

三条硬约束（技术方案 §6.8）：
1. 每个文件内部所有 DDL 都带 `IF NOT EXISTS`，可以重复执行
2. 已记录的版本直接跳过；版本号写入与 DDL 在同一事务
3. 启动时按序补差，不要求人工点确认

为什么用独立 .sql 文件而不是把 DDL 写进 Python：排障时能 `psql -f` 单跑，
review 时 diff 可读。Python 只做「发现版本 → 按序执行 → 记版本」。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pg8000.native

from .config import PgConfig
from .db import connect

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_FILENAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

# 固定值即可：只要全库唯一，就只拦住「同时有两个迁移进程」这一种情况
ADVISORY_LOCK_KEY = 728_190_001

TABLES: tuple[str, ...] = (
    "item_kinds",
    "items",
    "attachments",
    "tags",
    "item_tags",
    "queries",
    "domain_type_rules",
    "settings",
    "users",
    "sessions",
    "jobs",
    "schema_migrations",
)

_BOOTSTRAP = """
create table if not exists schema_migrations (
  version    int primary key,
  applied_at timestamptz not null default now()
)
"""


class MigrationError(RuntimeError):
    """迁移失败。消息里带文件与原始报错，便于直接定位。"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """读取目录并按版本排序。版本必须从 1 开始连续，跳号即报错。"""
    found: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if not match:
            raise MigrationError(
                f"迁移文件名不合规：{path.name}（应是 0001_lower_snake.sql）"
            )
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"版本号重复：{version}（{found[version].name} / {path.name}）"
            )
        found[version] = Migration(version=version, name=path.name, path=path)

    versions = sorted(found)
    if not versions:
        raise MigrationError(f"没有找到任何 .sql：{directory}")
    expected = list(range(1, len(versions) + 1))
    if versions != expected:
        raise MigrationError(f"版本号必须从 1 连续：实际 {versions}，期望 {expected}")
    return [found[v] for v in versions]


def applied_versions(conn: pg8000.native.Connection) -> set[int]:
    rows = conn.run("select version from schema_migrations order by version")
    return {int(row[0]) for row in rows}


def plan(migrations: list[Migration], applied: set[int]) -> list[Migration]:
    """待应用的迁移，按版本升序。纯函数，便于单测。"""
    return [m for m in migrations if m.version not in applied]


def ensure_version_table(conn: pg8000.native.Connection) -> None:
    """迁移器自举。0001 里也建一次（IF NOT EXISTS），两处幂等不冲突。"""
    conn.run(_BOOTSTRAP)


def apply_one(conn: pg8000.native.Connection, migration: Migration) -> None:
    """一个文件一个事务：失败则整份回滚，版本号不会残留。"""
    try:
        conn.run("begin")
        conn.run(migration.sql)
        conn.run(
            "insert into schema_migrations (version) values (:v)",
            v=migration.version,
        )
        conn.run("commit")
    except Exception as exc:
        try:
            conn.run("rollback")
        except Exception:
            pass
        raise MigrationError(f"{migration.name} 执行失败，已回滚：{exc}") from exc


def migrate(
    conn: pg8000.native.Connection, migrations: list[Migration]
) -> list[Migration]:
    """应用所有落后版本，返回本次实际执行的文件。"""
    ensure_version_table(conn)
    conn.run("select pg_advisory_lock(:k)", k=ADVISORY_LOCK_KEY)
    try:
        todo = plan(migrations, applied_versions(conn))
        for migration in todo:
            apply_one(conn, migration)
        return todo
    finally:
        conn.run("select pg_advisory_unlock(:k)", k=ADVISORY_LOCK_KEY)


def existing_tables(conn: pg8000.native.Connection) -> set[str]:
    rows = conn.run("select tablename from pg_tables where schemaname = 'public'")
    return {row[0] for row in rows}


def ensure_database(cfg: PgConfig) -> bool:
    """目标库不存在则创建。返回是否真的建了。"""
    admin = connect(cfg.with_database("postgres"))
    try:
        if admin.run("select 1 from pg_database where datname = :n", n=cfg.database):
            return False
        quoted = '"' + cfg.database.replace('"', '""') + '"'
        admin.run(f"create database {quoted}")
        return True
    finally:
        admin.close()


def status(
    conn: pg8000.native.Connection, migrations: list[Migration]
) -> dict[str, int]:
    """当前版本 / 已应用数 / 待应用数 / 已存在的白名单表数。"""
    present = existing_tables(conn)
    applied = applied_versions(conn) if "schema_migrations" in present else set()
    return {
        "schema_version": max(applied, default=0),
        "applied": len(applied),
        "pending": len(plan(migrations, applied)),
        "tables": len([t for t in TABLES if t in present]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m kb.store.migrate",
        description="把 kb/store/migrations/*.sql 应用到目标库（可重复执行）",
    )
    parser.add_argument("--database", help="覆盖库名，默认取 KB_PG_DATABASE 或 kb")
    parser.add_argument("--create-db", action="store_true", help="目标库不存在时先创建")
    parser.add_argument(
        "--check", action="store_true", help="只报告状态，不改库；缺表时退出码 1"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="列出待执行的版本，不执行"
    )
    args = parser.parse_args(argv)

    cfg = PgConfig.from_env(args.database)
    migrations = discover()
    print(f"[迁移] 库={cfg.database} 主机={cfg.host}:{cfg.port} 用户={cfg.user}")

    try:
        if args.create_db:
            if ensure_database(cfg):
                print(f"[迁移] 已创建数据库 {cfg.database}")
            else:
                print(f"[迁移] 数据库 {cfg.database} 已存在")

        conn = connect(cfg)
        try:
            if args.check:
                result = status(conn, migrations)
                present = existing_tables(conn)
                missing = [t for t in TABLES if t not in present]
                print(
                    f"schema_version={result['schema_version']} "
                    f"applied={result['applied']} pending={result['pending']} "
                    f"tables={result['tables']}/{len(TABLES)}"
                )
                if missing:
                    print(f"[迁移] 缺失的表：{', '.join(missing)}")
                    return 1
                return 0

            if args.dry_run:
                todo = (
                    plan(migrations, applied_versions(conn))
                    if "schema_migrations" in existing_tables(conn)
                    else migrations
                )
                for migration in todo:
                    print(f"[迁移] 待执行 {migration.name}")
                print(f"[迁移] 共 {len(todo)} 个待执行")
                return 0

            applied = (
                applied_versions(conn)
                if "schema_migrations" in existing_tables(conn)
                else set()
            )
            todo = migrate(conn, migrations)
            current = max(applied | {m.version for m in todo}, default=0)
            print(
                f"[迁移] 结果：应用 {len(todo)} 个，跳过 {len(migrations) - len(todo)} 个，"
                f"当前版本 {current}"
            )
            return 0
        finally:
            conn.close()
    except MigrationError as exc:
        print(f"[迁移] 失败：{exc}", file=sys.stderr)
        return 2
    except pg8000.exceptions.DatabaseError as exc:
        print(f"[迁移] 数据库报错：{exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

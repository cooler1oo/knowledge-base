# 迁移文件规则

## 命名

`0001_lower_snake.sql` —— 四位版本号 + 下划线 + 小写下划线名。
版本号必须从 1 开始连续，跳号或重号会让迁移器直接报错，不执行任何文件。

## 三条硬规则

| # | 规则 | 为什么 |
|---|---|---|
| 1 | **不得出现 `begin` / `commit` / `rollback`** | 迁移器已经在一个事务里执行整份文件，文件内再开事务会让版本号与 DDL 脱节 |
| 2 | **不得出现 `drop table` / `truncate` / `delete from`** | 单机自用没有回滚通道，删数据靠备份。需要改结构用 `alter table`，需要清数据手工执行 |
| 3 | **所有 DDL 带 `if not exists`** | 版本表丢失时能原地重建；与「版本跳过」构成双保险（技术方案 §6.8 修 N7） |

第 1、2 条由 `tests/test_migrate.py` 静态检查强制执行，不靠自觉。

## 加一个新版本

```bash
# 1. 新建下一个编号（现有最大 0004 → 0005）
touch kb/store/migrations/0005_add_xxx.sql
# 2. 写完跑本地检查
python -m pytest tests/test_migrate.py -q
# 3. 应用到真库
python -m kb.store.migrate
# 4. 连跑第二次，必须输出「应用 0 个」
python -m kb.store.migrate --check
```

## 排障：单独跑一份 SQL

```bash
psql -h 127.0.0.1 -U u0_a201 -d kb -f kb/store/migrations/0001_schema.sql
```

整份文件可以在事务外手工执行，这是选独立 `.sql` 而不是把 DDL 写进 Python 的直接好处。

## 已知事实

- 平板上 PG 在 Termux 原生层，Python 服务在 proot 层，两层通过 `127.0.0.1:5432` 互通
- 平板上 PG 的 role 是 `u0_a201`；容器内 `getpass.getuser()` 返回 `root`，必须显式设 `KB_PG_USER`
- `postgres` 库里残留 R0 的测试表（`item_kinds` / `t_props` / `t_search`），本项目只用 `kb` 与 `kb_test` 两个库

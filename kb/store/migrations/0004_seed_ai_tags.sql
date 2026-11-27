-- 0004 种子数据：8 个预置一级标签（AI-03）
-- 来源：docs/prd/2026-11-27-AI层增量PRD.md AI-03；docs/tech/2026-11-27-AI层设计.md §6.7
--
-- 为什么新开 0004 而不改 0003_seed.sql：
--   kb/store/migrate.py 的 plan() 只执行「版本号未记录」的文件。平板真库的
--   schema_migrations 里已有版本 3（R1–R5 全部跑过迁移），改 0003 在那个库上
--   **永远不会执行**，8 个预置标签一行都进不去，AI-02 的「标签只能从词表挑」
--   在功能上等价于「AI 永不打标签」。项目自己的 migrations/README.md 也写着
--   「现有最大 0003 → 0004」。
--
-- 为什么是裸 `on conflict do nothing` 而不是 `on conflict (name) do nothing`：
--   tags 的唯一索引是**表达式索引**
--     on tags (coalesce(parent_id, 0), lower(btrim(name)))
--   PG 不允许把表达式索引用作 `on conflict (cols)` 的仲裁索引，写列名会直接报
--   「没有匹配的唯一约束」，整份迁移回滚。
--
-- 为什么 parent_id 不出现：8 个都是一级标签（AI-03）。默认值为 NULL，不写即为
-- 一级；显式写 null 也能过，但省略更少一处出错的机会。

insert into tags (name, is_preset) values
  ('AI相关',      true),
  ('工作赚钱',    true),
  ('学点东西',    true),
  ('生活过日子',  true),
  ('健康',        true),
  ('好看的东西',  true),
  ('好玩放松',    true),
  ('先存着以后用', true)
on conflict do nothing;
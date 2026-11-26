-- 0003 种子数据：8 个预设类型
-- 来源：docs/tech/2026-11-26-技术方案.md §5.1
-- 规则：on conflict do nothing，重复执行不报错。

insert into item_kinds (kind, display_name, is_preset, sort_order) values
  ('article',    '文章',    true, 1),
  ('image_text', '图文',    true, 2),
  ('video',      '视频',    true, 3),
  ('image',      '图片',    true, 4),
  ('file',       '文件',    true, 5),
  ('text',       '纯文字',  true, 6),
  ('project',    '项目',    true, 7),
  ('link',       '链接',    true, 8)
on conflict (kind) do nothing;

-- 0002 函数与触发器
-- 来源：docs/tech/2026-11-26-技术方案.md §6.3（标签两级）/ §6.9（updated_at）
-- 规则：本文件由迁移器在一个事务里整体执行，不得出现 begin/commit。

-- ── 标签最多两级：新父必须是一级，自己不能已有子 ────────────────
create or replace function tags_enforce_two_levels() returns trigger as $$
begin
  if new.parent_id is not null then
    if (select parent_id from tags where id = new.parent_id) is not null then
      raise exception '标签最多两级：父标签必须是一级标签';
    end if;
    if exists (select 1 from tags where parent_id = new.id) then
      raise exception '标签最多两级：该标签已有子标签，不能再成为子标签';
    end if;
  end if;
  return new;
end;
$$ language plpgsql;

drop trigger if exists tags_two_levels on tags;
create trigger tags_two_levels
  before insert or update of parent_id on tags
  for each row execute function tags_enforce_two_levels();

-- ── updated_at 自动维护 ─────────────────────────────────────────
-- jobs 的「卡死 15 分钟」判定依赖 updated_at，忘了写会误杀正常长任务
create or replace function touch_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists items_touch on items;
create trigger items_touch before update on items
  for each row execute function touch_updated_at();

drop trigger if exists jobs_touch on jobs;
create trigger jobs_touch before update on jobs
  for each row execute function touch_updated_at();

drop trigger if exists settings_touch on settings;
create trigger settings_touch before update on settings
  for each row execute function touch_updated_at();

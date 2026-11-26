-- 0001 表结构：12 张表 + 全部索引
-- 来源：docs/tech/2026-11-26-技术方案.md §6.2 / §6.3
-- 规则：本文件由迁移器在一个事务里整体执行，不得出现 begin/commit；
--       所有 DDL 带 IF NOT EXISTS，保证重复执行不报错。

-- ── 类型注册表：加新形态 = 插一行，不改任何表结构 ──────────────────
create table if not exists item_kinds (
  kind         text primary key,
  display_name text not null,
  is_preset    boolean not null default false,
  sort_order   int not null default 0,
  created_at   timestamptz not null default now(),
  constraint item_kinds_format check (kind ~ '^[a-z][a-z0-9_]{0,31}$')
);

-- ── 内容主体 ────────────────────────────────────────────────────
create table if not exists items (
  id            bigserial primary key,
  kind          text not null references item_kinds(kind) on update cascade,
  url           text,
  url_norm      text,
  title         text not null default '',
  author        text,
  source_domain text not null default '',
  summary       text,
  content_text  text,
  ocr_text      text,
  primary_media bigint,
  props         jsonb not null default '{}',
  saved_at      timestamptz not null default now(),
  published_at  timestamptz,
  status        text not null default 'unread'
                  check (status in ('unread','reading','read','archived')),
  rating        smallint check (rating between 1 and 5),
  deleted_at    timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  constraint items_props_object check (jsonb_typeof(props) = 'object')
);

-- 生成列：把「经常按它筛」的长尾属性提升为可索引
-- 用 nullif 防空串转型报错：('')::int 会抛 invalid input syntax
alter table items
  add column if not exists duration_sec int
  generated always as (
    nullif(props->>'duration_sec', '')::int
  ) stored;

-- ── 附件：任何形态内容都可挂任意多个 ────────────────────────────
create table if not exists attachments (
  id           bigserial primary key,
  item_id      bigint not null references items(id) on delete cascade,
  role         text not null,
  storage_key  text not null,
  mime         text not null,
  size_bytes   bigint not null default 0,
  width        int,
  height       int,
  duration_sec int,
  sha256       text not null,
  sort_order   int not null default 0,
  created_at   timestamptz not null default now(),
  -- 括号必须有：`~` 优先级高于 `||`，不加括号会解析成 (mime ~ '...') || '...' → 返回 text
  constraint attachments_mime_allowed check (
    mime ~ ('^(image/|video/|audio/|text/'
         || '|application/(pdf|zip|msword|vnd\.|octet-stream))')
  ),
  constraint attachments_role_allowed check (
    role in ('cover','video','image','doc','snapshot','audio')
  )
);

-- ── 索引三件套 ──────────────────────────────────────────────────
create unique index if not exists items_url_norm_uniq
  on items (url_norm) where url_norm is not null and deleted_at is null;
create index if not exists items_props_gin on items using gin (props jsonb_path_ops);
create index if not exists items_kind_saved on items (kind, saved_at desc);
create index if not exists items_duration on items (duration_sec) where duration_sec is not null;

create index if not exists attachments_item on attachments (item_id, sort_order);
create unique index if not exists attachments_key_uniq on attachments (storage_key);

-- ── 两级标签 ────────────────────────────────────────────────────
create table if not exists tags (
  id         bigserial primary key,
  name       text not null,
  parent_id  bigint references tags(id) on delete cascade,
  color      text,
  is_preset  boolean not null default false,
  created_at timestamptz not null default now()
);

-- 标签名归一化：AI相关 与 ai相关 不得并存
create unique index if not exists tags_sibling_name_uniq
  on tags (coalesce(parent_id, 0), lower(btrim(name)));

create table if not exists item_tags (
  item_id    bigint not null references items(id) on delete cascade,
  tag_id     bigint not null references tags(id) on delete cascade,
  origin     text not null check (origin in ('ai','user')),
  confidence real check (confidence between 0 and 1),
  created_at timestamptz not null default now(),
  primary key (item_id, tag_id)
);

-- ── 筛子（保存的筛选条件）──────────────────────────────────────
create table if not exists queries (
  id          bigserial primary key,
  name        text not null,
  filter_json jsonb not null,
  icon        text,
  sort_order  int not null default 0,
  pinned      boolean not null default false,
  created_at  timestamptz not null default now()
);

-- ── 域名 → 类型修正规则（PRD M3-08）─────────────────────────────
create table if not exists domain_type_rules (
  domain     text primary key,
  kind       text not null references item_kinds(kind) on update cascade,
  created_at timestamptz not null default now()
);

-- ── 键值配置 ────────────────────────────────────────────────────
create table if not exists settings (
  key        text primary key,
  value      jsonb not null,
  updated_at timestamptz not null default now()
);

-- ── 单账号 ──────────────────────────────────────────────────────
create table if not exists users (
  id            bigserial primary key,
  username      text not null unique,
  password_hash text not null,
  created_at    timestamptz not null default now()
);

-- ── Cookie 会话 ─────────────────────────────────────────────────
create table if not exists sessions (
  id         bigserial primary key,
  token_hash text not null unique,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  last_seen  timestamptz not null default now()
);
create index if not exists sessions_expires on sessions (expires_at);

-- ── 后台任务队列 ────────────────────────────────────────────────
create table if not exists jobs (
  id         bigserial primary key,
  item_id    bigint not null references items(id) on delete cascade,
  step       text not null check (step in ('fetch','ocr','summarize','tag','index')),
  state      text not null default 'pending'
               check (state in ('pending','running','done','failed')),
  error      text,
  attempts   smallint not null default 0,
  run_after  timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- 同一 item 同一步骤只允许一条未完成任务
create unique index if not exists jobs_pending_uniq
  on jobs (item_id, step) where state in ('pending','running');
create index if not exists jobs_pick on jobs (state, run_after) where state = 'pending';

-- ── 迁移版本表（迁移器也会自举建一次）───────────────────────────
create table if not exists schema_migrations (
  version    int primary key,
  applied_at timestamptz not null default now()
);

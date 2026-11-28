"""`attachments` 表的读写。**全项目唯一允许出现「写 attachments」SQL 的地方。**

## 三条边界

1. 只有本层能写库（`layer_rules.toml` 的 `write_sql_allowed_prefixes`）。
2. 本层不认识 `kb.media` 的类型。收的是「与 `attachments` 列一一对应的一个对象」
   ——`StoredBlob` 恰好是这个形状，但本层只用 `getattr`，不做 `isinstance`。
   这样换掉媒体的值对象时 store 一行都不用改（与 `items.apply_extract` 收
   `list[dict]` 同一个口径）。
3. `role` 限定在 `('cover','image')`：将来 `snapshot` / `video` 的附件不会被
   fetch 重跑误删。

## 为什么「先删后插」必须在同一条语句里

分成两条语句就有一个窗口：旧的删了、新的没进去 → 条目没有封面，而
`attachments` 表里一行都不剩。用一个 `WITH` CTE 把它变成单条语句 → 单事务 →
要么「旧的全在」，要么「新的全在」，没有中间态。

## 为什么不写 `on conflict do nothing`

唯一索引冲突只可能来自两种情况：① 同 item 同内容重复插——被「先删后插」消掉；
② 128 bit 哈希碰撞——概率可忽略。所以冲突**不该发生**，若发生按编程错误处理。
写了 `do nothing` 等于把一次真实的数据完整性异常伪装成正常运行，而
「第二个 item 悄悄没有封面」这种故障排查起来极慢。
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence

import pg8000.native

#: 允许被 `replace_for_item` 整体替换的 role。**只有这两个**：
#: `snapshot`（抽帧）/ `video`（将来按需下载）/ `doc` / `audio` 不能被
#: 「fetch 重跑」误删——那些附件的产出代价比封面高得多。
REPLACEABLE_ROLES = ("cover", "image")


class BlobLike(Protocol):
    """`replace_for_item` 需要的字段。`kb.media.StoredBlob` 结构上满足它。

    用 Protocol 而不是 import `kb.media`：store 层不该依赖媒体层的类型存在，
    两侧只靠「一行记录的字段」这个朴素契约耦合。
    """

    role: str
    storage_key: str
    mime: str
    size_bytes: int
    width: int | None
    height: int | None
    duration_sec: int | None
    sha256: str
    sort_order: int


#: 单条语句完成「删旧的 cover/image + 插新的」。`unnest` 让一次往返写多行。
#:
#: 参数用 PG 数组（`:roles_arr` / `:keys_arr` …），pg8000 的 `run` 会把 Python
#: list 转成数组字面量。**不用逐行 insert**：20 张图 = 20 次往返，在平板上是
#: 可见的延迟。
#:
#: ## `where (select count(*) from gone) >= 0` 这一行不能删
#:
#: 它是**执行顺序的强制依赖**，不是「恒真条件」的废话。PG 文档 §7.8.2：
#:
#: > The sub-statements in WITH are executed concurrently with each other and
#: > with the main query. Therefore, **the order in which the specified updates
#: > actually happen is unpredictable.** All the statements are executed with
#: > the same snapshot, so they cannot see one another's effects on the target
#: > tables.
#:
#: 也就是说 `gone`（DELETE）与 `ins`（INSERT）是**兄弟节点**，谁先跑由计划器定。
#: 而这里必须 DELETE 先跑：重跑同一条内容时新旧 `storage_key` 完全相同，
#: 旧行不退场就撞 `attachments_key_uniq` → `UniqueViolation`。
#:
#: 删掉这一行后，外层 `select count(*) from ins` 只读 `ins`、不读 `gone`，
#: 「未被外层引用的 CTE 结果会在主查询之后才算数」这条规则会让 DELETE 排在
#: INSERT 后面——重跑必撞。这是 PG 的实测行为（pgsql-bugs 上 Tom Lane 的回复：
#: 「In your first example, the INSERT happens first and so it fails」）。
#:
#: ## 为什么是 `>= 0` 不是 `> 0`
#:
#: 首次 fetch 时 `gone` 一行都没删（表里本来没有旧附件），`count(*)` 为 0。
#: 写成 `> 0` 会让**第一次**写入一条都插不进去——把「重跑会撞」换成
#: 「首次全丢」，更糟。
#:
#: ## 为什么不改成两条语句（delete + 逐条 insert）
#:
#: `kb/store/` 走 `pg8000.native`，**这条路径不做事务管理**（见
#: `kb/store/usage.py` 的 docstring：`begin`/`commit`/`rollback`/`autocommit`
#: 在 pg8000 1.31.6 的 `native.py` 与 `core.py` 里一次都没出现，每条语句
#: 执行后立即生效）。拆成两条就有一个窗口：DELETE 生效了、INSERT 失败
#: → **条目的封面与配图全丢**，而库里连一条记录都没有，界面只能显示空白。
#: 单条语句是这里唯一的原子性来源。
#:
#: 注意：`where (select count(*) from gone) >= 0` 已经解决了执行顺序问题，
#: 所以「拆两条」不是为了绕开顺序问题——它换来的是「无窗口」这一条，
#: 而代价是失去原子性。两者不可兼得时选原子性。
_REPLACE_SQL = """
with gone as (
  delete from attachments
   where item_id = :item_id and role = any(:roles)
  returning id
), ins as (
  insert into attachments
    (item_id, role, storage_key, mime, size_bytes, width, height,
     duration_sec, sha256, sort_order)
  select :item_id, v.role, v.storage_key, v.mime, v.size_bytes, v.width,
         v.height, v.duration_sec, v.sha256, v.sort_order
    from unnest(
           :roles_arr::text[], :keys_arr::text[], :mimes_arr::text[],
           :sizes_arr::bigint[], :widths_arr::int[], :heights_arr::int[],
           :durations_arr::int[], :shas_arr::text[], :orders_arr::int[]
         ) as v(role, storage_key, mime, size_bytes, width, height,
                duration_sec, sha256, sort_order)
   -- ⚠️ 下面这一行**不是冗余恒真式，是求值依赖锚点。不许当「可以删的多余条件」删掉。**
   --
   -- 它语义上恒真（count 永远 >= 0），但作用是让 `ins` 引用 `gone`：
   -- PG 必须先算出 `gone`（那条 DELETE）才能判断这个条件，于是 DELETE 必然
   -- 排在 INSERT 之前。
   --
   -- 删掉它 → 外层 `select count(*) from ins` 只引用 `ins`、不引用 `gone`
   -- → PG 认为两者无依赖，**INSERT 会抢在 DELETE 之前执行** → 旧行还在
   -- → 同一个 `storage_key`（`sha256(item_id+内容)`，重跑时必然相同）
   -- → 撞 `attachments_key_uniq` → `UniqueViolation`。
   -- 也就是说：每次覆盖写都会失败，而**首次写入永远正常**（表里没有旧行），
   -- 所以这个 bug 只在「重跑」时暴露，最难在生产中发现。
   --
   -- 依据：平板真 PG 上的 A/B 对照实验（见 R8 交付记录）——
   --   保留此行 → count=1 成功；去掉此行 → `duplicate key value violates
   --   unique constraint`。理论依据是 PG 文档 §7.8.2「WITH 子语句以同一
   --   快照并发执行，实际顺序不可预测」，以及 pgsql-bugs 上 Tom Lane 的
   --   回复：「if they are not [read by the outer query], that happens AFTER
   --   completion of the outer query. In your first example, the INSERT
   --   happens first and so it fails.」
   --
   -- `>= 0` 不是 `> 0`：首次 fetch 时 `gone` 一行都没删（表里本来没有旧附件），
   -- count 为 0。写成 `> 0` 会让**第一次**写入一条都插不进去。
   where (select count(*) from gone) >= 0
  returning id
)
select count(*) from ins
"""

_MIME_SQL = "select mime from attachments where storage_key = :k"

_STATS_SQL = """
select role, count(*) as n, coalesce(sum(size_bytes), 0) as total
from attachments where item_id = :i group by role order by role
"""


def replace_for_item(
    conn: pg8000.native.Connection, item_id: int, blobs: Sequence[BlobLike]
) -> int:
    """整体替换某 item 的 `cover` / `image` 附件，返回写入行数。

    `blobs` 为空时**仍然执行删除**——语义是「这条内容现在没有任何封面与图片」，
    而「上次有一张、这次为零」必须让旧行消失。返回 0。

    ## 为什么空列表也要删

    若空列表直接返回 0 而不删，上游内容被改成纯文字后，库里那张旧封面会永远
    挂着——用户看到的是一张与内容无关的图，且没有任何逻辑会再碰它。

    ## 为什么 role 不在白名单时跳过而不是抛

    调用方（worker）只会传 `cover` / `image`。若传了别的，说明有人改了 plan
    却没改这里。跳过比抛好：抛会让整条 fetch 失败（`mark_failed`），
    而正确的结果是「那一条不落库、其余照常」。所以跳过并让行数如实反映。
    """
    usable = [b for b in blobs if b.role in REPLACEABLE_ROLES]
    rows = conn.run(
        _REPLACE_SQL,
        item_id=item_id,
        roles=list(REPLACEABLE_ROLES),
        roles_arr=[b.role for b in usable],
        keys_arr=[b.storage_key for b in usable],
        mimes_arr=[b.mime for b in usable],
        sizes_arr=[int(b.size_bytes) for b in usable],
        widths_arr=[b.width for b in usable],
        heights_arr=[b.height for b in usable],
        durations_arr=[b.duration_sec for b in usable],
        shas_arr=[b.sha256 for b in usable],
        orders_arr=[int(b.sort_order) for b in usable],
    )
    if not rows:
        return 0
    written = rows[0][0]
    return 0 if written is None else int(str(written))


def mime_for_key(conn: pg8000.native.Connection, key: str) -> str | None:
    """按 `storage_key` 取 mime。查不到返回 `None`。

    `None` 而不是空串：`/api/media/{key}` 要据此给 **404**，而空串会让
    `FileResponse` 拿不到 media_type、由 Starlette 按扩展名猜——那时
    「库里没有这条记录」就被伪装成「有记录但类型不明」了。
    """
    rows = conn.run(_MIME_SQL, k=key)
    if not rows:
        return None
    value = rows[0][0]
    return None if value is None else str(value)


def stats(conn: pg8000.native.Connection, item_id: int) -> dict[str, int]:
    """按 role 分组的**字节总数**。供健康检查与将来的清理逻辑用。

    只返回字节数、不返回条数：两个数各自都有用，但返回值形状固定成
    `dict[str, int]` 才能在协议里表达清楚。条数可用 `count_for_item`。

    为什么需要它：孤儿文件的清理（R9）要判断「这个 key 是否还被任何行引用」，
    而「这条内容占了多少字节」也是磁盘告警的输入。
    """
    rows = conn.run(_STATS_SQL, i=item_id)
    return {str(row[0]): int(str(row[2])) for row in rows}


def count_for_item(conn: pg8000.native.Connection, item_id: int) -> dict[str, int]:
    """按 role 分组的**条数**。与 `stats` 分开是为了让两者的返回值语义单纯。

    合成一个 `dict[str, tuple[int, int]]` 会让每个调用点都要记住元组里
    哪个是条数哪个是字节——而这两件事在排障时的用途完全不同
    （条数看「有没有下到」，字节看「磁盘涨了多少」）。
    """
    rows = conn.run(_STATS_SQL, i=item_id)
    return {str(row[0]): int(str(row[1])) for row in rows}


def primary_media_id(conn: pg8000.native.Connection, item_id: int) -> int | None:
    """这条内容的**展示图**附件 id，供写 `items.primary_media`。

    ## 为什么叫 `primary_media` 不叫 `primary_cover`

    `items.primary_media` 的语义是「**这条内容用来做展示图的那张附件**」，
    不是「`role='cover'` 的那张」。两种内容形态的答案不同：

    | 内容 | 展示图来自 |
    |---|---|
    | 视频 | `role='cover'`（`thumb_url` 与 mp4 是两个不同字节） |
    | 图文 | **第一张 `role='image'`**（根本没有 cover 行） |

    函数名带 `cover` 会诱使调用方以为「图文应该也有 cover 行」，
    而那正是会撞 `attachments_key_uniq` 的错误设计。

    ## 为什么回落做在 SQL 里，而不是让调用方传 `role`

    让调用方传 `role` 的写法是
    `primary_media_id(conn, item_id, role="cover" if has_cover else "image")`。
    它有两个问题：

    1. **调用方要知道库里有什么。** `has_cover` 通常取自 `result.stored`，
       而 `replace_for_item` 可能因 role 不在白名单而过滤掉某些 blob。
       两者不一致时传了 `'cover'` 却查到 `None`，卡片空白且没有报错。
    2. **真值源分裂。** 库里实际写进什么由本函数附近的 SQL 决定，
       让调用方在外面重新推一遍，等于同一件事算两次。

    所以排序写死在这里：`cover` 优先，没有就取 `sort_order` 最小的 image。
    第三个键 `id` 兜底，保证「同 sort_order 的两行」也有确定结果。

    ## 为什么要 `sort_order, id` 两个次要键

    一张内容最多一个封面，但「万一有多条」时结果必须确定——否则两次查询
    返回不同的 id，`items.primary_media` 会在两次 fetch 之间来回跳。
    """
    rows = conn.run(
        "select id from attachments where item_id = :i and role in ('cover', 'image') "
        "order by (role = 'cover') desc, sort_order, id limit 1",
        i=item_id,
    )
    if not rows:
        return None
    value: Any = rows[0][0]
    return None if value is None else int(str(value))

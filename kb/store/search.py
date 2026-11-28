"""检索用的只读 SQL：兜底 ILIKE、单条装配读、重建分页读。

## 本文件是**只读**的

一条写库语句都不出现在这里。`layer_rules.toml` 把写库限定在 `kb/store/`
（本文件确实在那个前缀下），但本文件的职责是「读」，所以不出现
`insert` / `update` / `delete`——这样「检索会不会改数据」这个问题
读一眼文件头就能回答。

## ⚠️ 写路径硬规则（回写设计 §10 的那一条）

**任何改动 `title` / `summary` / `content_text` / `ocr_text` / `tags` /
`kind` / `read_status` 的代码路径，必须在同一事务点之后
`jobs.enqueue(item_id, "index")`。**

不问就会出的问题：改了标题之后搜不到新标题，而索引里还是旧值。它不报错、
不写日志，只表现为「搜索结果是错的」。本轮 `PATCH /api/items/{id}` 还没实现，
所以暂时没有违反者；实现它的人必须先读这段。

漏入队的三条兜底手段：`python -m kb.worker.rebuild` 全量重灌、
`tests/test_search_e2e_pg.py` 的端到端用例、以及本段文字本身。

## 为什么本文件不 import `kb.search`

`kb/search/` 不 import `kb.store`（那条边界由 `layer_rules.toml` 守着）。
反向也不 import：本文件要返回 `IndexDoc` / `SearchHit`，它们定义在
`kb/search/models.py`。**这条反向 import 是允许的**——`kb.store` 不在这条
禁令的任一侧（禁令是「`kb/search/` 不得依赖 `kb.store`」，不是双向）。
```
kb/store/search.py  ──import──→  kb/search/models.py     （允许：只依赖纯类型）
kb/search/          ──禁止──→    kb/store/               （违反即架构检查红）
```
`kb/search/models.py` 里没有任何 I/O，所以这条依赖不会把「纯逻辑层不碰数据库」
这件事破坏掉。真正让 `Indexer` 能脱库测试的是「`kb/search/` 不 import store」，
不是「store 不 import search」。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

import pg8000.native

from kb.search.config import FALLBACK_FIELD_COUNT
from kb.search.models import IndexDoc, SearchFilter, SearchHit, SearchQuery
from kb.search.query import terms_param_names

#: DB `items_status_check` 的镜像常量（`0001_schema.sql` 第 33 行）。
#:
#: 枚举只有一个真源，镜像必须被守住——`tests/test_search_query.py` 有一条用例
#: 从 `0001_schema.sql` 正则抠出 CHECK 里的四个值与本常量比对，两处不一致就红。
#:
#: 为什么不改为运行时查 `pg_constraint`：那需要在每次校验时多一次往返，
#: 而 CHECK 值是迁移期就固定的。镜像 + 一条比对用例的成本更低、失败更早。
READ_STATUSES: tuple[str, ...] = ("unread", "reading", "read", "archived")

#: 索引写入的唯一「读库」入口用的列。与 `IndexDoc` 的字段一一对应，
#: 顺序与 `_doc_from_row` 的解包顺序必须一致。
_ROW_COLUMNS = (
    "i.id, i.title, i.summary, i.ocr_text, i.content_text, i.author, "
    "i.kind, i.status, i.saved_at, i.source_domain"
)


def _tag_names_subquery() -> str:
    """把某条内容的所有标签名聚成数组。

    `order by g.name` 不是装饰：没有它 `array_agg` 的顺序由执行计划决定，
    同一条内容的 `tags` 字段在两次 rebuild 之间可能不同——对账时会误报
    「索引里的 tags 与库不一致」。
    """
    return (
        "left join lateral ("
        " select array_agg(g.name order by g.name) as names"
        "   from item_tags it join tags g on g.id = it.tag_id"
        "  where it.item_id = i.id"
        ") t on true"
    )


def _doc_from_row(row: Sequence[object]) -> IndexDoc:
    """`_ROW_COLUMNS` + `tag_names` 的 11 列 → `IndexDoc`。

    ## `saved_at_ts` 用 `.timestamp()`

    给的是**绝对时刻**，与时区无关。索引侧的 `saved_at_ts` 因此不用改——
    要按上海时区解释的只有 `from`/`to` 这两个用户输入（设计 §3.9 末段）。

    ## `saved_at` 用 `isoformat()`

    带时区偏移的 ISO 串，只供展示。**不用它做筛选**：Meili 的字符串比较是
    字典序，`+00:00` 与 `+08:00` 在同一时刻会给出相反答案。

    ## 三处 `or ""`

    `summary` / `ocr_text` / `content_text` / `author` 在库里可能是 `NULL`。
    `IndexDoc` 的字段类型是 `str` 不是 `str | None`，所以在这里收成空串——
    索引文档里「字段不存在」与「字段为空串」在 Meili 上的行为不同
    （前者整键在响应里消失，前端要判 `undefined`）。
    """
    saved_at = row[8]
    if not isinstance(saved_at, datetime):
        raise TypeError(f"saved_at 不是 datetime：{type(saved_at).__name__}")
    # `tags` 是 `array_agg` 的产物：pg8000 给 list，`coalesce(..., '{}')` 给 list。
    # 声明 `object` 是 pg8000 的 `run` 返回类型决定的（`list[list[object]]`），
    # 所以这里必须显式收窄——直接迭代 `object` 在类型层不成立。
    raw_tags: object = row[10]
    tag_names: list[object] = raw_tags if isinstance(raw_tags, list) else []
    return IndexDoc(
        id=int(str(row[0])),
        title=str(row[1] or ""),
        summary=str(row[2] or ""),
        tags=tuple(str(name) for name in tag_names),
        ocr_text=str(row[3] or ""),
        content_text=str(row[4] or ""),
        author=str(row[5] or ""),
        kind=str(row[6] or ""),
        read_status=str(row[7] or ""),
        saved_at=saved_at.isoformat(),
        saved_at_ts=int(saved_at.timestamp()),
        source_domain=str(row[9] or ""),
    )


def index_source_row(conn: pg8000.native.Connection, item_id: int) -> IndexDoc | None:
    """装配一条索引文档。**不存在或已软删都返回 `None`。**

    `deleted_at is null` 这一条不能省：软删条目留在索引里会让卡片点进去 404
    （`/api/items/{id}` 也带同一个谓词）。rebuild 的差集会在下一轮把它清掉，
    而 `run_index` 这里直接返回 `None` 让调用方跳过。

    ## 为什么不过滤 `url is null`

    纯文字条目（`url` 为空）**也是内容**，也要能被搜到。设计 §4.4 明写
    「`run_summarize` 跳过（`no_text` / `title_only`）也要入队 index」，
    正是为了让这类条目进索引。
    """
    rows = conn.run(
        f"select {_ROW_COLUMNS}, coalesce(t.names, '{{}}') as tag_names "
        f"  from items i {_tag_names_subquery()} "
        " where i.id = :i and i.deleted_at is null",
        i=item_id,
    )
    if not rows:
        return None
    return _doc_from_row(rows[0])


def index_source_page(
    conn: pg8000.native.Connection, *, after_id: int, limit: int
) -> list[IndexDoc]:
    """按 `id > after_id order by id limit N` 分页读。rebuild 用它。

    判据「两次调用不重叠不遗漏」由**游标语义**保证：`id > after_id` 是开区间，
    `after_id` 取上一页最后一条的 id。用 `offset` 会在大库上越翻越慢，
    且期间有新内容插入时会漏条（offset 的含义随插入漂移）。

    `deleted_at is null` 同样不能省：软删条目不进索引，它们由 rebuild 的
    差集从索引里清掉。
    """
    rows = conn.run(
        f"select {_ROW_COLUMNS}, coalesce(t.names, '{{}}') as tag_names "
        f"  from items i {_tag_names_subquery()} "
        " where i.deleted_at is null and i.id > :after "
        " order by i.id "
        " limit :limit",
        after=after_id,
        limit=limit,
    )
    return [_doc_from_row(row) for row in rows]


# ── 兜底检索（设计 §6.2）──────────────────────────────────────
#
# 为什么用 ILIKE 而不是 pg_trgm：真机实测 `similarity('看完这篇你就懂了三体问题','三体')`
# = 0.000，改阈值也救不了（技术方案 §8.3.1）。中文没有空格，trigram 切出来的
# 三字组对二字查询基本没有重叠。ILIKE 的 `%词%` 是字面子串匹配，能用但走 Seq Scan。
#
# 性能口径：500 条以内毫秒级，5000 条时秒级。这是兜底路径「慢但能用」的量化
# 说明，也是「必须让用户看见降级」的理由。

#: `field_hits` 的表达式：每个字段「是否含查询里的**任一**词」，五路相加。
#:
#: `ilike any(cast(:patterns as text[]))` 的 `cast` 不能省：pg8000 把 list 参数
#: 当 `unknown` 类型发过去，`any(unknown)` 让 PG 推断不出元素类型并报
#: `could not determine data type of parameter`。显式 cast 是这里唯一可靠的写法。
#:
#: 计数口径是「该字段含任一查询词」而不是「含全部查询词」：多词时用「全部」
#: 会让 `field_hits` 几乎恒为 1（一个字段很少同时含三个词），排序信号消失。
_FIELD_HITS_EXPR = """(
  (case when i.title ilike any(cast(:patterns as text[])) then 1 else 0 end)
+ (case when coalesce(i.summary, '') ilike any(cast(:patterns as text[]))
        then 1 else 0 end)
+ (case when coalesce(i.content_text, '') ilike any(cast(:patterns as text[]))
        then 1 else 0 end)
+ (case when coalesce(i.ocr_text, '') ilike any(cast(:patterns as text[]))
        then 1 else 0 end)
+ (case when exists (
      select 1 from item_tags it
        join tags g on g.id = it.tag_id
       where it.item_id = i.id
         and g.name ilike any(cast(:patterns as text[]))
    ) then 1 else 0 end)
)"""


def _like_pattern(term: str) -> str:
    """把一个词变成 `%词%`，并转义 LIKE 的元字符。

    ## 三个必须转义的字符

    | 字符 | 不转义的后果 |
    |---|---|
    | `%` | `%100%%` 变成「含 100 再任意」，搜「100%」会命中「1000」 |
    | `_` | `_` 匹配任意单字符，搜「a_b」会命中「axb」 |
    | `\\` | 反斜杠是 LIKE 的转义符本身，不处理会把后面的字符吃掉 |

    **按字面匹配是兜底路径的整个承诺**（`notice` 原话是「结果按字面匹配」）。
    不转义元字符会让这句话变成假的。
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def fallback_search(
    conn: pg8000.native.Connection,
    *,
    terms: Sequence[str],
    limit: int,
    offset: int,
    filter: SearchFilter,
) -> list[SearchHit]:
    """Postgres `ILIKE` 兜底检索。**多词按 AND 连接，每个词跨 5 个字段 OR。**

    ## `terms` 为空 = 浏览器模式

    空查询没有词可匹配，此时按 `saved_at desc` 列出（与 Meili 的 placeholder
    search 一致）。**不能返回空列表**：那样「点开搜索框不输入」会看到空页面。

    ## `field_hits` 是「命中字段数」，不是相关度

    它是兜底路径能给出的最诚实的排序信号：命中 4 个字段的比命中 1 个的更可能
    是目标。多词时口径是「该字段至少含查询里的一个词」——用「含全部词」
    会让三词查询的 `field_hits` 几乎恒为 1，排序信号消失。

    API 里换算成 `ranking_score = field_hits / 5`，取值范围与 Meili 一致
    （0–1），语义差异由 `searchMode` 标明（设计 §6.2 第 3 点）。

    ## 参数化，不拼字符串

    `terms` 在纯函数层已切成 ≤5 个，这里只按 `len(terms)` 生成占位符名
    `t0..t4`（映射在 `query.terms_param_names`，只算一处），值全部走参数。
    **没有任何用户输入进 SQL 文本**，所以没有注入面。
    """
    terms = tuple(terms)
    where: list[str] = ["i.deleted_at is null"]
    # ⚠️ `patterns` **无条件**放进参数，即使是空查询。
    #
    # `_FIELD_HITS_EXPR` 里每个字段都写 `ilike any(cast(:patterns as text[]))`，
    # 那个占位符在 SQL 里始终存在。空查询时若把它从参数字典里删掉，
    # pg8000 会报 `There's a placeholder 'patterns' in the query, but no
    # matching keyword argument`——本机跑不出来，只有真库才炸（2026-11-28 实测）。
    #
    # 空数组的语义是正确的：`x ilike any('{}')` 恒为 false/null，
    # `field_hits` 恒为 0，排序退化为「按 saved_at 倒序」——正是浏览器模式要的。
    params: dict[str, object] = {
        "limit": limit,
        "offset": offset,
        "patterns": [_like_pattern(term) for term in terms],
    }

    if terms:
        names = terms_param_names(len(terms))
        for name, term in zip(names, terms):
            params[name] = _like_pattern(term)
        # 多词之间 AND：每个词都必须出现在至少一个字段里。
        for name in names:
            where.append(
                "("
                f"i.title ilike :{name}"
                f" or coalesce(i.summary, '') ilike :{name}"
                f" or coalesce(i.content_text, '') ilike :{name}"
                f" or coalesce(i.ocr_text, '') ilike :{name}"
                " or exists (select 1 from item_tags it2"
                "   join tags g2 on g2.id = it2.tag_id"
                f"  where it2.item_id = i.id and g2.name ilike :{name})"
                ")"
            )

    if filter.kinds:
        placeholders = ", ".join(f":kind{i}" for i in range(len(filter.kinds)))
        where.append(f"i.kind in ({placeholders})")
        for index, kind in enumerate(filter.kinds):
            params[f"kind{index}"] = kind

    if filter.read_statuses:
        placeholders = ", ".join(f":rs{i}" for i in range(len(filter.read_statuses)))
        where.append(f"i.status in ({placeholders})")
        for index, status in enumerate(filter.read_statuses):
            params[f"rs{index}"] = status

    if filter.tags:
        # 标签维度内 AND：每个标签都要有。用 `count(distinct ...) = N`
        # 而不是 N 个 `exists` ——一条子查询判完全部，PG 只扫 item_tags 一次。
        tag_names = ", ".join(f":tag{i}" for i in range(len(filter.tags)))
        where.append(
            "(select count(distinct g3.name) from item_tags it3"
            "   join tags g3 on g3.id = it3.tag_id"
            "  where it3.item_id = i.id"
            f" and g3.name in ({tag_names})"
            f") = {len(filter.tags)}"
        )
        for index, tag in enumerate(filter.tags):
            params[f"tag{index}"] = tag

    if filter.saved_from_ts is not None:
        # epoch 秒 → timestamptz 比较：`to_timestamp` 给出绝对时刻，
        # 与 `items.saved_at`（timestamptz）比较时不受会话时区影响。
        where.append("i.saved_at >= to_timestamp(:from_ts)")
        params["from_ts"] = float(filter.saved_from_ts)

    if filter.saved_to_ts is not None:
        # **半开区间 `<`**，与 Meili 侧口径一致（设计 §3.9）。
        where.append("i.saved_at < to_timestamp(:to_ts)")
        params["to_ts"] = float(filter.saved_to_ts)

    sql = (
        f"select i.id, i.kind, i.title, i.summary, i.source_domain, i.saved_at,"
        f"       i.status, coalesce(t.names, '{{}}') as tag_names,"
        f"       {_FIELD_HITS_EXPR} as field_hits "
        f"  from items i {_tag_names_subquery()} "
        f" where {' and '.join(where)} "
        f" order by field_hits desc, i.saved_at desc, i.id desc "
        f" limit :limit offset :offset"
    )
    rows = conn.run(sql, **params)
    return [_hit_from_row(row) for row in rows]


def _hit_from_row(row: Sequence[object]) -> SearchHit:
    """`fallback_search` 的 9 列 → `SearchHit`。

    `ranking_score = field_hits / 5`：取值 0.2 / 0.4 / 0.6 / 0.8 / 1.0。
    不存在 0——能出现在结果里说明至少命中一个字段。
    """
    saved_at = row[5]
    if not isinstance(saved_at, datetime):
        raise TypeError(f"saved_at 不是 datetime：{type(saved_at).__name__}")
    tags = row[7]
    field_hits = int(str(row[8]))
    # 与 `_doc_from_row` 同一个理由：pg8000 的 `run` 返回 `list[list[object]]`，
    # `array_agg` 的元素类型在静态层面是 `object`，迭代前必须显式收窄成 list。
    raw_tags: object = tags
    tag_names: list[object] = raw_tags if isinstance(raw_tags, list) else []
    return SearchHit(
        id=int(str(row[0])),
        kind=str(row[1] or ""),
        title=str(row[2] or ""),
        summary=str(row[3] or ""),
        tags=tuple(str(name) for name in tag_names),
        source_domain=str(row[4] or ""),
        saved_at=saved_at.isoformat(),
        read_status=str(row[6] or ""),
        status="",
        ranking_score=field_hits / FALLBACK_FIELD_COUNT,
    )


def fallback_for(
    conn: pg8000.native.Connection,
) -> Any:
    """把 `fallback_search` 包成 `SearchService` 要的 `FallbackFn`。

    签名是 `Callable[[SearchQuery], Sequence[SearchHit]]`——单个 `SearchQuery`
    入参。这个适配器让装配点（`kb/api/search.py`、`kb/worker/steps.py`）
    不用自己写闭包，也保证切词规则只有一处（`query.build_terms`）。

    **放在 store 层而不是 api 层**：它要调 `build_terms`（`kb.search.query`），
    而 `kb/store` 可以 import `kb.search`（见模块 docstring 的依赖图）。
    api 层写等价的闭包也行，但那会让「切词在哪切」有两个答案。
    """
    from kb.search.query import build_terms

    def _run(query: SearchQuery) -> list[SearchHit]:
        return fallback_search(
            conn,
            terms=build_terms(query.text),
            limit=query.limit,
            offset=query.offset,
            filter=query.filter,
        )

    return _run

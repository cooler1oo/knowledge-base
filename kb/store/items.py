"""items 表的读写、URL 归一化、处理状态聚合。

三条边界（技术方案 §7.5）：

1. 只有本层能写库。api 层只调用这里的函数，自己不出现任何写库 SQL。
2. 「待处理 / 处理中 / 已完成」不是 items 的列，而是从 jobs 现算的。一条内容
   可以「抓完了但没读」，也可以「读过了但还没抓完」；挤进 `items.status` 一列，
   后写的那一个必然覆盖前一个（技术方案 §8.2，v9.9）。
3. 入库与入队用**一条 CTE** 完成：单条语句天然原子，不会出现「条目进了、job 没进」。
   已在平板真库 PG 18.7 上实测：首次返回 1 行 id，同一 `url_norm` 第二次返回 0 行。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

import pg8000.native

from . import jobs

TITLE_LIMIT = 120

# 处理状态（来自 jobs 聚合），与 items.status（阅读状态）是两回事
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
NO_JOB = "none"

_WHITESPACE_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
# 抖音/小红书这类分享文案，标题前后常挂着标点与间隔号
_EDGE_PUNCT = " \t\r\n·,，。:：;；-—、"


@dataclass(frozen=True)
class ItemSummary:
    """列表项。`status` 是处理状态，`read_status` 是阅读状态。"""

    id: int
    kind: str
    url: str | None
    title: str
    source_domain: str
    saved_at: datetime
    status: str
    read_status: str
    summary: str | None = None


@dataclass(frozen=True)
class ItemDetail:
    id: int
    kind: str
    url: str | None
    title: str
    author: str | None
    source_domain: str
    summary: str | None
    content_text: str | None
    read_status: str
    status: str
    saved_at: datetime
    published_at: datetime | None
    props: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CreateResult:
    id: int
    kind: str
    status: str
    duplicate: bool


# ── URL 归一化（技术方案 §6.6，照 linkding 的 normalize_url）──────────────


def _netloc(parts: SplitResult) -> str:
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    auth = ""
    if parts.username:
        auth = parts.username
        if parts.password:
            auth = f"{auth}:{parts.password}"
        auth = f"{auth}@"
    try:
        port = parts.port
    except ValueError:  # 端口不是数字时 urlsplit 不报，取 .port 才报
        port = None
    suffix = f":{port}" if port else ""
    return f"{auth}{host}{suffix}"


def normalize_url(url: str) -> str:
    """同一链接的不同写法归一到同一个去重键。空输入返回空串。"""
    text = url.strip()
    if not text:
        return ""
    parts = urlsplit(text)
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit(
        (
            parts.scheme.lower(),
            _netloc(parts),
            parts.path.rstrip("/"),
            query,
            parts.fragment,
        )
    )


def is_http_url(url: str) -> bool:
    """只认 http/https 且必须有主机名。

    拦掉 `javascript:` 与 `ftp:` 不只是洁癖：前者会被原样塞进页面的 href，
    后者抓取层无法处理，存进来只会得到一条永远失败的 job。
    """
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(host)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return ""


# ── 标题 ────────────────────────────────────────────────────────────────


def title_from_text(text: str, limit: int = TITLE_LIMIT) -> str:
    """取首个非空行、压掉多余空白、截断。空文本返回空串。"""
    for line in text.splitlines():
        cleaned = _WHITESPACE_RE.sub(" ", line).strip()
        if cleaned:
            return cleaned[:limit]
    return ""


def title_from_share_text(raw: str, limit: int = TITLE_LIMIT) -> str:
    """分享文案里的标题：先把链接抠掉，再取首行。

    抖音的分享文案形如「7.68 复制打开抖音，看看【xxx】的作品 https://v.douyin.com/xxx/」，
    链接留在标题里既占位置又没法读。
    """
    return title_from_text(_URL_RE.sub(" ", raw), limit).strip(_EDGE_PUNCT)


# ── 处理状态 ────────────────────────────────────────────────────────────


def derive_status(pending: int, running: int, failed: int, total: int) -> str:
    """把 jobs 的计数收成一个状态。顺序有意义：running 最急，failed 最缓。"""
    if running:
        return RUNNING
    if pending:
        return PENDING
    if failed:
        return FAILED
    if total:
        return DONE
    return NO_JOB


# ── 写 ──────────────────────────────────────────────────────────────────

_CREATE_SQL = """
with new_item as (
  insert into items (kind, url, url_norm, title, source_domain, content_text)
  values (:kind, :url, :url_norm, :title, :domain, :content)
  on conflict (url_norm) where url_norm is not null and deleted_at is null
  do nothing
  returning id, kind
), queued as (
  insert into jobs (item_id, step)
  select id, :step from new_item
  on conflict (item_id, step) where state in ('pending','running') do nothing
  returning item_id
)
select id, kind from new_item
"""


def create_item(
    conn: pg8000.native.Connection,
    *,
    kind: str,
    url: str | None,
    url_norm: str | None,
    title: str,
    source_domain: str,
    content_text: str | None,
    step: str,
) -> CreateResult:
    """入库 + 入队。同一 `url_norm` 已存在时返回已有条目，`duplicate=True`。

    重复时不重新入队：幂等只承诺「不产生第二条」，上一条失败要不要重来是
    显式动作，不在这个函数里暗中再插一个 job。
    """
    rows = conn.run(
        _CREATE_SQL,
        kind=kind,
        url=url,
        url_norm=url_norm,
        title=title,
        domain=source_domain,
        content=content_text,
        step=step,
    )
    if rows:
        return CreateResult(
            id=int(rows[0][0]),
            kind=str(rows[0][1]),
            status=PENDING,
            duplicate=False,
        )

    if url_norm is None:
        # 没有 url_norm 的条目不可能触发唯一冲突，走到这里说明数据异常
        raise RuntimeError("插入未产生新条目，但没有 url_norm 可以回查")

    existing = conn.run(
        "select id, kind from items where url_norm = :u and deleted_at is null",
        u=url_norm,
    )
    if not existing:
        raise RuntimeError(f"插入后既没新增也没查到已有条目（url_norm={url_norm}）")
    item_id = int(existing[0][0])
    total, pending, running, failed = jobs.counts(conn, item_id)
    return CreateResult(
        id=item_id,
        kind=str(existing[0][1]),
        status=derive_status(pending, running, failed, total),
        duplicate=True,
    )


# ── 读 ──────────────────────────────────────────────────────────────────

_LIST_SQL = """
select i.id, i.kind, i.url, i.title, i.source_domain, i.saved_at, i.status, i.summary,
       coalesce(j.total, 0), coalesce(j.pending, 0),
       coalesce(j.running, 0), coalesce(j.failed, 0)
from items i
left join lateral (
  select count(*) as total,
         count(*) filter (where state = 'pending') as pending,
         count(*) filter (where state = 'running') as running,
         count(*) filter (where state = 'failed') as failed
  from jobs where item_id = i.id
) j on true
where i.deleted_at is null
order by i.id desc
limit :limit
"""

_DETAIL_SQL = """
select i.id, i.kind, i.url, i.title, i.author, i.source_domain, i.summary,
       i.content_text, i.status, i.saved_at, i.published_at, i.props
from items i
where i.id = :i and i.deleted_at is null
"""


def _as_int(value: object) -> int:
    """pg8000 的行元素是 object；count/serial 列要显式收口，否则 mypy 严格模式不给过。"""
    if isinstance(value, bool):
        raise TypeError("期望整数，实际 bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"期望整数，实际 {type(value).__name__}")


def _summary_from_row(row: list[object]) -> ItemSummary:
    return ItemSummary(
        id=_as_int(row[0]),
        kind=str(row[1]),
        url=None if row[2] is None else str(row[2]),
        title=str(row[3]),
        source_domain=str(row[4]),
        saved_at=_as_datetime(row[5]),
        read_status=str(row[6]),
        summary=None if row[7] is None else str(row[7]),
        status=derive_status(
            _as_int(row[9]), _as_int(row[10]), _as_int(row[11]), _as_int(row[8])
        ),
    )


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    raise TypeError(f"期望 datetime，实际 {type(value).__name__}")


def list_items(
    conn: pg8000.native.Connection, *, limit: int, before_id: int | None
) -> list[ItemSummary]:
    """按 id 倒序取一页。`before_id` 非空时只取更早的（游标翻页）。

    判空不写进 SQL：`:before is null` 会让 pg8000 在参数类型推断上卡住
    （unknown 类型的 $n 参与 is null 时 PG 无法定类型），所以按有无参数拼两段。
    """
    if before_id is None:
        rows = conn.run(_LIST_SQL, limit=limit)
    else:
        rows = conn.run(
            _LIST_SQL.replace(
                "where i.deleted_at is null",
                "where i.deleted_at is null and i.id < :before",
            ),
            limit=limit,
            before=before_id,
        )
    return [_summary_from_row(row) for row in rows]


def get_item(conn: pg8000.native.Connection, item_id: int) -> ItemDetail | None:
    rows = conn.run(_DETAIL_SQL, i=item_id)
    if not rows:
        return None
    row = rows[0]
    total, pending, running, failed = jobs.counts(conn, item_id)
    return ItemDetail(
        id=int(row[0]),
        kind=str(row[1]),
        url=None if row[2] is None else str(row[2]),
        title=str(row[3]),
        author=None if row[4] is None else str(row[4]),
        source_domain=str(row[5]),
        summary=None if row[6] is None else str(row[6]),
        content_text=None if row[7] is None else str(row[7]),
        read_status=str(row[8]),
        status=derive_status(pending, running, failed, total),
        saved_at=_as_datetime(row[9]),
        published_at=None if row[10] is None else _as_datetime(row[10]),
        props=_as_props(row[11]),
    )


def _as_props(value: object) -> dict[str, object]:
    """jsonb 可能被 pg8000 解成 dict，也可能是原始字符串，两种都认。"""
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return (
            {str(k): v for k, v in loaded.items()} if isinstance(loaded, dict) else {}
        )
    return {}


def item_jobs(conn: pg8000.native.Connection, item_id: int) -> list[dict[str, object]]:
    return jobs.rows(conn, item_id)


# ── 检索用：批量补处理状态 + 类型枚举真源（R6）──────────────────────


def summaries_by_ids(
    conn: pg8000.native.Connection, ids: list[int] | tuple[int, ...]
) -> list[ItemSummary]:
    """一次查询取回多条列表项（含 `status` 与 `read_status`）。

    ## 为什么不是 N+1

    `/api/search` 一次返回 20 条命中，逐条调 `get_item` 就是 20 次往返
    （每次还要再查一次 `jobs.counts`，实际 40 次）。这里一条 SQL 完成。

    ## 为什么查不回来的要跳过，而不是补个默认值

    `ids` 里有的、库里没有的，说明**索引里有、库已删**（软删或物理删）。
    设计 §3.7 明写：宁可少一条，也不给一个点进去 404 的卡片。
    接口返回的条数少于传入的 ids 数就是这种情况，调用方据此自然跳过。

    ## 空列表零查询

    `where id = any(cast('{}' as bigint[]))` 在 PG 上是合法且返回空集的，
    但发一次必然返回空的往返没有意义，而 rebuild / 空搜索会高频走到这里。
    """
    if not ids:
        return []
    # 参数名 `ids`，按数组传。`cast` 不能省：pg8000 把 list 当 `unknown` 发过去，
    # `any(unknown)` 让 PG 推断不出元素类型并报
    # `could not determine data type of parameter`。
    # 外层套一层 `select * from (...)`：`_LIST_SQL` 自带 `order by` 与 `limit`，
    # 不能直接把 `where` 追加到它的末尾（那样 `limit` 会变成子句顺序错误）。
    inner = _LIST_SQL.replace("order by i.id desc", "").replace("limit :limit", "")
    rows = conn.run(
        f"select * from ({inner}) sub "
        " where sub.id = any(cast(:ids as bigint[])) "
        " order by sub.saved_at desc, sub.id desc",
        ids=[int(i) for i in ids],
    )
    return [_summary_from_row(row) for row in rows]


def known_kinds(conn: pg8000.native.Connection) -> tuple[str, ...]:
    """全部合法类型，来自 `item_kinds` 表。

    **不许硬编码类型枚举**（设计 §9 T04 判据）：`items.kind` 有外键指向这张表，
    「合法类型有哪些」由数据决定。写成常量的话，用户在 `item_kinds` 里加一类
    （迁移设计明确支持「加新形态 = 插一行」）后，`/api/search?kind=新类型`
    会返 400「未知类型」——而库里明明有这类内容。

    按 `sort_order, kind` 排序：与 `item_kinds` 的展示顺序一致，
    报错消息里列出的候选值也就按同一个顺序显示。
    """
    rows = conn.run("select kind from item_kinds order by sort_order, kind")
    return tuple(str(row[0]) for row in rows)


# ── 抓取结果落库 ─────────────────────────────────────────────
#
# 参数为什么用扁平的结构化字段而不是直接收 `kb.extract.router.ExtractOutcome`：
# store 层不该知道抓取层的类型存在。收 dict 与 list 让这一层只管「怎么存」，
# 「抓到了什么」由 worker 负责翻译。这样换抓取实现时 store 一行都不用改。


def apply_extract(
    conn: pg8000.native.Connection,
    item_id: int,
    *,
    ok: bool,
    kind: str = "",
    title: str = "",
    content_text: str = "",
    platform: str = "",
    media: list[dict[str, object]] | None = None,
    error: str = "",
) -> None:
    """把一次抓取的结果写回 items。

    成功：更新 title / content_text / kind，并把媒体与时长合并进 props。
    失败：**只补空标题 + 记 fetchError**，不动 kind。

    为什么失败不改 kind：`kind='link'` 是入库时按「只有链接」定下的降级结果。
    抓取失败时把它改成别的（比如 video），等于谎报抓到了东西；改回 link 又是空操作。
    保持原值，让界面只靠 props.fetchError 显示「处理失败，可重试」。

    为什么媒体不写 attachments：那张表要求 storage_key / mime / sha256 三者非空，
    而这三样都要媒体真正下载后才有值。R5 只抓元数据不下载，硬写只能填 URL 冒充
    storage_key——那会让将来的 /api/media 出现路径穿越风险。媒体信息先进 props，
    等下载轮次（R8 之后）再转成正式附件。

    props 用 `||` 合并而非整体覆盖：入库时已写入的字段（如分享文案）被抹掉是
    静默数据丢失，不会报错。
    """
    media_list = media or []
    media_urls = [str(m.get("url", "")) for m in media_list if m.get("url")]
    media_props: dict[str, object] = {
        "platform": platform,
        "mediaCount": len(media_list),
        "mediaUrls": media_urls,
    }
    # 第一个媒体的时长就是这条内容的时长（视频只有一条；图文没有）
    first_duration = next(
        (
            m.get("durationSec")
            for m in media_list
            if isinstance(m.get("durationSec"), int)
        ),
        None,
    )
    if first_duration is not None:
        # 写成字符串：items.duration_sec 是 props->>'duration_sec' 的生成列，
        # 直接写 int 也能转型，但统一走字符串避免 (int)::text 的格式差异
        media_props["duration_sec"] = str(first_duration)

    if ok:
        # 成功时显式删掉上次的 fetchError：`||` 只会覆盖同名键，不会删掉
        # 这次没带的旧键。不删的话，重试成功后界面会一直显示「处理失败」。
        media_props.pop("fetchError", None)
        conn.run(
            "update items set "
            " title = :t, "
            " content_text = :ct, "
            " kind = :k, "
            " props = (props - 'fetchError') || cast(:p as jsonb), "
            " updated_at = now() "
            "where id = :i",
            i=item_id,
            t=title,
            ct=content_text,
            k=kind,
            p=json.dumps(media_props, ensure_ascii=False),
        )
        return

    # 失败路径：不出现 kind = 赋值
    fail_props = dict(media_props)
    fail_props["fetchError"] = error
    conn.run(
        "update items set "
        " title = case when title = '' then :t else title end, "
        " props = props || cast(:p as jsonb), "
        " updated_at = now() "
        "where id = :i",
        i=item_id,
        t=title,
        p=json.dumps(fail_props, ensure_ascii=False),
    )


# ── 摘要落库（A1 / A4 / G1 / G2 / G3）──────────────────────────────────
#
# 为什么不复用 `apply_extract`：那条 SQL 同时写 `title` / `content_text` /
# `kind`，摘要走它会**把正文覆盖成空串**。这是本轮最容易犯且最难发现的错：
# 界面上摘要正常显示，只是正文没了，而「正文」与「摘要」互相污染之后
# 检索权重也失去意义（PRD §4.7 明确不做「把摘要写进 content_text」）。
#
# 所以摘要路径的 SQL 里**不得出现** `content_text` / `kind` / `ocr_text`
# 这三个名字——`tests/test_items_ai_props.py` 的第一条用例逐字断言这一点。


def apply_summary(
    conn: pg8000.native.Connection,
    item_id: int,
    summary: str,
    basis: str,
) -> None:
    """写 `items.summary` 与 `props.summaryBasis`。**不碰其他任何列。**

    `basis` 只有两个合法值：`content`（依据正文）/ `title`（依据标题）。
    `AI-08` 要求每次成功都写它，因为用户需要知道「这条摘要读过全文还是只看过标题」
    ——抖音条目的摘要只能基于标题，照实说明比假装读过全文更可信。

    同时清掉 `summarySkipped`：上一次可能因 `no_text` 被跳过，这次成功了，
    留着旧标记会让界面一直显示「未生成摘要」。
    """
    props: dict[str, object] = {"summaryBasis": basis}
    conn.run(
        "update items set "
        " summary = :s, "
        " props = (props - 'summarySkipped') || cast(:p as jsonb), "
        " updated_at = now() "
        "where id = :i",
        i=item_id,
        s=summary,
        p=json.dumps(props, ensure_ascii=False),
    )


def mark_summary_skipped(
    conn: pg8000.native.Connection, item_id: int, reason: str
) -> None:
    """标记「这一次没产出摘要」，**`items.summary` 保持原值**。

    `reason` 只有两个合法值：
    - `no_text`（`B15`）：正文标题都空，客观无内容可摘要，零次调用
    - `title_only`（`B16`）：模型返回的摘要与标题逐字相同

    为什么不写 `summary = null`：`title_only` 只表示「这一次没产出」，
    不表示「这条内容不该有摘要」。重跑场景里把已有摘要清空是数据丢失。
    """
    conn.run(
        "update items set "
        " props = props || cast(:p as jsonb), "
        " updated_at = now() "
        "where id = :i",
        i=item_id,
        p=json.dumps({"summarySkipped": reason}, ensure_ascii=False),
    )


# ── `props.aiTags`：`summarize` → `tag` 的交接（AI-09）──────────────────
#
# 为什么存 `props` 而不是临时表 / `jobs` 加列（设计 §6.3）：
# - `props` 是长尾属性桶，写它不需要迁移，一条 `props || ...` 即可
# - 临时表要多一次迁移 + 一张表的生命周期管理（写、读、清理、崩溃后残留）
# - `jobs` 加列会让「任务」与「任务产出」混在一张表里，且 `jobs` 的行会被后续
#   轮次清理，而 `B19` 的重跑对照需要这份数据在 job 标 done 后仍可读
#
# 存**原始名**而不是 `tag_id`：`tag` 步骤要做词表校验，校验的输入必须是
# 「模型当时说了什么」。存 id 等于在 `summarize` 里就把校验做了一半，
# 而两步分开的意义正是「生成」与「校验落库」解耦（`F1` 空词表场景可证）。


def set_ai_tags_raw(
    conn: pg8000.native.Connection, item_id: int, names: tuple[str, ...] | list[str]
) -> None:
    """把模型返回的原始标签名写进 `props.aiTags`。**覆盖，不是追加。**

    为什么覆盖很重要（`B19`）：重跑三次若写成数组拼接，`aiTags` 会有 9 个名字，
    `tag` 步骤的「最多 3 个」就会基于叠加后的错误输入工作。

    空数组也写（`A5`）：「没写过」与「写了空」在重跑对照时含义不同——
    前者说明这一步还没跑过。
    """
    conn.run(
        "update items set "
        " props = props || cast(:p as jsonb), "
        " updated_at = now() "
        "where id = :i",
        i=item_id,
        p=json.dumps({"aiTags": list(names)}, ensure_ascii=False),
    )


def get_ai_tags_raw(conn: pg8000.native.Connection, item_id: int) -> tuple[str, ...]:
    """读 `props.aiTags`。条目不存在、键不存在、json 坏了都返回空元组。

    返回空元组而不是 `None`：调用方（`tag` 步骤）对「没有标签可贴」只有一种
    处理（写空、标 done），区分 `None` 与 `()` 只会让每个调用点多一个分支。
    """
    rows = conn.run("select props from items where id = :i", i=item_id)
    if not rows:
        return ()
    props = _as_props(rows[0][0])
    raw = props.get("aiTags")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def set_ai_tag_ambiguous(
    conn: pg8000.native.Connection, item_id: int, ambiguous: bool
) -> None:
    """记下「这次的标签名在词表里对应多个父标签」（`F3`）。

    为什么要把这件事写出来而不是静默取小 id：静默丢一半会让用户看到
    「AI 只打了一个标签」却不知道为什么。写一个布尔之后，界面轮次可以直接
    提示「有个标签名在多个分类下都有，只取了其中一个」。
    """
    conn.run(
        "update items set "
        " props = props || cast(:p as jsonb), "
        " updated_at = now() "
        "where id = :i",
        i=item_id,
        p=json.dumps({"aiTagAmbiguous": ambiguous}, ensure_ascii=False),
    )


# ── 媒体下载的落库收尾（R8）──────────────────────────────────
#
# 为什么单独两个函数而不复用 `apply_extract`：那条 SQL 写 title / content_text /
# kind / props(**覆盖式合并 mediaProps**)，而媒体阶段在它之后跑，
# 走它会把这些字段重新写一遍（内容已经对了，但 `props` 里 mediaCount 等会被
# 同一批值覆盖——看起来无害，实际会在「重跑 fetch 带不同媒体」时把上一批
# 的 mediaError 抹成空）。分开写让两阶段的写入范围各自封闭。


def set_media_props(
    conn: pg8000.native.Connection,
    item_id: int,
    *,
    primary_media: int | None,
    media_error: str,
    media_truncated: int,
) -> None:
    """写媒体阶段的三样产出：`primary_media` / `props.mediaError` / `props.mediaTruncated`。

    ## 三个字段各自的语义

    - `primary_media`：**「这条内容用来做展示图的那张附件」**，不是「视频附件」。
      视频条目指向**封面**。语义写错，M7-01 的卡片会拿不到图。
      `None` 表示没有封面（文章或无封面内容）——显式写 `null` 而不是「不写」：
      「上次有、这次没有」必须让旧值消失，否则卡片会显示一张与内容无关的图。
    - `props.mediaError`：**空串表示这次全部成功**，此时必须**删掉**这个键。
      `||` 只覆盖同名键、不删旧键，所以成功路径要显式 `- 'mediaError'`——
      不删的话，修好一次之后界面会一直显示「封面未能下载」。
    - `props.mediaTruncated`：被 20 张上限截掉的图片数。0 表示没截断，此时同样删键。

    ## 为什么**不写 `jobs.error`**

    `derive_status` 只要看到任意 job 是 `failed` 就把条目显示成「处理失败」。
    封面下载失败与「正文没抓到」是完全不同量级的事——前者不影响这条内容可用。
    为了把前者也标红，用户会对一堆本来能读的条目点重试，每次重试都重新走一遍
    ParseHub（10–20 秒 + 上游风控风险），而**本轮根本没有重试端点**。
    """
    payload: dict[str, object] = {}
    if media_truncated > 0:
        payload["mediaTruncated"] = media_truncated

    conn.run(
        "update items set "
        " primary_media = :pm, "
        # ⚠️ `- 'mediaTruncated'` 这一句不能删，也不能挪到 `||` 之后。
        #
        # `payload` 只在本次**有截断**（`media_truncated > 0`）时才带这个键，
        # 所以「上次截了 15 张、这次一张没截」时 `||` 不会覆盖它 —— 旧值会
        # 一直留在 `props` 里，界面永远显示「已截断 15 张」，而实际上这次
        # 全存下了。用户会以为丢了图。
        #
        # 必须**先无条件删**再 `||` 合并新值：
        #   - 本次没截断 → payload 无此键 → 删掉之后就是「没有这个键」（正确）
        #   - 本次有截断 → payload 有此键 → `||` 写回新值（正确）
        #
        # `mediaError` 走 `case` 三态（删/写），`mediaTruncated` 走「先删后
        # 合并」二态 —— 两者形态不同是因为前者有三态语义（本次成功/本次失败/
        # 本次没跑媒体阶段），后者只有两种（截了 N 张 / 没截）。
        " props = ((case when :err = '' then props - 'mediaError' "
        "                else props || cast(:e as jsonb) end) "
        "          - 'mediaTruncated') "
        "         || cast(:p as jsonb), "
        " updated_at = now() "
        "where id = :i",
        i=item_id,
        pm=primary_media,
        err=media_error,
        e=json.dumps({"mediaError": media_error}, ensure_ascii=False),
        p=json.dumps(payload, ensure_ascii=False),
    )


def clear_media_props(conn: pg8000.native.Connection, item_id: int) -> None:
    """把 `primary_media` 置空并删掉两个媒体错误键。

    用在哪：抓取失败（`ok=False`）时不该留着上一次的封面与错误串——
    上次的封面属于上一版内容，留着会让用户看到一张错的图。而这次没有媒体
    阶段可跑（`run_fetch` 在 `not outcome.ok` 时直接 return）。
    """
    conn.run(
        "update items set "
        " primary_media = null, "
        " props = props - 'mediaError' - 'mediaTruncated', "
        " updated_at = now() "
        "where id = :i",
        i=item_id,
    )


def set_kind(conn: pg8000.native.Connection, item_id: int, *, kind: str) -> bool:
    """改一条内容的类型。返回**是否真的改到了**（`False` = 没有这条）。

    ## kind 合法性由调用方先查（`domain_rules.remember` 里就查了）

    这里只写库。不在本函数里再查一次 `item_kinds`，理由：修类型这个动作
    **必然**同时调 `domain_rules.remember`（否则用户改了不会被记住，M3-08
    就是空的），而那个函数已经校验过。两处都查是两次往返，且第二次与第一次
    的空隙里表可能变——错误消息会与校验结果不一致。

    外键（`items.kind references item_kinds`）仍是最后一道防线：万一将来
    有人绕过 remember 直接调本函数，数据库会拒绝，不会静默写入脏值。

    ## 为什么不用 `returning`

    `update ... returning id` 与单独查一次等价，但 `row_count` 在 pg8000 上
    的行为版本间有差异（1.31.6 有，更早的没有），而 `returning` 是 SQL 标准、
    语义自证。与 `domain_rules.forget` 同一手法。

    ## 不改 `updated_at` 之外的东西

    改类型不影响标题、正文、标签——那些是「内容」，kind 是「归类」。
    但 `updated_at` 要动：它是「这条记录最后一次被用户操作」的依据，
    将来做「最近整理过的」列表要靠它。
    """
    rows = conn.run(
        "update items set kind = :k, updated_at = now() where id = :i returning id",
        k=kind,
        i=item_id,
    )
    return bool(rows)

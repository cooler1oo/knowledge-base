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

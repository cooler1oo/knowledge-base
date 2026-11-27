"""`items` 的四个新能力：摘要落库、跳过标记、AI 标签原始名读写。

**三条不能越的界**（`G1` / `G2` / `G3`）：摘要写入不得污染 `content_text`、
不得改 `kind`、不得写 `ocr_text`。现有 `apply_extract` 一条 SQL 同时写
`title` / `content_text` / `kind`，复用它会把正文覆盖成空——这是本轮最容易
犯且最难发现的错（界面正常，只是正文没了）。所以必须有独立的
`apply_summary`，且下面第一条用例逐字断言。

**`props` 一律用 `||` 合并**，与 `apply_extract` 一致：整体覆盖会静默抹掉
入库时写入的字段（如分享文案 `rawText`），不报错也不留痕。
"""

from __future__ import annotations

import pytest

from kb.store import items as items_store


class _Recorder:
    """假连接：记下 SQL 与参数。"""

    def __init__(self, rows: list[list[object]] | None = None) -> None:
        self.seen: list[str] = []
        self.params: list[dict[str, object]] = []
        self._rows = rows or []

    def run(self, sql: str, **params: object) -> list[list[object]]:
        self.seen.append(" ".join(sql.split()))
        self.params.append(params)
        return self._rows


def _joined(conn: _Recorder) -> str:
    return " | ".join(conn.seen).lower()


def _params(conn: _Recorder, key: str) -> list[object]:
    return [p[key] for p in conn.params if key in p]


# ── apply_summary：只写 summary 与 props（G1 / G2 / G3）────────


def test_apply_summary不改content_text不改kind不改ocr_text_G1_G2_G3():
    """`G1`/`G2`/`G3` 三条一次性断言。

    `apply_summary` 的 SQL 里**不得出现** `content_text` / `kind` / `ocr_text`
    的赋值。这不是风格问题：复用 `apply_extract` 会把正文写成空串，
    条目的「正文」与「摘要」两个字段互相污染，检索权重随之失去意义
    （PRD §4.7 明确不做）。
    """
    conn = _Recorder()
    items_store.apply_summary(conn, 1, "一段十四个字的摘要内容吧", "content")

    sql = _joined(conn)
    assert "content_text" not in sql
    assert "kind" not in sql
    assert "ocr_text" not in sql
    assert "summary = :s" in sql


def test_apply_summary写summaryBasis_A1_A4():
    """A1（`content`）/ A4（`title`）：每次成功都写 `props.summaryBasis`。"""
    conn = _Recorder()
    items_store.apply_summary(conn, 1, "一段十四个字的摘要内容吧", "title")

    payload = str(_params(conn, "p")[0])
    assert "summaryBasis" in payload
    assert "title" in payload


def test_apply_summary用合并而不是整体覆盖props():
    """`props || cast(:p as jsonb)` 才能保住入库时写进去的分享文案。

    断言看的是「有没有 `||` 合并」而不是「`props ||` 这个字面量」：
    本实现写的是 `(props - 'summarySkipped') || cast(...)`（先删旧标记再合并），
    仍然满足「不整体覆盖」这条语义。
    """
    conn = _Recorder()
    items_store.apply_summary(conn, 1, "一段十四个字的摘要内容吧", "content")
    sql = _joined(conn)
    assert "|| cast(:p as jsonb)" in sql, "整体覆盖会抹掉入库时的 props"
    assert "props = cast(:p as jsonb)" not in sql


def test_apply_summary同时清掉上次的summarySkipped():
    """重跑成功后不能还留着「跳过」的标记，否则界面会一直显示「未生成摘要」。"""
    conn = _Recorder()
    items_store.apply_summary(conn, 1, "一段十四个字的摘要内容吧", "content")
    sql = _joined(conn)
    assert "summaryskipped" in sql.lower(), f"没有清掉 summarySkipped：{sql}"


def test_apply_summary更新时间戳():
    conn = _Recorder()
    items_store.apply_summary(conn, 1, "一段十四个字的摘要内容吧", "content")
    assert "updated_at = now()" in _joined(conn)


# ── mark_summary_skipped（B15 / B16）──────────────────────────


def test_mark_summary_skipped写summarySkipped且summary保持null_B15_B16():
    """`B15`/`B16`：**不写 `items.summary`**（保持 NULL），只写跳过原因。

    写 `summary = null` 也不对：那会让重跑场景里「本来有摘要」的条目被清空，
    而 `title_only` 只表示「这一次没产出」，不表示「这条内容不该有摘要」。
    """
    conn = _Recorder()
    items_store.mark_summary_skipped(conn, 1, "no_text")

    sql = _joined(conn)
    assert "summary = " not in sql, "跳过时不得写 items.summary"
    assert "summarySkipped" in str(conn.params[0]["p"])
    assert "no_text" in str(conn.params[0]["p"])


def test_mark_summary_skipped允许两种原因_no_text与title_only():
    for reason in ("no_text", "title_only"):
        conn = _Recorder()
        items_store.mark_summary_skipped(conn, 1, reason)
        assert reason in str(conn.params[0]["p"])


# ── set_ai_tags_raw / get_ai_tags_raw（AI-09 / B19）────────────


def test_aiTags写入后可原样读回_AI_09():
    """AI-09：`summarize` 成功后把模型返回的原始标签名写进 `props.aiTags`。

    这一条是 `summarize` → `tag` 的交接：`tag` 步骤读它、校验词表、写 `item_tags`。
    方案 §6.4 把两步分开却没说标签名在哪存，`props.aiTags` 是本轮的答案
    （写 `jobs.error` 是滥用，临时表要多一次迁移）。
    """
    conn = _Recorder(rows=[['{"aiTags": ["健康", "穿搭"]}']])
    names = items_store.get_ai_tags_raw(conn, 1)
    assert names == ("健康", "穿搭")


def test_重复写入aiTags是覆盖不是追加_B19前置():
    """`B19` 的重跑场景：`||` 合并语义下同名键被**覆盖**，不是追加。

    若写成数组拼接，重跑三次后 `aiTags` 会有 9 个名字，`tag` 步骤的
    「最多 3 个」会基于错误的输入工作。
    """
    conn = _Recorder()
    items_store.set_ai_tags_raw(conn, 1, ("健康", "穿搭"))

    sql = _joined(conn)
    assert "props ||" in sql, "整体覆盖会抹掉其他 props 键"
    assert "aiTags" in str(conn.params[0]["p"])


def test_set_ai_tags_raw写空数组也写进去_A5():
    """A5：模型返回空标签数组时，`aiTags` 要写成 `[]` 而不是不写。

    「没写过」与「写了空」在重跑对照时含义不同：前者说明这一步没跑过。
    """
    conn = _Recorder()
    items_store.set_ai_tags_raw(conn, 1, ())
    assert "aiTags" in str(conn.params[0]["p"])
    assert "[]" in str(conn.params[0]["p"])


def test_get_ai_tags_raw在aiTags不存在时返回空元组():
    conn = _Recorder(rows=[["{}"]])
    assert items_store.get_ai_tags_raw(conn, 1) == ()


def test_get_ai_tags_raw在条目不存在时返回空元组():
    conn = _Recorder(rows=[])
    assert items_store.get_ai_tags_raw(conn, 1) == ()


def test_get_ai_tags_raw对非字符串元素做过滤():
    """防御性：`props` 是用户可手改的 jsonb，里面塞了什么都有可能。"""
    conn = _Recorder(rows=[['{"aiTags": ["健康", 1, null]}']])
    assert items_store.get_ai_tags_raw(conn, 1) == ("健康",)


def test_get_ai_tags_raw在json坏了时不抛异常():
    conn = _Recorder(rows=[["不是 json"]])
    assert items_store.get_ai_tags_raw(conn, 1) == ()


# ── set_ai_tag_ambiguous（F3）─────────────────────────────────


def test_同名多父标记写进props_F3():
    """F3：`aiTagAmbiguous=true`。让用户能看出「AI 只打了一个标签」不是漏跑。"""
    conn = _Recorder()
    items_store.set_ai_tag_ambiguous(conn, 1, True)
    assert "aiTagAmbiguous" in str(conn.params[0]["p"])
    assert "true" in str(conn.params[0]["p"]).lower()


# ── 真库行为 ──────────────────────────────────────────────────


@pytest.fixture
def pg_conn(pg_cfg):
    from kb.store.db import connect

    c = connect(pg_cfg)
    c.run("begin")
    try:
        yield c
    finally:
        c.run("rollback")
        c.close()


@pytest.fixture
def pg_item(pg_conn):
    """造一条有正文、有 kind、有 ocr_text 的条目，供「不被污染」的断言使用。"""
    item_id = pg_conn.run(
        "insert into items (kind, title, content_text, ocr_text, props)"
        " values ('article', '原标题', '原正文内容', '原OCR文字',"
        ' \'{"rawText": "入库时的分享文案"}\'::jsonb) returning id'
    )[0][0]
    return int(str(item_id))


@pytest.mark.pg
def test_apply_summary真库不改content_text与kind与ocr_text_G1_G2_G3(pg_conn, pg_item):
    """G1/G2/G3：真库上逐字断言「正文、类型、OCR 文字」都没变。"""
    items_store.apply_summary(pg_conn, pg_item, "一段十四个字的摘要内容吧", "content")

    row = pg_conn.run(
        "select summary, content_text, kind, ocr_text, props from items where id = :i",
        i=pg_item,
    )[0]
    assert row[0] == "一段十四个字的摘要内容吧"
    assert row[1] == "原正文内容", "G1：摘要污染了正文"
    assert row[2] == "article", "G2：摘要改了 kind"
    assert row[3] == "原OCR文字", "G3：摘要写了 ocr_text"


@pytest.mark.pg
def test_apply_summary真库保留其他props(pg_conn, pg_item):
    items_store.apply_summary(pg_conn, pg_item, "一段十四个字的摘要内容吧", "title")

    props = items_store.get_item(pg_conn, pg_item)
    assert props is not None
    assert props.props.get("rawText") == "入库时的分享文案", "整体覆盖把分享文案抹了"
    assert props.props.get("summaryBasis") == "title"


@pytest.mark.pg
def test_mark_summary_skipped真库不改summary_B15(pg_conn, pg_item):
    # 先放一个摘要进去，验证「跳过」不会把它清掉
    items_store.apply_summary(pg_conn, pg_item, "原本就有的一段中文摘要", "content")
    items_store.mark_summary_skipped(pg_conn, pg_item, "title_only")

    detail = items_store.get_item(pg_conn, pg_item)
    assert detail is not None
    assert detail.summary == "原本就有的一段中文摘要", "跳过不该清空已有摘要"
    assert detail.props.get("summarySkipped") == "title_only"


@pytest.mark.pg
def test_aiTags真库原样读回_AI_09(pg_conn, pg_item):
    items_store.set_ai_tags_raw(pg_conn, pg_item, ("健康", "穿搭"))
    assert items_store.get_ai_tags_raw(pg_conn, pg_item) == ("健康", "穿搭")


@pytest.mark.pg
def test_aiTags真库重复写入是覆盖不是追加_B19(pg_conn, pg_item):
    items_store.set_ai_tags_raw(pg_conn, pg_item, ("健康", "穿搭"))
    items_store.set_ai_tags_raw(pg_conn, pg_item, ("工作赚钱",))
    assert items_store.get_ai_tags_raw(pg_conn, pg_item) == ("工作赚钱",)


@pytest.mark.pg
def test_aiTags真库写入空数组后读回空元组_A5(pg_conn, pg_item):
    items_store.set_ai_tags_raw(pg_conn, pg_item, ())
    assert items_store.get_ai_tags_raw(pg_conn, pg_item) == ()


@pytest.mark.pg
def test_repeated_apply_summary_keeps_other_props(pg_conn, pg_item):
    """重跑两次，`props` 里的其他键一次都不能丢。"""
    items_store.set_ai_tags_raw(pg_conn, pg_item, ("健康",))
    items_store.apply_summary(pg_conn, pg_item, "第一次的摘要内容十几个字", "content")
    items_store.apply_summary(pg_conn, pg_item, "第二次的摘要内容十几个字", "title")

    detail = items_store.get_item(pg_conn, pg_item)
    assert detail is not None
    assert detail.summary == "第二次的摘要内容十几个字"
    assert detail.props.get("rawText") == "入库时的分享文案"
    assert detail.props.get("aiTags") == ["健康"]


# ── B20：入库入队必须带 on conflict ────────────────────────────


def test_入队SQL带on_conflict_B20():
    """**B20：重复入库同一条目时，`insert into jobs` 不得抛唯一约束冲突。**

    ⚠️ **这是一个真实 bug 的守门用例，平板真库上才暴露。**

    `_CREATE_SQL` 的 CTE 里第二段 `insert into jobs` 原本是裸插：

    ```sql
    ), queued as (
      insert into jobs (item_id, step)
      select id, :step from new_item
      returning item_id
    )
    ```

    后果：同一条目重复入库时（用户重复分享同一个链接），`jobs_pending_uniq`
    是部分唯一索引 `on jobs (item_id, step) where state in ('pending','running')`，
    裸插直接抛 `23505 duplicate key`，**整个 `create_item` 失败 ——
    用户看到「保存失败」而不是「已存在」**。

    为什么本机的假连接发现不了：`_Recorder.run` 只记 SQL 字符串、不做 PG
    解析，唯一约束是数据库执行时的事。**这是「假连接能过、真库必炸」的第三例**
    （前两例：42P08 参数类型、`jobs.error` 缺响应原文）。

    所以用静态断言守形状，再用真库用例（`@pytest.mark.pg`）守行为。
    """
    from kb.store.items import _CREATE_SQL

    sql = _CREATE_SQL.lower()
    assert "insert into jobs" in sql, "_CREATE_SQL 里没有入队语句"
    # 谓词必须完整：不写 `where state in (...)` PG 找不到部分唯一索引做仲裁
    assert "on conflict (item_id, step) where state in ('pending','running')" in sql, (
        "入队语句缺 on conflict 仲裁目标（照 jobs.enqueue 的写法）"
    )
    assert "do nothing" in sql


def test_入队on_conflict必须带state谓词_B20():
    """部分唯一索引的仲裁目标**必须带谓词**，否则 PG 报「没有匹配的唯一约束」。

    `jobs_pending_uniq` 是 `on jobs (item_id, step) where state in ('pending','running')`。
    `on conflict (item_id, step) do nothing` 少了 `where`，PG 找不到对应的
    唯一索引，整条语句报 `42P10 invalid_column_reference`。

    （这条是 `jobs.enqueue` 的 docstring 早就写过的事实，本轮 `items.py`
    这条路径漏了 —— 所以单独钉一条，防止再漏。）
    """
    import re

    from kb.store.items import _CREATE_SQL

    # 从 `on conflict (item_id, step)` 之后截到 `do nothing`，中间那段就是仲裁谓词。
    # 用 `re.escape` 处理括号，避免把 `(item_id, step)` 里的逗号当成终止符。
    pattern = (
        re.escape("on conflict (item_id, step)") + r"(.*?)" + re.escape("do nothing")
    )
    match = re.search(pattern, _CREATE_SQL.lower(), re.S)
    assert match, "没找到 on conflict (item_id, step) ... do nothing"
    clause = match.group(1)
    assert "where state in" in clause, (
        f"on conflict 的仲裁目标缺 state 谓词，实际子句：{clause!r}"
    )

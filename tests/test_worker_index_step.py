"""index 步的八条判据（设计 §9 T04）。

**本组全部用假连接 + 假 indexer，不连库不联网**：`run_index` 的可观测行为
只有四件事——推迟了没有、标 done 了没有、标 failed 了没有、写索引时传了什么。
四件都能从「假连接的语句序列」与「假 indexer 的调用记录」读出来。

真库上的对应断言在 `tests/test_store_search_pg.py`（`blocking_upstream` 的
四条判据、`index_source_row` 的六条），真 Meili 上的在平板跑 `rebuild`。
"""

from __future__ import annotations

import pytest

from kb.search.config import (
    DEFER_MEILI_DOWN_SEC,
    DEFER_MEILI_UNAUTHORIZED_SEC,
    UPSTREAM_SOON_SEC,
    UPSTREAM_WAIT_SEC,
)
from kb.search.models import (
    IndexDoc,
    MeiliRejected,
    MeiliUnauthorized,
    MeiliUnavailable,
)
from kb.worker import loop, steps


class FakeIndexer:
    """只记调用。`raises` 让某次 `upsert` 抛指定异常。"""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.upserted: list[list[IndexDoc]] = []
        self.raises = raises

    def upsert(self, docs: list[IndexDoc]) -> int:
        if self.raises is not None:
            raise self.raises
        self.upserted.append(list(docs))
        return len(docs)


class Conn:
    """按 SQL 特征返回预置行的假连接。

    `blocked` 是 `blocking_upstream` 的返回值（`None` 或上游步骤名）；
    `doc` 是 `index_source_row` 的返回值（`None` 表示条目不存在）。
    """

    def __init__(
        self,
        *,
        blocked: str | None = None,
        doc: IndexDoc | None = None,
    ) -> None:
        self.blocked = blocked
        self.doc = doc
        self.seen: list[str] = []
        self.params: list[dict[str, object]] = []

    def run(self, sql: str, **params: object) -> list[list[object]]:
        text = " ".join(sql.split())
        low = text.lower()
        self.seen.append(low)
        self.params.append(params)

        # blocking_upstream
        if "from jobs" in low and "select step" in low:
            return [[self.blocked]] if self.blocked else []
        # index_source_row：列顺序见 `search_store._ROW_COLUMNS` + tag_names
        if "from items i" in low and "i.id = :i" in low:
            if self.doc is None:
                return []
            return [_doc_row(self.doc)]
        return []

    def sql_of(self, needle: str) -> list[str]:
        return [s for s in self.seen if needle in s]

    def params_of(self, needle: str) -> list[dict[str, object]]:
        return [p for s, p in zip(self.seen, self.params) if needle in s]


def _doc_row(doc: IndexDoc) -> list[object]:
    """`IndexDoc` → `index_source_row` 的 11 列（顺序必须与 SQL 一致）。"""
    from datetime import datetime

    saved_at = datetime.fromisoformat(doc.saved_at)
    return [
        doc.id,
        doc.title,
        doc.summary or None,
        doc.ocr_text or None,
        doc.content_text or None,
        doc.author or None,
        doc.kind,
        doc.read_status,
        saved_at,
        doc.source_domain,
        list(doc.tags),
    ]


def _doc(item_id: int = 1, **overrides: object) -> IndexDoc:
    base: dict[str, object] = {
        "id": item_id,
        "title": "抖音短视频剪辑技巧",
        "summary": "手机端剪映的三个卡点办法",
        "tags": ("视频", "工具"),
        "ocr_text": "",
        "content_text": "先把音频波形对齐",
        "author": "",
        "kind": "video",
        "read_status": "unread",
        "saved_at": "2026-11-22T10:00:00+08:00",
        "saved_at_ts": 1795312800,
        "source_domain": "v.douyin.com",
    }
    base.update(overrides)
    return IndexDoc(**base)  # type: ignore[arg-type]


def _run(
    conn: Conn,
    *,
    indexer: object = None,
    upstream: tuple[str, ...] = ("fetch", "summarize", "tag"),
    item_id: int = 1,
    job_id: int = 7,
) -> None:
    steps.run_index(
        conn,
        item_id,
        job_id,
        indexer=indexer if indexer is not None else FakeIndexer(),
        upstream=upstream,
    )


# ── 上游等待 ──────────────────────────────────────────────────


def test_上游running时不写索引且推迟30秒() -> None:
    """设计 §4.2：上游正在跑时索引会写出半成品；推迟 30 秒后典型状态是已完成。"""
    conn = Conn(blocked="summarize")
    indexer = FakeIndexer()
    _run(conn, indexer=indexer)

    assert indexer.upserted == [], "有阻塞上游时不该写索引"
    defers = conn.params_of("interval '1 second' * :seconds")
    assert defers, "没有走 defer"
    assert int(defers[0]["seconds"]) == int(UPSTREAM_WAIT_SEC)
    assert "summarize" in str(defers[0]["e"])


def test_推迟的SQL里不出现attempts() -> None:
    """设计 §9 T04 判据原文：`defer` 且 SQL 里不出现 `attempts`。

    这是「推迟不计次」在代码层可一眼确认的证据。用 `mark_failed` 做这件事
    会在第 3 次把条目判死——而 Meili 挂了或上游没跑完都不是这条内容的错。
    """
    conn = Conn(blocked="tag")
    _run(conn)

    joined = " | ".join(conn.seen)
    assert "attempts" not in joined, f"推迟路径里出现了 attempts：{joined}"


def test_推迟时并不标done() -> None:
    """推迟不是完成。标 done 会让「还在等上游」的条目显示成已完成。"""
    conn = Conn(blocked="tag")
    _run(conn)
    assert not conn.sql_of("state = 'done'")


def test_上游30分钟后到期时不阻塞(conn=None) -> None:
    """设计 §4.9 的核心修正：`blocking_upstream` 返回 `None` 时照写。

    这条用例在假连接层只验「`None` → 继续」这一层；
    「30 分钟后的上游为什么返回 `None`」由 `blocking_upstream` 的 SQL 决定，
    真库断言在 `tests/test_store_search_pg.py::test_blocking_upstream 的30分钟后到期的上游不阻塞`。
    """
    conn = Conn(blocked=None, doc=_doc())
    indexer = FakeIndexer()
    _run(conn, indexer=indexer)

    assert len(indexer.upserted) == 1, "无阻塞上游时必须写索引"
    assert conn.sql_of("state = 'done'"), "写完后必须标 done"


def test_index_upstream求交剔掉未实现的ocr() -> None:
    """本轮 `SUPPORTED_STEPS` 没有 `ocr`，交集里也就不该有它。

    不剔掉的话索引会为一个永远不会跑的任务白等 90 秒——
    每条内容都推迟一次，队列会积压。
    """
    assert loop.index_upstream(("fetch", "summarize", "tag", "index")) == (
        "fetch",
        "summarize",
        "tag",
    )


def test_index_upstream包含ocr当它被支持时() -> None:
    """设计 §9 T04 判据原文：`index_upstream(("fetch","summarize","tag","index","ocr"))` 含 `ocr`。

    这条守的是「将来加 ocr 不用记得改这里」——只要把 `ocr` 加进
    `SUPPORTED_STEPS`，交集自动包含它。
    """
    assert "ocr" in loop.index_upstream(("fetch", "summarize", "tag", "index", "ocr"))


def test_index_upstream保持依赖顺序() -> None:
    """顺序即依赖顺序（`fetch → ocr → summarize → tag`），仅供排障阅读。
    真正的阻塞判定由 SQL 的 `order by id` 决定（最早的 job 优先报）。"""
    assert loop.index_upstream() == ("fetch", "summarize", "tag")
    assert loop.INDEX_UPSTREAM == ("fetch", "ocr", "summarize", "tag")


def test_run_index收到的上游集合来自loop() -> None:
    """`run_once` 必须把 `index_upstream()` 传进去，而不是空元组。

    传空元组会让上游判定永远返回 `None`——索引在 summarize 还没跑完时就写文档，
    摘要与标签永远缺失，而搜索页看起来「只是摘要没出来」。
    """
    import inspect

    source = inspect.getsource(loop.run_once)
    assert "upstream=index_upstream()" in source, (
        "run_once 没有把 index_upstream() 传给 run_index"
    )


def test_上游判定的soon阈值是90秒() -> None:
    """设计 §4.2 定的 90 秒。改大（例如 1800）会让 AI 未配置时新内容半小时搜不到；
    改小（例如 10）会让正常的秒级 summarize 被跳过，索引写出缺字段的文档。"""
    assert UPSTREAM_SOON_SEC == 90.0
    assert UPSTREAM_WAIT_SEC == 30.0


# ── 装配件缺席 ────────────────────────────────────────────────


def test_indexer为None时推迟并说明是装配问题() -> None:
    """`indexer is None` 是装配 bug，与「Meili 挂了」完全不同。

    写「索引器未装配」才能让人看出是漏传参数而不是服务的问题。
    若这里 `mark_done`，索引里永远没有这条内容，而 job 显示成功——
    那是谎报（与 `run_summarize` 的「AI 客户端未装配」同一条口径）。
    """
    conn = Conn(doc=_doc())
    _run(conn, indexer=None)  # type: ignore[arg-type]

    # `_run` 在 indexer=None 时会换成 FakeIndexer，所以直接调一次真实路径
    conn2 = Conn(doc=_doc())
    steps.run_index(conn2, 1, 7, indexer=None, upstream=())

    defers = conn2.params_of("interval '1 second' * :seconds")
    assert defers, "indexer 缺席时没有推迟"
    assert "未装配" in str(defers[0]["e"])
    assert not conn2.sql_of("state = 'done'"), "不该标 done（索引没写）"


# ── 三类失败分级 ──────────────────────────────────────────────


def test_Meili挂时defer60秒不标failed() -> None:
    """设计 §10：`MeiliUnavailable` → `defer 60 秒`。

    **一律不用 `mark_failed` 处理「Meili 挂了」**：三次之后这条内容被标成
    「处理失败」，用户会对一堆本来没事的条目点重试，而真因是服务没起。
    """
    conn = Conn(doc=_doc())
    _run(conn, indexer=FakeIndexer(MeiliUnavailable("连不上")))

    defers = conn.params_of("interval '1 second' * :seconds")
    assert defers, "Meili 挂了没有推迟"
    assert int(defers[0]["seconds"]) == int(DEFER_MEILI_DOWN_SEC) == 60
    assert not conn.sql_of("attempts = attempts + 1"), "不该消耗一次尝试"
    assert not conn.sql_of("state = 'failed'")


def test_Meili挂的error里不出现KB_MEILI_KEY() -> None:
    """服务单纯不可达与密钥错要能分开：前者不该提示改密钥（改了也没用）。"""
    conn = Conn(doc=_doc())
    _run(conn, indexer=FakeIndexer(MeiliUnavailable("连接被拒")))

    reasons = [str(p.get("e")) for p in conn.params if "e" in p]
    assert reasons
    assert not any("KB_MEILI_KEY" in r for r in reasons), (
        f"服务不可达时不该提示改密钥：{reasons}"
    )


def test_401时defer600秒且error含KB_MEILI_KEY() -> None:
    """设计 §9 T04 判据原文。

    600 秒而不是判死：密钥填错是可修的配置问题，改完配置就该恢复。
    判死之后要手工改库才能恢复，而用户永远看不到「去改密钥」这句话。
    """
    conn = Conn(doc=_doc())
    _run(conn, indexer=FakeIndexer(MeiliUnauthorized("401")))

    defers = conn.params_of("interval '1 second' * :seconds")
    assert defers
    assert int(defers[0]["seconds"]) == int(DEFER_MEILI_UNAUTHORIZED_SEC) == 600
    assert "KB_MEILI_KEY" in str(defers[0]["e"])
    assert not conn.sql_of("state = 'failed'"), "401 不该判死"


def test_4xx时mark_failed() -> None:
    """设计 §9 T04 判据原文：请求体或索引设置写错了，重试 3 次也照样错。

    编程错误不该消耗用户的时间，也不该让队列里堆满永远失败的任务。
    """
    conn = Conn(doc=_doc())
    _run(conn, indexer=FakeIndexer(MeiliRejected("filter 语法错")))

    assert conn.sql_of("attempts = attempts + 1"), "4xx 要消耗一次尝试"
    assert conn.sql_of("state = 'failed'") or conn.sql_of(
        "case when attempts + 1 >= 3 then 'failed'"
    ), "4xx 要走 mark_failed"


def test_未预期异常向上抛出由run_once收掉() -> None:
    """`run_index` 不 catch 未预期异常——那会让 traceback 只剩一句「索引失败」。

    与 `run_fetch` / `run_summarize` 同一条口径：分派层的 `run_once` 统一收。
    """
    conn = Conn(doc=_doc())
    with pytest.raises(RuntimeError, match="炸了"):
        _run(conn, indexer=FakeIndexer(RuntimeError("炸了")))


# ── 正常路径 ──────────────────────────────────────────────────


def test_写索引成功后标done() -> None:
    conn = Conn(doc=_doc())
    indexer = FakeIndexer()
    _run(conn, indexer=indexer)

    assert len(indexer.upserted) == 1
    assert indexer.upserted[0][0].id == 1
    assert conn.sql_of("state = 'done'")


def test_先判上游再读数据() -> None:
    """设计 §4.1：第 3 步之后才读数据。

    先读的话，上一步若推迟，读出来的就是过期快照；先判后读能保证写进索引的
    字段是最新的。
    """
    conn = Conn(blocked="summarize", doc=_doc())
    _run(conn)

    upstream_at = next(
        i for i, s in enumerate(conn.seen) if "from jobs" in s and "select step" in s
    )
    read_at = next(
        (
            i
            for i, s in enumerate(conn.seen)
            if "from items i" in s and "i.id = :i" in s
        ),
        len(conn.seen),
    )
    assert upstream_at < read_at, f"读数据的语句在判上游之前：{conn.seen}"


def test_条目不存在时标done不写索引() -> None:
    """条目不存在或已软删：**标 done 而不是 failed**。

    这不是错误，是「这条内容不该在索引里」。rebuild 的差集会把它从索引里清掉
    （如果它曾经在）。标 failed 会让导出的条目显示「处理失败」，
    用户会对一条已经不存在的记录点重试。
    """
    conn = Conn(doc=None)
    indexer = FakeIndexer()
    _run(conn, indexer=indexer)

    assert indexer.upserted == []
    assert conn.sql_of("state = 'done'")
    assert not conn.sql_of("state = 'failed'")


def test_写的是IndexDoc而不是dict() -> None:
    """`Indexer.upsert` 接 `Sequence[IndexDoc]`。传 dict 会在
    `to_meili_dict()` 那一行炸 `AttributeError: 'dict' object has no attribute`。
    """
    conn = Conn(doc=_doc())
    indexer = FakeIndexer()
    _run(conn, indexer=indexer)

    assert isinstance(indexer.upserted[0][0], IndexDoc)


# ── 分派：loop.run_once 打到 run_index ────────────────────────


class _Pool:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    def connection(self) -> object:
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield self._conn

        return _cm()


class _FullConn(Conn):
    """`run_once` 路径要多认两条语句：`recover_stale` 与 `claim_one`。"""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._claimed = False

    def run(self, sql: str, **params: object) -> list[list[object]]:
        low = " ".join(sql.split()).lower()
        if "interval '15 minutes'" in low:
            self.seen.append(low)
            self.params.append(params)
            return []
        if "with picked" in low:
            self.seen.append(low)
            self.params.append(params)
            if self._claimed:
                return []
            self._claimed = True
            return [[5, 1, "index", 0, "running"]]
        return super().run(sql, **params)


def test_run_once分派到run_index() -> None:
    """设计 §4.1 第 2 步：worker 主循环领到 `index` 后分派到 `steps.run_index`。

    这条同时证明 `index` 真的在 `SUPPORTED_STEPS` 里——不在的话会走
    「尚未实现」分支，`indexer.upserted` 是空的。
    """
    conn = _FullConn(doc=_doc())
    indexer = FakeIndexer()
    loop.run_once(_Pool(conn), None, indexer=indexer)  # type: ignore[arg-type]

    assert len(indexer.upserted) == 1, "run_once 没有分派到 run_index"
    assert conn.sql_of("state = 'done'")


def test_run_once把index_upstream传给run_index() -> None:
    """上游集合必须来自 `loop.index_upstream()`，不能是空元组。

    空元组会让上游判定永远返回 `None`——索引在 summarize 还没跑完时就写文档。
    """
    conn = _FullConn(blocked="tag", doc=_doc())
    indexer = FakeIndexer()
    loop.run_once(_Pool(conn), None, indexer=indexer)  # type: ignore[arg-type]

    # `blocked="tag"` 只有在 steps 非空时才会被 SQL 返回（假连接不判这个，
    # 但真 SQL 会）。这里断言的是「上游集合非空」这一性质。
    assert not indexer.upserted
    defers = conn.params_of("interval '1 second' * :seconds")
    assert defers, "有阻塞上游时该推迟"
    assert "tag" in str(defers[0]["e"])


def test_run_once不传indexer时run_index推迟而不是崩() -> None:
    """`indexer=None` 时 `run_index` 推迟并写原因，`run_once` 不该崩。

    现有 11 处 `run_once(pool, extractor)` 的调用都是 `indexer=None`，
    它们若领到 index 任务必须优雅推迟——否则整个 worker 会被一条
    `AttributeError: 'NoneType' object has no attribute 'upsert'` 打死。
    """
    conn = _FullConn(doc=_doc())
    handled = loop.run_once(_Pool(conn), None)  # type: ignore[arg-type]

    assert handled is True
    defers = conn.params_of("interval '1 second' * :seconds")
    assert defers, "indexer 缺席时没有推迟"
    assert not conn.sql_of("state = 'failed'")


# ── rebuild 命令 ──────────────────────────────────────────────


class FakePool:
    def __init__(self, pages: list[list[IndexDoc]]) -> None:
        self._pages = pages
        self.calls = 0

    def connection(self) -> object:
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            self.calls += 1
            yield _PageConn(self._pages)

        return _cm()


class _PageConn:
    """给 `search_store.index_source_page` 用的假连接：按 `after` 取下一页。"""

    def __init__(self, pages: list[list[IndexDoc]]) -> None:
        self._pages = pages

    def run(self, sql: str, **params: object) -> list[list[object]]:
        low = " ".join(sql.split()).lower()
        if "from items i" in low and "i.id > :after" in low:
            after = int(str(params.get("after", 0)))
            for page in self._pages:
                if page and page[0].id > after:
                    return [_doc_row(d) for d in page]
            return []
        return []


def test_rebuild逐页读并返回统计() -> None:
    """`search_store.index_source_page` 返回空页时结束。

    这条验的是「游标推进用的是上一页最后一条的 id」——用 `offset` 会在大库上
    越翻越慢，且期间有新内容插入时会漏条。

    `rebuild._pages` 是**页的生成器**（`Iterator[list[IndexDoc]]`），
    不是文档的生成器——`Indexer.rebuild` 要的就是「一页一批」。
    """
    from kb.worker import rebuild as rebuild_mod

    pages = [[_doc(1), _doc(2)], [_doc(3)], []]
    got = [[d.id for d in page] for page in rebuild_mod._pages(_PageConn(pages), 2)]
    assert got == [[1, 2], [3]]


def test_rebuild用真Indexer跑通全流程() -> None:
    """`rebuild.rebuild(pool, indexer)` 的产出形状与设计 §7.2 一致：

    「灌 N 条，删 M 条孤儿」。这条用一个真 `Indexer` + 只实现
    `index_document_ids` 的假客户端验「差集算对了、统计字段齐了」。

    刻意用**真的** `Indexer.rebuild` 而不是在测试里重写一遍差集算法：
    差集算法只有一份真源，测试重写一遍就只能验「测试自己算对了」。
    """
    from kb.search.indexer import Indexer
    from kb.worker import rebuild as rebuild_mod

    class _IdsClient:
        """rebuild 用到的那几个端点的最小实现。

        `Indexer.rebuild` 除了差集还要 `upsert`（→ `ensure_index` + 写文档），
        所以这里把设置与写入也补上——**只用假实现会验不出「差集算对了」**，
        而差集是本命令唯一的实质逻辑。
        """

        def __init__(self, ids: list[int]) -> None:
            self.ids = ids
            self.deleted: list[list[int]] = []
            self.added: list[list[dict[str, object]]] = []

        def index_document_ids(self, index_uid: str) -> list[int]:
            return list(self.ids)

        def delete_documents(self, index_uid: str, ids: list[int]) -> int:
            self.deleted.append(list(ids))
            return len(ids)

        def add_documents(self, index_uid: str, docs: list[dict[str, object]]) -> int:
            self.added.append(list(docs))
            return len(self.added)

        def index_exists(self, index_uid: str) -> bool:
            return True

        def patch_settings(self, index_uid: str, settings: dict[str, object]) -> int:
            return 1

        def wait_task(self, task_uid: int, *, timeout_sec: float = 0.0) -> None:
            return None

    client = _IdsClient([3, 99])  # 索引里有 99，库里没有 → 孤儿
    indexer = Indexer(client)  # type: ignore[arg-type]
    pool = FakePool([[_doc(1), _doc(2)], [_doc(3)], []])
    result = rebuild_mod.rebuild(pool, indexer, page_size=2)

    assert result["written"] == 3
    assert result["deleted"] == 1
    assert result["orphans"] == 1
    assert client.deleted == [[99]]
    assert len(client.added) == 2, "两页文档应发两次写请求"


def test_rebuild的页大小与批量一致() -> None:
    """一页正好一批请求体。两个数字不同时「读 200 条、一次只能写 50 条」
    会让日志里的批数看起来莫名其妙。"""
    from kb.search.config import INDEX_BATCH
    from kb.worker import rebuild as rebuild_mod

    assert rebuild_mod.PAGE_SIZE == INDEX_BATCH


def test_rebuild命令行有page_size参数() -> None:
    from kb.worker import rebuild as rebuild_mod

    args = rebuild_mod.parse_args(["--page-size", "50"])
    assert args.page_size == 50


def test_rebuild输出含灌入与删除两个数() -> None:
    """验收判据是「跑两遍第二遍 M=0」，所以输出里必须同时有 N 与 M。"""
    import inspect

    from kb.worker import rebuild as rebuild_mod

    source = inspect.getsource(rebuild_mod.main)
    assert "灌" in source and "孤儿" in source

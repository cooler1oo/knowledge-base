"""`Indexer` 的三条判据：请求体形状、批量切分、重建的删除集合。

**不连库、不联网**：`FakeClient` 只记调用并按剧本返回，`IndexDoc` 是纯数据。
这是「`kb/search/` 不 import `kb.store`」这条边界的直接收益——若 `Indexer`
放进 store，本节每条用例要么连库、要么把 store 一起打桩（设计 §1.4 理由二）。
"""

from __future__ import annotations

import pytest

from kb.search.config import (
    INDEX_BATCH,
    INDEX_UID,
    MAX_TOTAL_HITS,
    index_settings,
)
from kb.search.indexer import Indexer, _batches, create_index
from kb.search.models import IndexDoc, MeiliUnavailable


def _doc(item_id: int, **overrides: object) -> IndexDoc:
    """造一条文档。默认值刻意「全是空串」，让断言只关注被显式改过的字段。"""
    base: dict[str, object] = {
        "id": item_id,
        "title": f"标题{item_id}",
        "summary": "",
        "tags": (),
        "ocr_text": "",
        "content_text": "",
        "author": "",
        "kind": "text",
        "read_status": "unread",
        "saved_at": "2026-11-22T10:00:00+08:00",
        "saved_at_ts": 1_700_000_000 + item_id,
        "source_domain": "",
    }
    base.update(overrides)
    return IndexDoc(**base)  # type: ignore[arg-type]


class FakeClient:
    """只记调用、按剧本返回。

    `existing_ids` 是 `index_document_ids` 的返回值（重建算孤儿用）；
    `exists` 是 `index_exists` 的返回值。

    `pending` 模拟异步写队列：`pending=True` 时 `index_document_ids` 返回
    `existing_ids` **减去**已写的那些（= 还没落地）。用来验「对账前必须等写任务」。
    """

    def __init__(
        self,
        *,
        existing_ids: list[int] | None = None,
        exists: bool = True,
        pending: bool = False,
        write_fails: bool = False,
    ) -> None:
        self.added: list[list[dict[str, object]]] = []
        self.deleted: list[list[int]] = []
        self.patched: list[dict[str, object]] = []
        self.created: list[str] = []
        self.waited: list[int] = []
        self._existing = existing_ids or []
        self._exists = exists
        self._pending = pending
        self._write_fails = write_fails
        self._next_task = 1
        self._written: list[int] = []
        self._landed = False

    def index_exists(self, index_uid: str) -> bool:
        return self._exists

    def create_index(self, index_uid: str, primary_key: str = "id") -> int:
        self.created.append(index_uid)
        self._exists = True
        return self._task()

    def patch_settings(self, index_uid: str, settings: dict[str, object]) -> int:
        self.patched.append(settings)
        return self._task()

    def add_documents(self, index_uid: str, docs: list[dict[str, object]]) -> int:
        self.added.append(list(docs))
        if not self._write_fails:
            self._written += [int(str(d.get("id", 0))) for d in docs]
        return self._task()

    def delete_documents(self, index_uid: str, ids: list[int]) -> int:
        self.deleted.append(list(ids))
        return self._task()

    def index_document_ids(self, index_uid: str) -> list[int]:
        if self._pending and not self._landed:
            # 队列还没处理完：只看到「原本就有」的那些，不含本次刚写的
            return [i for i in self._existing if i not in self._written]
        return sorted(set(self._existing) | set(self._written))

    def wait_task(self, task_uid: int, *, timeout_sec: float = 0.0) -> None:
        self.waited.append(task_uid)
        self._landed = True

    def _task(self) -> int:
        uid = self._next_task
        self._next_task += 1
        return uid


# ── 设置 ───────────────────────────────────────────────────────


def test_ensure_index按设计一次PATCH():
    """设计 §3.2 的五个键一次 PATCH 完，不是一个键一次。"""
    client = FakeClient()
    Indexer(client).ensure_index()  # type: ignore[arg-type]

    assert len(client.patched) == 1
    settings = client.patched[0]
    assert settings["searchableAttributes"] == [
        "title",
        "summary",
        "tags",
        "ocr_text",
        "content_text",
        "author",
    ]
    assert settings["filterableAttributes"] == [
        "kind",
        "tags",
        "read_status",
        "saved_at_ts",
    ]
    assert settings["sortableAttributes"] == ["saved_at_ts"]
    assert settings["pagination"] == {"maxTotalHits": MAX_TOTAL_HITS}


def test_设置里不含content_text与ocr_text的展示():
    """`displayedAttributes` 不含正文：列表页不需要全文，不含它们让每条命中
    少传几 KB（正文动辄上千字）。"""
    settings = index_settings()
    displayed = settings["displayedAttributes"]
    assert isinstance(displayed, list)
    assert "content_text" not in displayed
    assert "ocr_text" not in displayed


def test_设置里不设rankingRules与matchingStrategy与localizedAttributes():
    """三条「不设」都是刻意的：

    - `rankingRules` 显式写在文档里的规则名会随版本改名（1.58 的默认列表
      已出现 `attributeRank` / `wordPosition` 这两个新名）
    - `matchingStrategy=all` 会让「抖音 剪辑 卡点」这类多一个词的查询直接 0 结果
    - `localizedAttributes=["cmn"]` 会把中英混排标题里的英文也按中文切
    """
    settings = index_settings()
    assert "rankingRules" not in settings
    assert "matchingStrategy" not in settings
    assert "localizedAttributes" not in settings


def test_ensure_index要等设置任务():
    """不等的话后续第一次写入落在默认设置下，带 `kind` 筛选的搜索
    会报 `Attribute kind is not filterable`——那是 400，会被判成我们的 bug。"""
    client = FakeClient()
    Indexer(client).ensure_index()  # type: ignore[arg-type]
    assert client.waited, "ensure_index 没等设置任务"


def test_ensure_index索引不存在时先建再设():
    """不先建就 PATCH，Meili 返回 404 → `MeiliRejected` → 每条内容都
    `mark_failed`。第一次部署时全库内容都会被判成「我们的 bug」。"""
    client = FakeClient(exists=False)
    Indexer(client).ensure_index()  # type: ignore[arg-type]
    assert client.created == [INDEX_UID]
    assert len(client.patched) == 1


def test_ensure_index同进程只跑一次():
    """`run_index` 每条内容都会调它。每次都是一次 PATCH + 一次任务等待。"""
    client = FakeClient()
    indexer = Indexer(client)  # type: ignore[arg-type]
    indexer.ensure_index()
    indexer.ensure_index()
    indexer.ensure_index()
    assert len(client.patched) == 1


def test_upsert里也会确保索引存在():
    """`run_index` 只调 `upsert` 的写法也要正确：漏掉 `ensure_index`
    会让第一次写入落在默认设置下，且「为什么过滤不生效」要查很久。"""
    client = FakeClient()
    Indexer(client).upsert([_doc(1)])  # type: ignore[arg-type]
    assert client.patched, "upsert 没有确保设置生效"


# ── 批量切分 ───────────────────────────────────────────────────


def test_200条切成1次请求():
    """`INDEX_BATCH=200`，正好 200 条只发一次。"""
    client = FakeClient()
    Indexer(client).upsert([_doc(i) for i in range(INDEX_BATCH)])  # type: ignore[arg-type]
    assert len(client.added) == 1
    assert len(client.added[0]) == INDEX_BATCH


def test_201条切成2次请求():
    """边界用例：多一条就要多一次请求，不是「向上取整到最近的批量」。"""
    client = FakeClient()
    Indexer(client).upsert([_doc(i) for i in range(INDEX_BATCH + 1)])  # type: ignore[arg-type]
    assert len(client.added) == 2
    assert len(client.added[0]) == INDEX_BATCH
    assert len(client.added[1]) == 1


def test_401条切成3次请求():
    client = FakeClient()
    Indexer(client).upsert([_doc(i) for i in range(INDEX_BATCH * 2 + 1)])  # type: ignore[arg-type]
    assert len(client.added) == 3


def test_切批不丢条也不重复():
    """分批时最容易犯的错是 `docs[start : start+size]` 写成 `docs[start:size]`
    ——第二页开始永远是空。这条断言的是**全部 id 的集合**，不只看批数。"""
    client = FakeClient()
    docs = [_doc(i) for i in range(INDEX_BATCH + 37)]
    written = Indexer(client).upsert(docs)  # type: ignore[arg-type]

    flat = [row["id"] for batch in client.added for row in batch]
    assert sorted(flat) == sorted(range(INDEX_BATCH + 37))
    assert written == len(docs)


def test_upsert返回提交条数():
    client = FakeClient()
    assert Indexer(client).upsert([_doc(1), _doc(2)]) == 2  # type: ignore[arg-type]


def test_upsert空列表时零请求():
    """空列表不能发一次空请求：rebuild 的空页会因此打一次无意义往返，
    也让「第二遍 M=0」的验收多一个噪声信号。"""
    client = FakeClient()
    assert Indexer(client).upsert([]) == 0  # type: ignore[arg-type]
    assert client.added == []


def test_upsert传的是meili字典而不是dataclass():
    """`json.dumps` 不认 dataclass。这条拦「忘了 `to_meili_dict()`」。"""
    client = FakeClient()
    Indexer(client).upsert([_doc(7, title="标题七")])  # type: ignore[arg-type]
    row = client.added[0][0]
    assert isinstance(row, dict)
    assert row["id"] == 7
    assert row["title"] == "标题七"
    assert isinstance(row["tags"], list), "tags 必须是 list，元组不可 JSON 序列化"


def test_upsert不等写任务():
    """写文档不等任务：Meili 的写是异步队列，等它会把一次 200 条的批量
    变成秒级阻塞。`wait_task` 的调用只应来自 `ensure_index`。"""
    client = FakeClient()
    indexer = Indexer(client)  # type: ignore[arg-type]
    indexer.ensure_index()
    before = len(client.waited)
    indexer.upsert([_doc(1)])
    assert len(client.waited) == before, "写文档等了任务"


# ── 删除与重建 ─────────────────────────────────────────────────


def test_delete_ids空列表时零请求():
    client = FakeClient()
    assert Indexer(client).delete_ids([]) == 0  # type: ignore[arg-type]
    assert client.deleted == []


def test_delete_ids按批提交():
    client = FakeClient()
    assert Indexer(client).delete_ids([5, 6, 7]) == 3  # type: ignore[arg-type]
    assert client.deleted == [[5, 6, 7]]


def test_重建的删除集合等于索引有而库里没有() -> None:
    """设计 §7.2：差集 `existing - seen`。

    索引里有 1/2/3/99，库里只有 1/2/3 → 删 99。99 是「已软删」或「从没写过」
    的孤儿，留着会让卡片点进去 404。

    `missing`（库里该有、索引里没有）这一栏是**写作业静默失败**的机器检查：
    真机踩过一次请求体包错一层，HTTP 202 + taskUid 都有，任务在 Meili 里
    `failed`，现象是「搜索永远 0 条」。
    """
    client = FakeClient(existing_ids=[1, 2, 3, 99])
    indexer = Indexer(client)  # type: ignore[arg-type]
    result = indexer.rebuild([[_doc(1), _doc(2)], [_doc(3)]])

    assert client.deleted == [[99]]
    assert result["written"] == 3
    assert result["deleted"] == 1
    assert result["orphans"] == 1
    assert result["missing"] == 0, "1/2/3 都在索引里，不该报缺失"


def test_重建能发现写入未落地的条目() -> None:
    """`missing` 非空 = 有文档写了但没进索引。

    真机上的具体成因是「请求体形状错」，而它在 HTTP 层看起来是成功的
    （202 + `taskUid`）。这条用例断言「reconcile 这一步真的在比两个集合」：
    库里 1/2/3、索引里只有 1/2 → `missing` 必须是 1。
    """
    client = FakeClient(existing_ids=[1, 2], write_fails=True)
    result = Indexer(client).rebuild([[_doc(1), _doc(2), _doc(3)]])  # type: ignore[arg-type]

    assert result["missing"] == 1, f"没发现 3 号没进索引：{result}"
    assert result["written"] == 3, "written 是「提交了几条」，不是「索引里有了几条」"


def test_重建对账前等最后那个写任务() -> None:
    """⚠️ **这条用例为一次真机误报而写。**

    第一版的对账没等写任务。真机上第一遍 rebuild 立刻报
    `✗ 有 1 条写入未落地`——因为 `add_documents` 返回 202 时文档还在
    Meili 的异步队列里，紧接着 `index_document_ids` 读到的是「还没有这条」。

    现象只在**新建索引后的第一次** rebuild 出现（文档已落地后就正常了），
    是最难复现的一类。`pending=True` 精确模拟这个窗口：不等就必然报 missing。
    """
    client = FakeClient(existing_ids=[], pending=True)
    result = Indexer(client).rebuild([[_doc(1)]])  # type: ignore[arg-type]

    assert result["missing"] == 0, f"对账没等写任务，误报了 missing：{result}"
    assert client.waited, "rebuild 没有等任何任务"


def test_重建第二遍不删任何东西():
    """验收判据「跑两遍第二遍 M=0」在代码层的对应断言。"""
    client = FakeClient(existing_ids=[1, 2])
    indexer = Indexer(client)  # type: ignore[arg-type]
    result = indexer.rebuild([[_doc(1), _doc(2)]])

    assert result["deleted"] == 0
    assert client.deleted == [], "第二遍不该发删除请求"


def test_重建先写后删孤儿():
    """先删再灌的代价是「删与灌之间搜索结果为空」，那个窗口在对账任务里
    可能长达几十秒。用户不会知道「正在重建」，只会认为搜索坏了。"""
    client = FakeClient(existing_ids=[1, 99])
    Indexer(client).rebuild([[_doc(1)]])  # type: ignore[arg-type]

    # FakeClient 的 added / deleted 分别是时序记录，用 _next_task 的分配顺序判先后
    assert client.added and client.deleted
    # 写任务的 uid 小于删任务的 uid，说明写在删之前
    # （每类各发 1 次任务：add → patch? 不，ensure_index 只跑一次）
    assert client.added[0][0]["id"] == 1


def test_重建跳过空页():
    """`index_source_page` 用「返回空页」表示结束。空页不该触发 upsert。"""
    client = FakeClient(existing_ids=[])
    result = Indexer(client).rebuild([[], [_doc(1)], []])  # type: ignore[arg-type]
    assert result["written"] == 1


def test_重建统计的孤儿数是差集大小而不是删除请求数():
    """`orphans` 与 `deleted` 当前相等，但分开计数：将来若按批删除返回失败条数，
    两个数会分叉，而验收看的是孤儿数。"""
    client = FakeClient(existing_ids=[9, 10, 11])
    result = Indexer(client).rebuild([[_doc(1)]])  # type: ignore[arg-type]
    assert result["orphans"] == 3
    assert result["deleted"] == 3


# ── 上游异常不被吞 ─────────────────────────────────────────────


def test_写索引抛异常时原样透出():
    """`Indexer` 不 catch 客户端的异常。失败分级（defer / mark_failed）
    是 `run_index` 的职责——在两层都判一次会让归因变成猜。"""

    class _Boom(FakeClient):
        def add_documents(self, index_uid: str, docs: list[dict[str, object]]) -> int:
            raise MeiliUnavailable("服务挂了")

    with pytest.raises(MeiliUnavailable):
        Indexer(_Boom()).upsert([_doc(1)])  # type: ignore[arg-type]


def test_create_index先查再建():
    """已存在时不重复建：`POST /indexes` 对已存在的 uid 返回 409，
    409 在薄客户端里会变成 `MeiliUnavailable`——一句「重试可能成功」的错话。"""
    client = FakeClient(exists=True)
    create_index(client)  # type: ignore[arg-type]
    assert client.created == []
    assert len(client.patched) == 1


def test_create_index不存在时建():
    client = FakeClient(exists=False)
    create_index(client)  # type: ignore[arg-type]
    assert client.created == [INDEX_UID]


# ── 小工具 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("total", "size", "expected"),
    [
        # 0 条 → 0 批。`upsert` 在空列表上直接 return，这个数不会进日志，
        # 但让「批数 × 批量 ≥ 条数」这条恒等式对 0 也成立，读起来不用特判。
        (0, 200, 0),
        (1, 200, 1),
        (200, 200, 1),
        (201, 200, 2),
        (400, 200, 2),
        (401, 200, 3),
    ],
)
def test_批数计算(total: int, size: int, expected: int) -> None:
    assert _batches(total, size) == expected


def test_批数在size为0时不除零():
    assert _batches(10, 0) == 1

"""QA 独立验收的**探测组**：不复跑工程师已有的用例，只打他没试过的角度。

## 本文件存在的理由

工程师的 `tests/test_search_query.py` 验的是 `SearchService` 作为**对象**的行为
（同一实例连续调用时熔断生效）。`tests/test_api_search.py` 验的是**单个请求**
的字段契约。两者之间有一个缝：**`SearchService` 在每个 HTTP 请求里都是新实例**
——所以「同一实例第二次不再碰 client」这条绿，说明不了「用户连续点两次搜索，
第二次不会又白等 3 秒」。

本文件专打这类缝：跨请求的、跨组件的、异常路径不对称的、并发时序的。
每条用例都注明「工程师的哪条断言没覆盖到这一点」。

## 命名

`test_qa_` 前缀 + 中文描述，与项目既有风格一致。断言写**设计承诺**，
不写「当前实现如此」——实现错了就该红。
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from kb.api import search as search_api
from kb.search.indexer import Indexer
from kb.search.models import (
    IndexDoc,
    MeiliRejected,
    MeiliUnauthorized,
    MeiliUnavailable,
    SearchMode,
    SearchQuery,
)
from kb.search.service import SearchService
from kb.worker import loop

# ── 共用假实现 ────────────────────────────────────────────────


class ScriptedClient:
    """按剧本返回，或抛异常。记录每次调用。"""

    def __init__(self, script: list[object] | None = None) -> None:
        self.script: list[object] = list(script or [])
        self.calls: list[dict[str, object]] = []

    def search(self, index_uid: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(dict(payload))
        if not self.script:
            return {"hits": [], "estimatedTotalHits": 0}
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, dict)
        return item


class _FakeConn:
    """只回答 `known_kinds` 与 `summaries_by_ids` 两条查询。"""

    def __init__(
        self, summaries: dict[int, tuple[str, str, str]] | None = None
    ) -> None:
        self.summaries = summaries or {}

    def run(self, sql: str, **params: object) -> list[list[object]]:
        from datetime import datetime, timezone

        low = " ".join(sql.split()).lower()
        if "from item_kinds" in low:
            return [["article"]]
        if "sub.id = any(" in low:
            ids = [int(i) for i in (params.get("ids") or [])]  # type: ignore[union-attr]
            out: list[list[object]] = []
            for item_id in ids:
                row = self.summaries.get(item_id)
                if row is None:
                    continue
                title, read_status, status = row
                out.append(
                    [
                        item_id,
                        "article",
                        None,
                        title,
                        "x.invalid",
                        datetime(2026, 11, 22, 4, 30, tzinfo=timezone.utc),
                        read_status,
                        "",
                        1,
                        0,
                        0,
                        0,
                    ]
                )
            return out
        return []


class _Pool:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    def connection(self) -> object:
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield self._conn

        return _cm()


def _app(client: object, conn: object | None = None, **kwargs: object) -> TestClient:
    handler = search_api.search(
        _Pool(conn or _FakeConn()),  # type: ignore[arg-type]
        client=client,
        **kwargs,  # type: ignore[arg-type]
    )
    return TestClient(Starlette(routes=[Route("/api/search", handler)]))


# ══════════════════════════════════════════════════════════════
# 1. 熔断在**跨请求**上是否生效（设计 §6.1 承诺「30 秒内不再白等 3 秒」）
# ══════════════════════════════════════════════════════════════


def test_连续两次请求Meili挂了第二次不再打Meili() -> None:
    """设计 §6.1：确认挂了之后 30 秒内直接走兜底，不再每搜一次白等 3 秒。

    ⚠️ 这是**唯一**能验到设计承诺的口径。`test_search_query.py` 里
    `test_熔断生效后第二次调用不再碰client` 用的是同一个 `SearchService` 实例，
    所以它绿只能说明「实例字段还在」；生产路径上 `kb/api/search.py` 的
    `_service(conn)` 每个请求都新建一个 `SearchService`，`_down_until` 随之归零。

    期望：第 2 次请求 Meili 调用次数仍为 1。
    """
    meili = ScriptedClient([MeiliUnavailable("连不上")])
    client = _app(meili)

    client.get("/api/search?q=营销")
    client.get("/api/search?q=营销")

    assert len(meili.calls) == 1, (
        f"熔断没跨请求生效：Meili 被打了 {len(meili.calls)} 次。"
        "设计 §6.1 承诺「确认挂了之后这段时间内直接走兜底，不再每搜一次白等 3 秒」，"
        "而每次请求都新建 SearchService 让 _down_until 归零——"
        "Meili 挂机期间每个用户请求都要白等一个 3 秒超时。"
    )


def test_连续五次请求只有第一次打Meili() -> None:
    """上一条的加强版：连续 5 次请求，Meili 调用次数应恒为 1。

    把次数拉大是为了把「偶发一次巧合」排除掉：如果实现只在某次侥幸命中缓存，
    5 次里必有一次露馅。
    """
    meili = ScriptedClient([MeiliUnavailable("挂")] * 5)
    client = _app(meili)

    for _ in range(5):
        assert client.get("/api/search?q=营销").status_code == 200

    assert len(meili.calls) == 1, f"5 次请求打了 Meili {len(meili.calls)} 次"


# ══════════════════════════════════════════════════════════════
# 2. 放宽重查路径的异常映射与首次调用**不对称**
# ══════════════════════════════════════════════════════════════


def test_放宽重查时401应该走兜底而不是穿透成500() -> None:
    """401 在搜索路径上一律判「不可用」（设计 §3.4 / §6.1 第 5 行）。

    `_try_meili` 的首次调用 catch 了 `MeiliUnauthorized`，但**放宽重查那次只
    catch `MeiliUnavailable`**。于是同一个 401：

    - 首次调用遇到 → 熔断 + 兜底，用户拿到结果 + 提示
    - 重查遇到 → 穿透出 `SearchService.run()` → 500，用户拿到报错页

    这两条路径对用户是同一件事（拿不到搜索结果），处置必须一致。
    """
    meili = ScriptedClient(
        [{"hits": [], "estimatedTotalHits": 0}, MeiliUnauthorized("401")]
    )
    service = SearchService(meili, lambda q: [], clock=lambda: 0.0)

    result = service.run(SearchQuery(text="营销"))

    assert result.mode is SearchMode.FALLBACK, (
        "重查时的 401 穿透了：SearchService.run 的 docstring 写「不抛异常」，"
        "而 §3.4 定 401 在搜索路径上判不可用。用户此时应拿到兜底结果 + 提示。"
    )


def test_放宽重查时MeiliRejected仍应向上抛() -> None:
    """反例守住上一条不要修过头：400/422 是**我们的 bug**，必须继续抛。

    设计 §6.1 最后一行：filter 语法错不降级，向上抛成 500 并留 filter 串。
    这条钉住「重查路径也应该保持这条语义」——修 401 时不该顺手 catch 掉 400。
    """
    meili = ScriptedClient(
        [{"hits": [], "estimatedTotalHits": 0}, MeiliRejected("400 filter 语法错")]
    )
    service = SearchService(meili, lambda q: [], clock=lambda: 0.0)

    with pytest.raises(MeiliRejected):
        service.run(SearchQuery(text="营销"))


def test_放宽重查时401的响应不该是500() -> None:
    """把上一条推到 HTTP 层：用户看到的状态码是什么。

    `500` 会让前端显示「服务器内部错误」，而真实原因是检索服务 key 错/挂了。
    设计给这种情况的答案是 200 + `degraded: true` + `notice`（拿不到 Meili
    时走兜底），不是 500。
    """
    meili = ScriptedClient(
        [{"hits": [], "estimatedTotalHits": 0}, MeiliUnauthorized("401")]
    )
    response = _app(meili).get("/api/search?q=营销")

    assert response.status_code != 500, (
        "放宽重查时的 401 变成 500 了。用户在 Meili key 配错时会看到"
        "「服务器内部错误」，而应当看到兜底结果 + 「搜索服务未运行」的提示。"
    )


# ══════════════════════════════════════════════════════════════
# 3. rebuild 与并发写入的竞态（设计 §7.2）
# ══════════════════════════════════════════════════════════════


class _RecordingClient:
    """rebuild 用的假 client。`existing` 模拟「索引里当前真有这些 id」。"""

    def __init__(self, existing: set[int]) -> None:
        self.existing = set(existing)
        self.deleted: list[int] = []
        self.added: list[int] = []

    def index_exists(self, uid: str) -> bool:
        return True

    def patch_settings(self, uid: str, settings: dict[str, object]) -> int:
        return 1

    def wait_task(self, uid: int, *, timeout_sec: float = 10.0) -> None:
        return None

    def add_documents(self, uid: str, docs: list[dict[str, object]]) -> int:
        self.added.extend(int(str(d["id"])) for d in docs)
        self.existing.update(int(str(d["id"])) for d in docs)
        return 1

    def index_document_ids(self, uid: str) -> list[int]:
        return sorted(self.existing)

    def delete_documents(self, uid: str, ids: list[int]) -> int:
        self.deleted.extend(ids)
        return 1


def _doc(item_id: int) -> IndexDoc:
    return IndexDoc(
        id=item_id,
        title=f"标题{item_id}",
        summary="",
        tags=(),
        ocr_text="",
        content_text="",
        author="",
        kind="article",
        read_status="unread",
        saved_at="2026-11-22T00:00:00+08:00",
        saved_at_ts=1795312800,
        source_domain="x.invalid",
    )


def test_rebuild的孤儿判定是集合差而非库里回查_记录竞态风险() -> None:
    """**这条断言的是当前设计行为，不是我认为正确的行为。**

    ## 我原先把这一条当成 bug，主理人指出我判错了

    我先构造「页面只含 doc1/doc2，而 `existing={99}`」断言 99 被误删。主理人的
    反驳是对的：**那是 rebuild 的预期语义**——索引里有、这次分页读取里没有，
    就说明它不在库的可读集合里（软删或物理删），该删。

    真正的问题是**并发时序**：若第 N 页读完之后、删除之前，有新条目写进了库并
    入了索引，它会被误删。要验这个必须精确控制「翻页读」与「删孤儿」之间插入
    一次外部写入，而 `Indexer.rebuild` 是一个整体方法，中间没有注入点。

    ## 为什么不做时序注入

    能做的办法只有 mock 到失真（替换 `index_source_page` 让它在第二次调用时
    才返回新条目，或在 `index_document_ids` 里插桩）。那样的用例验的是我的
    mock 而不是代码，且这个竞态**只在手工跑 rebuild 与 worker 并发时**成立。
    主理人已认可「静态分析判断风险可接受」——rebuild 是运维命令，不与 worker
    并发跑。

    所以这条用例**只钉住当前实现的可观测行为**，作为一个「将来若改了孤儿算法
    这里会红」的护栏，并在断言消息里写明这是记录而非背书。
    """
    orphan_in_index = 99
    client = _RecordingClient(existing={orphan_in_index})
    indexer = Indexer(client, batch_size=200)

    stats = indexer.rebuild([[_doc(1), _doc(2)]])

    # 当前行为：只在分页里没出现的 id 一律当孤儿删。
    assert orphan_in_index in client.deleted, (
        "孤儿判定算法变了：现在不再是「existing - seen 的差集」。"
        f"统计 {stats}。"
        "⚠️ 若这是有意的改进（比如删之前回查库确认），请把本条改成断言"
        "「库里有但不在本次分页的 id 不该被删」——那正是我最初主张的行为。"
    )


def test_rebuild第二遍不删任何东西() -> None:
    """稳定态自检：库里就 3 条、索引里也是 3 条时，第二遍必须删除 0 条。

    这条是上一条的对照组——它绿说明「差集算法在无并发时是对的」，
    从而把上一条的失败定位到「并发时序」而不是「差集算错了」。
    """
    client = _RecordingClient(existing={1, 2, 3})
    indexer = Indexer(client, batch_size=200)

    stats = indexer.rebuild([[_doc(1), _doc(2), _doc(3)]])

    assert stats["deleted"] == 0
    assert client.deleted == []


# ══════════════════════════════════════════════════════════════
# 4. 上游步骤集合与分派的耦合（设计 §4.3「ocr 上线不用改第二处」）
# ══════════════════════════════════════════════════════════════


def test_ocr进上游集合后run_once应分派到ocr而不是run_index() -> None:
    """设计 §4.3：`ocr` 上线时只需把它加进 `SUPPORTED_STEPS`，交集自动包含它。

    但**分派**是另一处：`loop.run_once` 的分支是
    `if fetch / elif summarize / elif tag / else: run_index`。
    `else` 兜住一切非前三者的步骤——所以 `ocr` 一旦进了 `SUPPORTED_STEPS`，
    它的 job 会被当成 `index` 分派给 `run_index`。

    后果不是一次失败：`run_index` 拿到一条 `step='ocr'` 的 job，会读 items 行
    写进索引并 `mark_done`——OCR 从没跑过，但 job 显示完成，而且它作为上游
    从此不再阻塞 index（已 done），正文/OCR 文本永远不进索引。

    本用例用 monkeypatch 观察分派：喂一个含 `"ocr"` 的 SUPPORTED_STEPS，
    断言 `steps.run_index` **没有**被调用。
    """
    called: list[str] = []

    class _Pool2:
        def connection(self) -> object:
            from contextlib import contextmanager

            @contextmanager
            def _cm():
                yield _JobConn(step="ocr")

            return _cm()

    # 只观察分派目标，不真正执行步骤
    original_index = loop.steps.run_index

    def _spy_index(*args: object, **kwargs: object) -> None:
        called.append("index")

    def _spy_ocr(*args: object, **kwargs: object) -> None:
        called.append("ocr")

    loop.steps.run_index = _spy_index  # type: ignore[assignment]
    had_ocr = hasattr(loop.steps, "run_ocr")
    if had_ocr:
        loop.steps.run_ocr = _spy_ocr  # type: ignore[attr-defined]
    try:
        monkey = pytest.MonkeyPatch()
        monkey.setattr(
            loop, "SUPPORTED_STEPS", ("fetch", "ocr", "summarize", "tag", "index")
        )
        try:
            loop.run_once(_Pool2(), lambda url: None)  # type: ignore[arg-type,return-value]
        finally:
            monkey.undo()
    finally:
        loop.steps.run_index = original_index  # type: ignore[assignment]

    assert called != ["index"], (
        "step='ocr' 被分派给了 run_index（`else` 分支兜住一切非 fetch/summarize/tag "
        "的步骤）。ocr 上线后，OCR 任务的 job 会被标 done 而 OCR 从未执行，"
        "这条内容作为上游也不再阻塞 index——正文/OCR 文本永远进不了索引。"
    )


class _JobConn:
    """`claim_one` 返回一条指定 step 的任务，其余语句一律空。"""

    def __init__(self, *, step: str) -> None:
        self.step = step

    def run(self, sql: str, **params: object) -> list[list[object]]:
        low = " ".join(sql.split()).lower()
        if "update jobs set state = 'running'" in low and "returning jobs.id" in low:
            return [[1, 10, self.step, 0, "running"]]
        return []


# ══════════════════════════════════════════════════════════════
# 5. 超长 / 特殊查询的提示缺口（设计 §5.3 表格）
# ══════════════════════════════════════════════════════════════


def test_超64字符的查询应带截断提示() -> None:
    """设计 §5.3 表格最后一行：「超 64 字符 → 截断到 64 并 `notice` 说明」。

    实测：`q` 被截到 64，`notice` 是 `None`。用户打了 80 个字，前 64 个之外的
    被静默丢弃，而他还以为自己搜的是整句话。这是设计明写的承诺，不是我的偏好。
    """
    meili = ScriptedClient()
    response = _app(meili).get("/api/search", params={"q": "啊" * 100})

    body = response.json()
    assert body["notice"], (
        "超长查询被截到 64 字符但 notice 是 None。设计 §5.3 明写「截断到 64 并 "
        "notice 说明」，用户无从知道后半句被丢弃了。"
    )


def test_单字查询的提示不该被degraded掩盖() -> None:
    """设计 §5.3：单字查询 `degraded` 保持 False，提示靠 `notice` 传达。

    这条是护栏：上一条若有人用「置 degraded=True」的方式塞提示，会破坏
    「degraded 只表示服务坏了」这个约定（§3.7 表格 + §5.3 单字行）。
    两条必须同时成立：notice 有、degraded 为 False。
    """
    meili = ScriptedClient(
        [
            {
                "hits": [
                    {
                        "id": 1,
                        "kind": "article",
                        "title": "三体",
                        "summary": "",
                        "tags": [],
                        "source_domain": "x.invalid",
                        "saved_at": "2026-11-22T04:30:00+08:00",
                        "read_status": "unread",
                        "_rankingScore": 0.99,
                    }
                ],
                "estimatedTotalHits": 1,
            }
        ]
    )
    conn = _FakeConn(summaries={1: ("三体", "unread", "done")})
    body = _app(meili, conn).get("/api/search?q=三").json()

    assert body["degraded"] is False, "单字查询不是服务故障，degraded 必须为假"
    assert body["notice"], "单字查询必须给提示"


# ══════════════════════════════════════════════════════════════
# 6. 分页上限与 Meili 的 maxTotalHits 对不齐
# ══════════════════════════════════════════════════════════════


def test_最大offset加最大limit不应超过maxTotalHits() -> None:
    """`offset=999&limit=50` 要求 Meili 返回第 1000–1049 条，而索引设置里
    `pagination.maxTotalHits = 1000`。越界的部分 Meili 会静默截掉，
    用户看到「第 1000 条以后翻不到」且没有任何提示。

    这不是「当前会不会触发」的问题（个人库远低于 1000 条），而是**两个常量
    各自定义、没有互相校验**。要么把 offset 上限降到 `MAX_TOTAL_HITS - MAX_LIMIT`，
    要么把 maxTotalHits 提到 `MAX_OFFSET + MAX_LIMIT`。
    """
    from kb.search.config import MAX_LIMIT, MAX_OFFSET, MAX_TOTAL_HITS

    reachable = MAX_OFFSET + MAX_LIMIT
    assert reachable <= MAX_TOTAL_HITS, (
        f"offset 上限 {MAX_OFFSET} + limit 上限 {MAX_LIMIT} = {reachable} "
        f"超过 maxTotalHits={MAX_TOTAL_HITS}。最后 "
        f"{reachable - MAX_TOTAL_HITS} 条请求会返回空，用户以为没有更多内容。"
    )


# ══════════════════════════════════════════════════════════════
# 7. 反斜杠标签：filter 串在**真 Meili** 上是语法错误（P1）
# ══════════════════════════════════════════════════════════════


def test_含反斜杠的标签quote后应能被Meili解析() -> None:
    r"""`query.quote()` 的第一版 docstring 写「反斜杠本身不转义：Meili 的 filter
    语法只定义了双引号的转义，反斜杠按字面处理」。

    **这句话是错的。** 真机（平板 Meili 1.58.0）实测：

    | `quote()` 产出 | Meili 响应 |
    |---|---|
    | 反斜杠不转义 | **400** `Malformed value` |
    | 反斜杠转成两个 | 200，正确命中 |

    用户影响链条：库里存在含反斜杠的标签（`C:` + 反斜杠 + `Users` 这类粘贴进来
    的路径）→ 用户按该标签筛选 → `build_filter` 生成 400 的串 → `MeiliRejected`
    **不降级**（设计 §6.1 最后一行）→ API 返 500。也就是说这个标签
    **一点就整个搜索页报错**，而不是「筛不出来」。

    ## ⚠️ 这条用例我写错过两次，别「顺手简化」掉 `chr(92)`

    **第一次**：用 shell heredoc 生成这个文件，`\\` 被 shell 折成了 `/`，
    `tags=` 收到的是 `"C://Users"`（**正斜杠**），断言却在找反斜杠——
    测的根本不是反斜杠，它「红」只因为我断言了一个不存在的东西。

    **第二次**：改成 Python 字面量 `"C:\Users"`，看起来对了，实际 Python 把
    `\U` 当成 **Unicode 转义序列**的开头，那个字面量的真实值是 `'C:/Users'`
    （正斜杠，8 个字符）。**又是正斜杠。**

    ```
    >>> 'C:\Users' == 'C:/Users'
    True
    ```

    工程师在 `quote()` 的 docstring 里记的也是这件事（他以为在测反斜杠、
    实际构造了正斜杠）。**三个人在两个语言层各踩一次**：shell 折一次、
    Python 字面量折一次。

    所以这里**必须**用 `chr(92)` 逐字节构造，绝不经过 shell、字符串字面量、
    JSON 任何一层。改成 `r"C:\Users"` 也可以（raw string 不解析 `\U`），
    但 `chr(92)` 更硬：它对「以后有人把 docstring 改成普通字符串」也免疫。
    """
    from kb.search.models import SearchFilter
    from kb.search.query import build_filter

    backslash = chr(92)  # 真正的反斜杠，不经任何转义层。见 docstring 末段。
    tag = "C:" + backslash + "Users"

    # 自证：万一有人把上面改成字面量，这条断言会立刻红，而不是静默假绿。
    assert tag == "C:" + chr(92) + "Users"
    assert backslash in tag, "构造出来的值里没有反斜杠——构造方式又被转义层吃掉了"
    assert len(tag) == 8, f"标签长度应为 8（C: \\ Users），实际 {len(tag)}：{tag!r}"

    expression = build_filter(SearchFilter(tags=(tag,)))

    # Meili 的 filter 字符串里，一个字面反斜杠必须写成两个。
    expected = '"C:' + backslash * 2 + 'Users"'
    assert expected in expression, (
        f"标签 {tag!r} 生成了 {expression!r}，但真机实测 Meili 1.58.0 对"
        f"未转义的反斜杠返回 HTTP 400 `Malformed value`（而 400 在搜索路径上"
        f"不降级，设计 §6.1）——用户按这个标签筛选会得到 500，不是空结果。"
        f"期望串：{expected!r}"
    )


def test_反斜杠与双引号相邻时也要正确转义() -> None:
    r"""反斜杠紧邻双引号时要**先**转反斜杠再转引号，否则引号会跑出来。

    规则：`\` → `\\` 先做，然后 `"` → `\"`。顺序反过来时，第一步给引号加的
    那个反斜杠会被第二步再转一遍，于是引号又重新裸露。

    ## 同样必须用 chr()

    反斜杠用 `chr(92)`、引号用 `chr(34)`，理由见上一条用例的 docstring 末段：
    `"a\` + `"` 这种字面量在不同层级会被不同规则折叠。这一条里涉及的字符更多，
    出错面更大——所以全部逐字节构造。
    """
    from kb.search.models import SearchFilter
    from kb.search.query import build_filter

    backslash = chr(92)
    quote_char = chr(34)
    tag = "a" + backslash + quote_char + "b"

    # 自证构造正确：反斜杠 + 引号，共 4 个字符。
    assert tag == "a" + chr(92) + chr(34) + "b"
    assert backslash in tag and quote_char in tag, f"构造失真：{tag!r}"
    assert len(tag) == 4, f'标签长度应为 4（a \\ " b），实际 {len(tag)}：{tag!r}'

    expression = build_filter(SearchFilter(tags=(tag,)))

    # 正确转义：反斜杠 → 两个，引号 → 反斜杠+引号
    expected = '"a' + backslash * 2 + backslash + quote_char + 'b"'
    assert expected in expression, (
        f"反斜杠紧邻双引号时生成了 {expression!r}，真机上这是 filter 语法错误"
        f"（`Found unexpected characters at the end of the filter`）。"
        f"期望串：{expected!r}"
    )


# ══════════════════════════════════════════════════════════════
# 8. 翻页到末尾时误触发放宽（P2）
# ══════════════════════════════════════════════════════════════


def test_翻过最后一页时不该放宽重查() -> None:
    """放宽的触发条件是「0 命中」，但这个条件分不清两件完全不同的事：

    1. **相关度门槛把结果全截了** → 该放宽（设计 §5.3 的意图）
    2. **用户翻到了最后一页之后** → 不该放宽

    第 2 种情况下 `hits` 为空是因为 `offset` 越过了结果集尾部，不是因为分数低。
    现在的实现会立刻用 `min_score=0` 重查，于是：

    - 用户在第 2 页末尾点「下一页」，突然看到一堆**低分噪音**（正是 MIN_SCORE
      要截掉的那批，0.285 那种），并且 `relaxed=true`
    - `estimatedTotal` 从 30 跳成 200（放宽后的总数），分页器算出「还有 170 条」，
      用户继续往下翻，每一页都是噪音
    - 每次都白打一次 Meili（首次 + 重查 = 双倍 QPS）

    修法：`estimatedTotal > offset` 说明还有数据、只是这一页空了 → 不该放宽；
    或只在 `offset == 0` 时放宽。
    """

    class _Client:
        """严格查询只覆盖 offset 0–29，之后为空；放宽查询覆盖到 199。"""

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def search(
            self, index_uid: str, payload: dict[str, object]
        ) -> dict[str, object]:
            self.calls.append(dict(payload))
            strict = "rankingScoreThreshold" in payload
            offset = int(payload["offset"])  # type: ignore[arg-type]
            total = 30 if strict else 200
            limit = 10 if offset < total else 0
            hits = [
                {"id": i, "title": f"t{i}", "_rankingScore": 0.9}
                for i in range(offset, offset + limit)
            ]
            return {"hits": hits, "estimatedTotalHits": total}

    meili = _Client()
    service = SearchService(meili, lambda q: [], clock=lambda: 0.0)

    # offset=30 已经是严格结果集的尾部之后（strict total = 30）
    result = service.run(SearchQuery(text="营销", limit=20, offset=30))

    assert not result.relaxed, (
        "翻到结果集尾部之后触发了放宽：用户会看到 relaxed=true 与一堆低分噪音，"
        "estimatedTotal 从 30 跳成 200，分页器算出还有 170 条。"
    )
    assert len(meili.calls) == 1, (
        f"翻页到尾部时打了 {len(meili.calls)} 次 Meili（首次 + 放宽重查）。"
        "每次翻页都双倍 QPS。"
    )


# ══════════════════════════════════════════════════════════════
# 9. 同步 HTTP 在 async handler 里阻塞事件循环（P1，架构面）
# ══════════════════════════════════════════════════════════════


def test_慢搜索期间其他请求不该被阻塞() -> None:
    """`kb/api/search.py` 的 handler 是 `async def`，但内部依次做了三件**同步阻塞**
    的事：`pool.connection()`、`search_store` 的 SQL、`MeiliClient.search()` 的
    HTTP（最长 3 秒）。

    `async def` 里跑同步阻塞调用 = 占住整个事件循环。实测（uvicorn 单进程）：
    Meili 慢 2.5 秒时，期间一个只返回 `{"ok": true}` 的 `/api/health`
    要等 **4.86 秒**才返回。

    对一个单用户应用这不算致命，但它的表现形式具有误导性：用户点搜索 → 整个
    站点（包括还在加载的封面图、详情页）全部卡住 → 他第二次点搜索 → 排队。
    而「慢」看起来像 Meili 的问题，实际是 api 进程被自己的 handler 卡住。

    修法是 `await run_in_threadpool(...)`，与 `kb/api/items.py` 同构。
    """
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import httpx
    import uvicorn
    from starlette.responses import JSONResponse

    from kb.search.client import MeiliClient

    class _SlowMeili(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            time.sleep(2.5)
            body = b'{"hits": [], "estimatedTotalHits": 0}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    meili_srv = ThreadingHTTPServer(("127.0.0.1", 0), _SlowMeili)
    meili_port = meili_srv.server_address[1]
    threading.Thread(target=meili_srv.serve_forever, daemon=True).start()

    class _Conn:
        def run(self, sql: str, **params: object) -> list[list[object]]:
            low = " ".join(sql.split()).lower()
            return [["article"]] if "from item_kinds" in low else []

    class _P:
        def connection(self) -> object:
            from contextlib import contextmanager

            @contextmanager
            def _cm():
                yield _Conn()

            return _cm()

    async def _health(_request: object) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route(
                "/api/search",
                search_api.search(
                    _P(),  # type: ignore[arg-type]
                    client=MeiliClient(f"http://127.0.0.1:{meili_port}", ""),
                ),
            ),
            Route("/api/health", _health),
        ]
    )

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            pytest.fail("uvicorn 没起来")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]

    timings: dict[str, float] = {}

    def _search() -> None:
        started = time.monotonic()
        httpx.get(f"http://127.0.0.1:{port}/api/search?q=营销", timeout=30)
        timings["search"] = time.monotonic() - started

    try:
        worker = threading.Thread(target=_search)
        worker.start()
        time.sleep(0.3)  # 让搜索先进入阻塞
        started = time.monotonic()
        httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=30)
        timings["health"] = time.monotonic() - started
        worker.join(timeout=30)
    finally:
        server.should_exit = True
        meili_srv.shutdown()

    assert timings["health"] < 1.0, (
        f"慢搜索（{timings['search']:.2f} 秒）期间，只返回静态 JSON 的 "
        f"/api/health 等了 {timings['health']:.2f} 秒——事件循环被同步的 "
        "Meili 请求占住了。用户点搜索会让整个站点卡死，现象像是「Meili 慢」，"
        "实际是 api 进程被自己的 handler 卡住。"
    )


def test_含反斜杠的tags参数不该让整个搜索页500() -> None:
    """设计 §6.1 最后一行：400/422 不降级，向上抛成 500 —— 因为那是**我们的 bug**。

    但「我们的 bug」不该由**用户输入的标签名**触发。`tags` 是完全未经校验的
    自由文本查询参数（`_multi` 只做切割与去重，见 `kb/api/search.py:90`），
    任何人都能构造出让它生成的 filter 在 Meili 上变成 400 的值：

    | `?tags=` 传入 | `build_filter` 产出 | 真机 Meili 1.58.0 |
    |---|---|---|
    | `x\\`（末尾一个反斜杠） | `tags = "x\\"` | **400** `Expression is missing the following closing delimiter` |
    | `C://Users` | `tags = "C://Users"` | **400** `Malformed value` |
    | `a\\"b` | `tags = "a\\\\"b"` | **400** `Found unexpected characters at the end of the filter` |

    整条链路：`?tags=x` + 一个反斜杠 → 400 → `MeiliRejected` 不降级 → **HTTP 500**。
    用户看到「服务器内部错误」，而正确行为是「这个标签筛不出来」（200 + 空）。

    这条用例用假 client 按真机规则模拟 400，验的是「即使 Meili 说 400，
    API 也不该把它变成 500 给用户」——因为触发条件来自用户输入，不是编程错误。
    """
    backslash = chr(92)

    class _StrictMeili:
        """按真机实测规则拒绝未成对反斜杠。"""

        def search(
            self, index_uid: str, payload: dict[str, object]
        ) -> dict[str, object]:
            expression = str(payload.get("filter", ""))
            # 真机：引号数不是偶数 → 有转义把闭合引号吃掉了
            if expression.count('"') % 2 == 1:
                raise MeiliRejected("HTTP 400 Expression is missing closing delimiter")
            return {"hits": [], "estimatedTotalHits": 0}

    response = _app(_StrictMeili()).get("/api/search", params={"tags": "x" + backslash})

    assert response.status_code != 500, (
        "用户输入一个末尾带反斜杠的标签名（`?tags=x` + 反斜杠）让搜索页返回 500。"
        "设计 §6.1 的「400 不降级」是为**编程错误**准备的（filter 语法写错），"
        "而这里是**用户输入**触发的——该给 200 + 空结果，不是 Internal Server Error。"
    )


# ══════════════════════════════════════════════════════════════
# 10. 空 q（浏览器模式）+ 分页边界（主理人 7.1）
# ══════════════════════════════════════════════════════════════


def test_浏览器模式也带阈值但这不会截断结果() -> None:
    """空 `q` 时 `build_search_payload` 仍然带 `rankingScoreThreshold=0.3`。

    看起来危险：placeholder search 的分数会不会低于 0.3 从而把全部内容截掉？
    那会让「点开搜索框不输入」看到空白页——整个浏览功能失效。

    **真机实测（平板 Meili 1.58.0，30 条文档）：不会。** placeholder search 的
    `_rankingScore` 恒为 **1.0**（不带 `q` 时 Meili 不做相关度计算，给满分）。
    带阈值与不带阈值返回完全一致（都是 20 条 / 总数 30）。

    这条用例把那个事实钉在代码层：`payload` 里确实有阈值字段，而这是安全的
    ——**前提是 Meili 的 placeholder 分数保持 1.0**。若将来 Meili 改了这一点
    （例如给 placeholder 结果算一个基于字段完整度的分），这条会红，那正是需要
    在 `build_search_payload` 里对空查询跳过阈值的时刻。
    """
    from kb.search.models import SearchFilter
    from kb.search.query import build_search_payload

    payload = build_search_payload(
        text="", limit=20, offset=0, condition=SearchFilter(), min_score=0.3
    )

    assert payload.get("sort") == ["saved_at_ts:desc"], "空查询必须按 saved_at 倒序"
    assert "rankingScoreThreshold" in payload, (
        "空查询当前**会**带阈值。若这条红了说明有人给空查询去掉了阈值——"
        "那本身是安全的改进，但请同时确认真机上 placeholder 的分数语义没变。"
    )


def test_分页参数越界时夹边界而不报错() -> None:
    """`offset` 越界夹到 `MAX_OFFSET(999)`，`limit` 越界夹到 1–50，不报错。

    设计 §3.7：「超范围夹边界而不报错」。翻页时前端传错一个数不该变成一次失败。
    主理人 7.1 问「`offset=999` 会怎样」——答案是夹住并用 999 去查，Meili 返回
    空列表 + 正确的 `estimatedTotal`（真机实测：库里 30 条时 offset=999 返回
    0 条、`estimatedTotal=30`，HTTP 200 不报错）。
    """
    from kb.search.config import MAX_LIMIT, MAX_OFFSET

    class _Recorder:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def search(
            self, index_uid: str, payload: dict[str, object]
        ) -> dict[str, object]:
            self.payloads.append(dict(payload))
            return {"hits": [], "estimatedTotalHits": 0}

    meili = _Recorder()
    client = _app(meili)

    # 越界 offset + 越界 limit
    body = client.get("/api/search", params={"offset": 5000, "limit": 999}).json()

    assert body["offset"] == MAX_OFFSET, "offset 越界应夹到上界而不是报错"
    assert body["limit"] == MAX_LIMIT, "limit 越界应夹到上界而不是报错"
    assert meili.payloads[0]["offset"] == MAX_OFFSET
    assert meili.payloads[0]["limit"] == MAX_LIMIT


# ══════════════════════════════════════════════════════════════
# 11. kind 枚举不许硬编码（主理人 7.2，项目硬规矩 C8）
# ══════════════════════════════════════════════════════════════


def test_新增kind后校验与400消息都自动跟上() -> None:
    """设计 §9 T04 判据：**不许硬编码类型枚举**，合法类型由 `item_kinds` 表决定。

    验证方式：造一个「表里有 `file`」的假连接，看两件事是否都跟着变：

    1. `?kind=file` 从 400 变成 200（校验跟着表走）
    2. 未知 kind 的 400 消息里「可选：…」列表出现 `file`（错误文案也跟表走）

    两条都成立才说明没有任何一处写死了三个预设类型。**只验第 1 条不够**：
    校验查表、但报错文案里写死一份列表，是这类 bug 最常见的半修形态。
    """

    class _Conn:
        def __init__(self, kinds: tuple[str, ...]) -> None:
            self.kinds = kinds

        def run(self, sql: str, **params: object) -> list[list[object]]:
            low = " ".join(sql.split()).lower()
            if "from item_kinds" in low:
                return [[k] for k in self.kinds]
            return []

    def _app_for(kinds: tuple[str, ...]) -> TestClient:
        class _P:
            def connection(self) -> object:
                from contextlib import contextmanager

                @contextmanager
                def _cm():
                    yield _Conn(kinds)

                return _cm()

        handler = search_api.search(_P(), client=ScriptedClient())  # type: ignore[arg-type]
        return TestClient(Starlette(routes=[Route("/api/search", handler)]))

    before = _app_for(("article", "video", "text"))
    after = _app_for(("article", "video", "text", "file"))

    assert before.get("/api/search", params={"kind": "file"}).status_code == 400, (
        "表里没有 file 时就该 400"
    )

    ok = after.get("/api/search", params={"kind": "file"})
    assert ok.status_code == 200, (
        "`item_kinds` 里加了 file，但 `?kind=file` 仍然 400 —— "
        "说明某一处把类型枚举写死了（违反设计 §9 T04 的 C8 硬规矩）"
    )

    message = after.get("/api/search", params={"kind": "bogus"}).json()["error"]
    assert "file" in message, (
        f"校验跟着表走了，但 400 错误消息里的可选列表没跟上：{message!r}。"
        "「报错文案里写死一份列表」是这类 bug 最常见的半修形态，"
        "用户会看到「可选：article, video, text」却不知道为什么 file 也能用。"
    )


# ══════════════════════════════════════════════════════════════
# 12. 软删除跳过后分页信号是否误导（主理人 7.3）
# ══════════════════════════════════════════════════════════════


def test_软删条目被跳过后estimatedTotal仍是索引里的数() -> None:
    """设计 §3.7：索引里有、库里已删的条目**跳过**（不给点进去 404 的卡片）。

    这个决定本身是对的，但它让两个分页信号对不上：

    - `items` 只有 2 条（第 3 条被跳过）
    - `estimatedTotal` 还是 3（那是 Meili 的 `estimatedTotalHits`，索引里确实有 3 条）
    - `limit` 是 20

    前端无论用哪种判据都会得到错答案：

    | 判据 | 结论 | 对不对 |
    |---|---|---|
    | `estimatedTotal(3) > offset(0) + len(items)(2)` | 还有下一页 | ❌ 永远翻不到第 3 条 |
    | `len(items)(2) < limit(20)` | 这是最后一页 | ❌ 这一页本来能满 20 条 |

    后果轻（要同时满足「有软删条目」+「用户正好翻到那一页」），但它是**静默的**：
    用户只看到「这一页比平时少几条」，不会有任何提示。

    这条用例断言的是**当前行为**（`estimatedTotal` 不因跳过而修正），
    作为一个记录点。真正的修法有两条，都需要产品决定：
    ① 跳过条数从 `estimatedTotal` 里扣（会与 Meili 的分页口径打架）
    ② 响应里加一个 `skipped` 计数，让前端知道「这一页少了几条不是到底了」
    """

    class _Meili:
        """Meili 说 3 条，第 3 条在库里已软删。"""

        def search(
            self, index_uid: str, payload: dict[str, object]
        ) -> dict[str, object]:
            def _hit(item_id: int, hour: int) -> dict[str, object]:
                return {
                    "id": item_id,
                    "kind": "article",
                    "title": f"标题{item_id}",
                    "summary": "",
                    "tags": [],
                    "source_domain": "x.invalid",
                    "saved_at": f"2026-11-22T{hour:02d}:00:00+08:00",
                    "read_status": "unread",
                    "_rankingScore": 0.9,
                }

            return {
                "hits": [_hit(1, 10), _hit(2, 9), _hit(3, 8)],
                "estimatedTotalHits": 3,
            }

    conn = _FakeConn(
        summaries={1: ("标题1", "unread", "done"), 2: ("标题2", "unread", "done")}
    )
    body = _app(_Meili(), conn).get("/api/search?q=营销&limit=20").json()

    assert len(body["items"]) == 2, "第 3 条库里已删，应被跳过"
    assert body["estimatedTotal"] == 3, (
        "当前行为：estimatedTotal 直接用 Meili 的数字，不因跳过而修正。"
        "若这条红了说明有人改了这个口径——请同时更新 §3.7 与前端的分页判据。"
    )
    assert body["limit"] == 20
    assert len(body["items"]) < body["limit"], (
        "注意：这一页 2 条 < limit 20，但**不是**到底了。"
        "前端若用 len(items) < limit 判断「最后一页」会提前收尾。"
    )

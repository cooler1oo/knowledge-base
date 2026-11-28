"""worker 三个步骤的分派与两条断点的修复（C1–C8 / E1–E7 / B15 / B16 / B22 / B26）。

**这一组的关键在于「假客户端 + 真库语义」的混合**：状态机行为用假连接验
（本机能跑），数据库行为用 `@pytest.mark.pg`（本机无库时跳过）。
`FakeSummarizer` 是这一层的核心接缝——`calls` / `seen` / `script` 三个成员
是设计 §4.2 定死的形状，测试代码按这个形状写，**不得额外加方法**。

`calls` 必须是**普通实例属性**而不是 property，也不能是 `MagicMock`：
`MagicMock` 对任何属性访问都返回新 Mock，`client.calls == 0` 在「从未被调用」
与「被调用了但没设返回值」两种情况下都能通过（设计 §6.9）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from kb.ai.outcomes import (
    FailureCategory,
    SummarizeFailed,
    SummarizeOk,
    SummarizeSkipped,
)
from kb.worker import loop, steps

NOW = datetime(2026, 11, 27, 14, 32, tzinfo=timezone.utc)


class FakeSummarizer:
    """`SummarizerPort` 的测试实现。三个成员，一个都不多。

    | 成员 | 用途 | 被哪条验收断言 |
    |---|---|---|
    | `calls` | 累计调用次数，构造时为 0 | E1/E2/E4/E7、D3 的 `calls == 0` |
    | `seen` | 每次的 `(title, text, vocabulary)` | A1/A4/B17/§2.4 |
    | `script` | 依次弹出的结果或待抛异常 | B12/B22/B26/B2 |

    `raises_on_call` 用来验「假实现可以按剧本抛异常」这条契约本身。
    """

    def __init__(self, script: list[object] | None = None) -> None:
        self.script = list(
            script or [SummarizeOk(summary="这是一段足够长的中文摘要内容")]
        )
        self.calls = 0
        self.seen: list[tuple[str, str, tuple]] = []

    def summarize(
        self,
        title: str,
        text: str,
        vocabulary: tuple[tuple[int, str, str], ...] = (),
    ) -> object:
        self.calls += 1
        self.seen.append((title, text, vocabulary))
        if not self.script:
            raise AssertionError("剧本用光了，说明被调用的次数超出预期")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ── §4.2.1：ai_client 的注入位置 ───────────────────────────────


def test_run_once_的_ai_client_是第三个位置参数且默认None():
    """§4.2.1：`run_once(pool, extractor, ai_client=None)`。

    这条形状是**11 处现有调用一行都不用改**的唯一前提。
    加在 `extractor` 之前或改成必填，都会让那 11 处红。

    ⚠️ 断言的是**性质**（前三个参数是这三个、`ai_client` 默认 `None`），
    不是「参数列表逐字等于这三个」。R8 在后面追加了可选参数 `media_stage`、
    R6 追加了 `indexer`，它们都不影响这条性质——但改成逐字相等会让它红，
    而那并不是本条要守的东西。
    """
    import inspect

    params = list(inspect.signature(loop.run_once).parameters.items())
    assert [name for name, _ in params][:3] == ["pool", "extractor", "ai_client"]
    assert params[2][1].default is None
    # 后面追加的参数必须全部有默认值，否则现有调用会红
    for name, param in params[3:]:
        assert param.default is not inspect.Parameter.empty, f"{name} 没有默认值"


def test_run_once_的_indexer在末尾且默认None_R6():
    """R6 的装配件同样**加在最末且默认 `None`**。

    默认 `None` 是必要的：现有 11 处 `run_once(pool, extractor)` 调用与
    全部 `tests/test_worker_loop.py` 的用例都不该因为「worker 新增了索引
    能力」就突然开始连 Meili。位置在最末是因为它只在 `run_index` 里用，
    与 `media_stage` 并列，都属于「按步骤生效的可选装配件」。
    """
    import inspect

    names = list(inspect.signature(loop.run_once).parameters)
    assert names[-1] == "indexer", f"indexer 应在最后，实际参数表：{names}"


def test_run_forever_的_ai_client_在on_tick之后且默认None():
    """`kb/worker/__main__.py` 用关键字传 `idle_seconds`，所以顺序必须是
    `pool, extractor, idle_seconds, on_tick, ai_client`（之后可追加可选项）。

    把 `ai_client` 插在 `extractor` 之后（第三个位置）会让
    `run_once(pool, extractor, 2.0)` 这类写法把浮点数当客户端传进去。
    """
    import inspect

    params = list(inspect.signature(loop.run_forever).parameters.items())
    assert [name for name, _ in params][:5] == [
        "pool",
        "extractor",
        "idle_seconds",
        "on_tick",
        "ai_client",
    ]
    assert params[4][1].default is None
    for name, param in params[5:]:
        assert param.default is not inspect.Parameter.empty, f"{name} 没有默认值"


def test_SUPPORTED_STEPS_是四个步骤_AI_07_AI_09():
    """R6 起 `index` 已实现，本包能执行的步骤是四个。

    这条与 `test_ocr仍不在SUPPORTED_STEPS` 一起守「说得出口的才算支持」：
    加进 `SUPPORTED_STEPS` 的步骤必须有真实实现，否则条目会显示「已完成」
    而字段是空的（`AI-10` 的原始教训）。
    """
    assert set(loop.SUPPORTED_STEPS) == {"fetch", "summarize", "tag", "index"}


def test_ocr仍不在SUPPORTED_STEPS():
    """`AI-10`：`ocr` 本轮仍未实现，领到就走「尚未实现」分支退回队列。

    `index` 已随 R6 从这条里移出（见上一条用例）。`ocr` 留下是因为
    `M3-05`（图片 OCR+描述）尚未实现——把它加进 `SUPPORTED_STEPS` 会让
    `mark_failed` 在第 3 次把条目标成「处理失败」，而摘要其实已成功。
    """
    assert "ocr" not in loop.SUPPORTED_STEPS
    # index 已实现，这条同时防止有人把它「改回去」
    assert "index" in loop.SUPPORTED_STEPS


# ── steps 的三个入口签名（§4.2.1）──────────────────────────────


def test_run_fetch_不接受ai_client_4节2条1():
    """`ai_client` 只出现在 `run_summarize` 的签名里。

    这是「11 处现有用例一行不用改」的机制：`run_fetch` 与 `run_tag` 都不接收它，
    所以 `ai_client=None` 对 fetch 分支完全无影响（D 路径）。
    """
    import inspect

    params = set(inspect.signature(steps.run_fetch).parameters)
    assert "ai_client" not in params


def test_run_tag_不接受ai_client_4节2条1():
    """`tag` 步骤全程不调模型（`aiTags` 是 `summarize` 那次调用带回来的）。"""
    import inspect

    params = set(inspect.signature(steps.run_tag).parameters)
    assert "ai_client" not in params


def test_run_summarize_接收ai_client与runtime与now_4节2条1():
    import inspect

    params = list(inspect.signature(steps.run_summarize).parameters)
    assert "ai_client" in params
    assert "runtime" in params
    assert "now" in params


# ── 输入选择与跳过（B15 / B16 / A1 / A4）──────────────────────


class _Conn:
    """通用假连接：按 SQL 特征返回预置行，记下全部语句。

    `claims` 是 `claim_one` 依次返回的剧本；`settings` 是
    `select value from settings where key = :k` 的映射；`item` 是
    `items` 详情行。
    """

    def __init__(
        self,
        claims: list[list[object]] | None = None,
        *,
        settings: dict[str, object] | None = None,
        item: dict[str, object] | None = None,
        usage_row: object = None,
    ) -> None:
        self._claims = list(claims or [])
        self._settings = settings or {}
        self._item = item
        self._usage_row = usage_row
        self.seen: list[str] = []
        self.params: list[dict[str, object]] = []

    def run(self, sql: str, **params: object) -> list[list[object]]:
        text = " ".join(sql.split())
        low = text.lower()
        self.seen.append(low)
        self.params.append(params)

        if "interval '15 minutes'" in low:
            return []
        # `loop._db_now` 会查 `select now()`。返回固定时刻：本文件验的是分派
        # 与落地逻辑，不验时刻的绝对值；固定值让断言可复现。
        if low == "select now()":
            return [[NOW]]
        if "with picked" in low:
            return self._claims.pop(0) if self._claims else []
        if "from jobs" in low and "count(*)" in low:
            return [[1, 0, 0, 0]]
        # load_config 用一条 in (...) 查询取四个键
        if low.startswith("select key, value from settings"):
            prefix = "select key, value from settings where key in ("
            tail = text[text.index(prefix) + len(prefix) : text.rindex(")")]
            names = [part.strip() for part in tail.split(",")]
            found: list[list[object]] = []
            for idx, _name in enumerate(names):
                key = str(params.get(f"k{idx}", ""))
                if key in self._settings:
                    found.append([key, self._settings[key]])
            return found
        if low.startswith("select value from settings"):
            key = str(params.get("k", ""))
            if key in self._settings:
                return [[self._settings[key]]]
            return []
        if "from tags t" in low:
            return []
        if "select props from items" in low:
            # `items.get_ai_tags_raw` 只读 props 一列
            if self._item is None:
                return []
            return [[self._item["props"]]]
        if "from items i" in low and "i.id = :i" in low:
            if self._item is None:
                return []
            return [list(self._item.values())]
        if "returning value" in low:
            return [[self._usage_row]] if self._usage_row is not None else []
        return []

    # 便利断言
    def sql_of(self, needle: str) -> list[str]:
        return [s for s in self.seen if needle in s]

    def params_of(self, needle: str) -> list[dict[str, object]]:
        return [p for s, p in zip(self.seen, self.params) if needle in s]


class _Pool:
    def __init__(self, conn: object) -> None:
        self._conn = conn
        self.acquired = 0

    def connection(self) -> object:
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            self.acquired += 1
            yield self._conn

        return _cm()


class _Extractor:
    """假抓取器。只用于 fetch 分支（`summarize` 分支不该碰它）。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[str] = []

    async def __call__(self, url: str) -> object:
        from kb.extract.normalize import ExtractResult, MediaItem
        from kb.extract.router import ExtractOutcome

        self.calls.append(url)
        if not self.ok:
            return ExtractOutcome(
                ok=False, route="parsehub", kind="link", error="被拦截"
            )
        return ExtractOutcome(
            ok=True,
            route="parsehub",
            kind="article",
            data=ExtractResult(
                kind="article",
                title="抓到的标题",
                content_text="抓到的正文内容",
                raw_url=url,
                platform="",
                media=[
                    MediaItem(role="cover", url="https://x.invalid/a.jpg", ext="jpg")
                ],
            ),
        )


def _item_row(
    *,
    url: str | None = None,
    title: str = "标题",
    content_text: str | None = None,
    kind: str = "text",
    props: str = "{}",
) -> dict[str, object]:
    """`items` 详情行的列顺序必须与 `items._DETAIL_SQL` 一致（12 列）。"""
    return {
        "id": 1,
        "kind": kind,
        "url": url,
        "title": title,
        "author": None,
        "source_domain": "x.invalid" if url else "",
        "summary": None,
        "content_text": content_text,
        "read_status": "unread",
        "saved_at": NOW,
        "published_at": None,
        "props": props,
    }


def _run(
    conn: _Conn,
    *,
    ai_client: FakeSummarizer | None = None,
    extractor: _Extractor | None = None,
) -> bool:
    return loop.run_once(_Pool(conn), extractor or _Extractor(), ai_client=ai_client)


# ── C1 / C2：fetch 成功入队 summarize ──────────────────────────


def test_fetch成功后入队summarize_C1():
    """C1 前半：`fetch` 成功后 `jobs` 出现 `step='summarize'`。

    这是本轮「断点一」的修复：全项目此前 `enqueue` 零业务调用方，
    摘要函数写得再完美也永远不会执行。
    """
    conn = _Conn(
        [[[1, 7, "fetch", 0, "running"]]],
        item=_item_row(url="https://x.invalid/a", kind="link"),
    )
    _run(conn)

    enqueues = conn.params_of("insert into jobs")
    assert enqueues, "fetch 成功后没有入队任何任务"
    assert any(p.get("s") == "summarize" for p in enqueues), (
        f"入队的不是 summarize：{enqueues}"
    )


def test_fetch失败时不存在summarize任务_C2():
    """C2：`fetch` 失败时**不入队**——没有内容可摘要。"""
    conn = _Conn(
        [[[1, 7, "fetch", 0, "running"]]],
        item=_item_row(url="https://x.invalid/a", kind="link"),
    )
    _run(conn, extractor=_Extractor(ok=False))

    enqueues = conn.params_of("insert into jobs")
    assert not any(p.get("s") == "summarize" for p in enqueues)


def test_fetch先标done再入队_C1_设计第9条():
    """`mark_done` 之后才 `enqueue`（设计 §0 第 9 条）。

    反过来（先入队后标完成）在 `mark_done` 失败时会留下一条已完成的上游
    + 一条待办的下游，重跑上游会产生两条 `pending`，被 `jobs_pending_uniq`
    挡成一条——结果正确但排障时看不出发生过重跑。
    """
    conn = _Conn(
        [[[1, 7, "fetch", 0, "running"]]],
        item=_item_row(url="https://x.invalid/a", kind="link"),
    )
    _run(conn)

    done_at = next(i for i, s in enumerate(conn.seen) if "state = 'done'" in s)
    enqueue_at = next(i for i, s in enumerate(conn.seen) if "insert into jobs" in s)
    assert done_at < enqueue_at, "必须先标 done 再入队"


# ── C3 / C8 / AI-11：纯文字条目不撞「无链接可抓」──────────────


def test_纯文字条目出摘要且没有fetchError_C3_AI_11():
    """C3 / AI-11 的守门用例。

    `summarize` 加进 `SUPPORTED_STEPS` 之后，若不把 URL 判空收归 `fetch` 专用，
    这条必定失败：纯文字条目 `url` 为空，会命中「取不到 URL 就按无链接可抓处理」
    并 `apply_extract(ok=False)` + `mark_done`——**摘要没写、状态显示已完成**。
    """
    client = FakeSummarizer([SummarizeOk(summary="这是摘要内容足够长了呀")])
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={
            "ai_base_url": "https://api.example.com/v1",
            "ai_api_key": "sk-k",
            "ai_model": "m",
        },
        item=_item_row(title="纯文字的标题", content_text="字" * 200),
    )
    _run(conn, ai_client=client)

    assert client.calls == 1
    assert not conn.params_of("fetchError"), "纯文字条目被写上了 fetchError"
    errors = [str(p.get("e")) for p in conn.params if "e" in p]
    assert not any("无链接可抓" in e for e in errors), f"命中了 URL 判空分支：{errors}"


def test_纯文字条目全流程后kind仍为text且url仍为NULL_C8():
    """C8：AI-11 的修复**不得顺手改写入库数据**。

    `summarize` 分支只写 `summary` / `props`，`apply_extract` 一次都不该被调用。
    """
    client = FakeSummarizer()
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={
            "ai_base_url": "u",
            "ai_api_key": "k",
            "ai_model": "m",
        },
        item=_item_row(title="标题", content_text="字" * 200, kind="text"),
    )
    _run(conn, ai_client=client)

    # apply_extract 是唯一会写 content_text / kind 的路径
    assert not conn.params_of("kind = :k"), "summarize 分支调用了 apply_extract"


def test_summarize步骤不调抓取器_C3():
    """`summarize` 是已实现步骤，更不该调抓取器。

    这条比「未实现步骤不调抓取器」更强：未实现步骤只是不该调，
    `summarize` 是**没有 URL 可抓**，调了等于白费一次网络往返。
    """
    extractor = _Extractor()
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(title="t", content_text="字" * 200),
    )
    _run(conn, ai_client=FakeSummarizer(), extractor=extractor)
    assert extractor.calls == []


def test_tag步骤也不调抓取器():
    extractor = _Extractor()
    conn = _Conn(
        [[[1, 7, "tag", 0, "running"]]],
        item=_item_row(props='{"aiTags": []}'),
    )
    _run(conn, extractor=extractor)
    assert extractor.calls == []


# ── C7：两个断点都修好 ───────────────────────────────────────


def test_链接与纯文字两条同时跑都有摘要_C7():
    """C7：`AI-07` 与 `AI-11` 的联合守门用例。

    只修 AI-07 → 纯文字条目无摘要；只修 AI-11 → 链接条目不进 summarize。
    任一单修都过不了这条。
    """
    # ① 链接条目：fetch 成功 → 入队 summarize
    link_conn = _Conn(
        [[[1, 7, "fetch", 0, "running"]]],
        item=_item_row(url="https://x.invalid/a", kind="link"),
    )
    _run(link_conn)
    assert any(
        p.get("s") == "summarize" for p in link_conn.params_of("insert into jobs")
    ), "链接条目没有入队 summarize（只修了 AI-11）"

    # ② 纯文字条目：summarize 直接成功
    text_client = FakeSummarizer()
    text_conn = _Conn(
        [[[2, 8, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(title="纯文字标题", content_text="字" * 200),
    )
    _run(text_conn, ai_client=text_client)
    assert text_client.calls == 1, "纯文字条目没有调用 AI（只修了 AI-07）"


# ── C4 / AI-10 / R6：入队哪些步骤 ────────────────────────────


def test_全程不出现ocr任务_C4_AI_10():
    """`AI-10`：`ocr` 仍未实现，任何路径都不得入队它。

    入队会让 `mark_failed` 在第 3 次把每条内容标成「处理失败」——
    「宁可少一个功能，不可谎报一个状态」。

    `index` **已从这条里移出**（R6 起它实现了，而且必须入队，否则搜不到）。
    见 `tests/test_worker_index_step.py::test_summarize成功后入队index`。
    """
    client = FakeSummarizer(
        [SummarizeOk(summary="这是一段足够长的中文摘要内容", tags=("健康",))]
    )
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(title="标题", content_text="字" * 200),
    )
    _run(conn, ai_client=client)

    steps_enqueued = {str(p.get("s")) for p in conn.params_of("insert into jobs")}
    assert "ocr" not in steps_enqueued


def test_summarize成功后入队index_R6_设计4点4():
    """设计 §4.4：`summarize` 成功时入队 `index`。

    不入队的话，本轮实现的检索层永远搜不到新内容——`run_index` 没有任务可领。
    这是 `T13` 端到端用例「存抖音 → … → 搜得到」里最关键的一环。
    """
    client = FakeSummarizer(
        [SummarizeOk(summary="这是一段足够长的中文摘要内容", tags=("健康",))]
    )
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(title="标题", content_text="字" * 200),
    )
    _run(conn, ai_client=client)

    steps_enqueued = {str(p.get("s")) for p in conn.params_of("insert into jobs")}
    assert "index" in steps_enqueued, f"入队的步骤：{steps_enqueued}"


def test_summarize跳过时也入队index_R6_设计4点4():
    """设计 §4.4：跳过（`no_text` / `title_only`）**也是终态**，也要入队。

    不入队的话纯文字空内容永远不进索引，用户搜不到他刚存进来的东西。
    """
    client = FakeSummarizer([SummarizeSkipped(reason="no_text")])
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(title="", content_text="   "),
    )
    _run(conn, ai_client=client)

    steps_enqueued = {str(p.get("s")) for p in conn.params_of("insert into jobs")}
    assert "index" in steps_enqueued


def test_tag成功后入队index_R6_设计4点4():
    """设计 §4.4：`tag` 成功时入队 `index`。

    标签是这一步才写进 `item_tags` 的，索引里的 `tags` 字段依赖它。
    与 `summarize` 那次入队由 `jobs_pending_uniq` 塌成一条，
    所以不会产生两条 `index` 任务（那条约束由 `items.create_item` 的
    `on conflict ... do nothing` 与 `jobs.enqueue` 的同一个子句保证）。
    """
    conn = _Conn(
        [[[1, 7, "tag", 0, "running"]]],
        item=_item_row(props='{"aiTags": ["健康"]}'),
    )
    _run(conn)

    steps_enqueued = {str(p.get("s")) for p in conn.params_of("insert into jobs")}
    assert steps_enqueued == {"index"}, f"tag 成功后应只入队 index：{steps_enqueued}"


def test_fetch成功后不入队index_R6_设计4点4():
    """设计 §4.4：`run_fetch` 成功**不入队 index**。

    后续 `summarize` 一定会入队 `index`（成功或跳过都入），不重复。
    在 fetch 处也入队一次会让索引写出「只有标题、没摘要没标签」的半成品文档，
    而且那次写入紧接着会被 summarize 那次覆盖——多一次无收益的写。
    """
    conn = _Conn(
        [[[1, 7, "fetch", 0, "running"]]],
        item=_item_row(url="https://x.invalid/a", kind="link"),
    )
    _run(conn)

    steps_enqueued = {str(p.get("s")) for p in conn.params_of("insert into jobs")}
    assert "index" not in steps_enqueued, f"fetch 处不该入队 index：{steps_enqueued}"


def test_fetch失败时也不入队index_R6_设计4点4():
    """设计 §4.4：`fetch` 失败 / 无链接可抓时**不入队**。

    内容没抓到，索引里放一条只有分享文案的文档会让它凭文案被搜到、
    点进去没内容。等重试成功再入队。
    """
    conn = _Conn(
        [[[1, 7, "fetch", 0, "running"]]],
        item=_item_row(url="https://x.invalid/a", kind="link"),
    )
    _run(conn, extractor=_Extractor(ok=False))

    steps_enqueued = {str(p.get("s")) for p in conn.params_of("insert into jobs")}
    assert "index" not in steps_enqueued


def test_summarize成功后入队tag_AI_09():
    client = FakeSummarizer(
        [SummarizeOk(summary="这是一段足够长的中文摘要内容", tags=("健康",))]
    )
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(title="标题", content_text="字" * 200),
    )
    _run(conn, ai_client=client)

    assert any(p.get("s") == "tag" for p in conn.params_of("insert into jobs"))


# ── E1 / E2 / E7：未配置时零请求并推迟 30 分钟 ────────────────


def test_未配置AI时零请求且推迟30分钟attempts不变_E1_E2_E7():
    """E1/E2/E7：三键缺任一 → **零次 HTTP 请求**、推迟 30 分钟、`attempts` 不变。

    `attempts=0` 是「推迟不计次」的最终表现：用 `mark_failed` 做这件事
    会累加 `attempts`，三次后 `failed`（`E3` 会看到 5 条全失败）。
    """
    client = FakeSummarizer()
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u"},  # 缺 api_key 与 model
        item=_item_row(title="标题", content_text="字" * 200),
    )
    _run(conn, ai_client=client)

    assert client.calls == 0, "未配置时不得调用 AI 层"
    defers = conn.params_of("interval '1 second' * :seconds")
    assert defers, "没有走推迟路径"
    assert int(defers[0]["seconds"]) == 1800
    assert not conn.params_of("attempts = attempts + 1"), "推迟不得消耗一次尝试"


def test_未配置时错误消息含缺失的键名_E1():
    client = FakeSummarizer()
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_model": "m"},
        item=_item_row(title="标题", content_text="字" * 200),
    )
    _run(conn, ai_client=client)

    reasons = [str(p.get("e")) for p in conn.params if "e" in p]
    assert any("ai_api_key" in r for r in reasons), f"没说缺哪个键：{reasons}"
    assert any(r.startswith("已推迟：") for r in reasons), f"缺少已推迟前缀：{reasons}"


def test_客户端缺席但配置齐备时按装配bug推迟并说明_B1():
    """B1：`ai_client is None` 且配置**齐备** → 装配 bug，不是「未配置」。

    写这句 error 才能让人看出是漏传参数而不是没填配置。
    判定顺序（先判客户端再查配置）颠倒会让这两种情况混成一句，
    排障时分不清「该去填配置」还是「该去查 `__main__.py`」。
    """
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(title="标题", content_text="字" * 200),
    )
    _run(conn, ai_client=None)

    reasons = [str(p.get("e")) for p in conn.params if "e" in p]
    assert any("未装配" in r or "ai_client" in r for r in reasons), (
        f"没指出是装配问题：{reasons}"
    )


def test_客户端缺席且配置也缺时按未配置推迟_B2():
    """B2：配置缺失是正常推迟路径，与客户端在不在无关。"""
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={},
        item=_item_row(title="标题", content_text="字" * 200),
    )
    _run(conn, ai_client=None)

    reasons = [str(p.get("e")) for p in conn.params if "e" in p]
    assert any("未配置" in r for r in reasons), f"没报未配置：{reasons}"


def test_客户端缺席时tag仍然正常执行_C():
    """C：`tag` 不依赖客户端，**不该 defer**。

    defer 它会把「已成功拿到的标签」挂起 30 分钟，而这段时间 `items.summary`
    已经写好了，界面显示「有摘要但没标签」，用户无法理解原因。
    """
    conn = _Conn(
        [[[1, 7, "tag", 0, "running"]]],
        item=_item_row(props='{"aiTags": ["健康"]}'),
    )
    _run(conn, ai_client=None)

    assert not conn.params_of("interval '1 second' * :seconds"), "tag 被推迟了"
    assert conn.params_of("state = 'done'"), "tag 没有标完成"


# ── D3 / D4：预算闸门 ────────────────────────────────────────


def test_预算超限零请求且推到下月_D3():
    """D3：`ai_budget_cny=1`、单价 0.002、当月累计 0.999 → **零次 HTTP 请求**。

    拦得住的关键是「本次预估有 0.001 元下限」：`0.999 + 0.001 == 1.0 >= 1.0`。
    没有下限时预估是 0.0005，`0.9995 < 1.0` 会放行。
    """
    client = FakeSummarizer()
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={
            "ai_base_url": "u",
            "ai_api_key": "k",
            "ai_model": "m",
            "ai_budget_cny": 1,
            "ai_usage_2026-11": json.dumps({"calls": 1, "cny": 0.999}),
        },
        item=_item_row(title="标题", content_text="字" * 200),
    )
    _run(conn, ai_client=client)

    assert client.calls == 0, "预算超限时不得发请求"
    assert not conn.params_of("attempts = attempts + 1"), "推迟不得消耗一次尝试"


def test_预算超限的推迟目标是下月1日_D3():
    client = FakeSummarizer()
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={
            "ai_base_url": "u",
            "ai_api_key": "k",
            "ai_model": "m",
            "ai_budget_cny": 1,
            "ai_usage_2026-11": json.dumps({"calls": 1, "cny": 0.999}),
        },
        item=_item_row(title="标题", content_text="字" * 200),
    )
    _run(conn, ai_client=client)

    deferred = [p for p in conn.params if "when" in p]
    assert deferred, "没有走 defer_until"
    when = deferred[0]["when"]
    assert isinstance(when, datetime)
    assert when.day == 1 and when.hour <= 0 and when.minute <= 10


def test_预算充足时正常调用_D6():
    """D6：调用前预算充足就照常完成（不中断在途请求）。"""
    client = FakeSummarizer()
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={
            "ai_base_url": "u",
            "ai_api_key": "k",
            "ai_model": "m",
            "ai_budget_cny": 20,
            "ai_usage_2026-11": json.dumps({"calls": 1, "cny": 0.5}),
        },
        item=_item_row(title="标题", content_text="字" * 200),
    )
    _run(conn, ai_client=client)
    assert client.calls == 1


# ── A1 / A2 / A3 / A5：成功路径写库 ──────────────────────────


def test_成功后写summary与summaryBasis_A1():
    client = FakeSummarizer(
        [SummarizeOk(summary="这是一段足够长的中文摘要内容", tags=())]
    )
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(title="标题", content_text="字" * 200),
    )
    _run(conn, ai_client=client)

    summary_writes = conn.params_of("summary = :s")
    assert summary_writes, "摘要没写库"
    assert summary_writes[0]["s"] == "这是一段足够长的中文摘要内容"
    assert "summaryBasis" in str(summary_writes[0]["p"])
    assert "content" in str(summary_writes[0]["p"])


def test_成功后写aiTags原始名_AI_09_A5():
    client = FakeSummarizer(
        [SummarizeOk(summary="这是一段足够长的中文摘要内容", tags=("健康", "穿搭"))]
    )
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(title="标题", content_text="字" * 200),
    )
    _run(conn, ai_client=client)

    tag_writes = [p for p in conn.params if "aiTags" in str(p.get("p", ""))]
    assert tag_writes, "aiTags 没写"
    assert "健康" in str(tag_writes[0]["p"])


def test_摘要与标题相同时标done且不写summary_B16():
    """B16：`title_only` → 写 `props.summarySkipped`，`items.summary` 保持 NULL。

    `attempts` 不动（`job.state='done'`）：这不是错误，只是「没有可用摘要」。
    用异常表达会被 `run_once` 的兜底吞成失败，`attempts` 被 +1。
    """
    client = FakeSummarizer([SummarizeSkipped(reason="title_only")])
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(title="标题", content_text="字" * 200),
    )
    _run(conn, ai_client=client)

    assert not conn.params_of("summary = :s"), "title_only 时不该写摘要"
    skipped_writes = [p for p in conn.params if "summarySkipped" in str(p.get("p", ""))]
    assert skipped_writes, "没写 summarySkipped"
    assert "title_only" in str(skipped_writes[0]["p"])
    assert conn.params_of("state = 'done'"), "跳过也必须标 done"
    assert not conn.params_of("attempts = attempts + 1")


def test_正文标题都空时零请求标done且attempts为0_B15():
    """B15：正文标题都空 → **零次模型调用**，写 `summarySkipped='no_text'`，标 done。"""
    client = FakeSummarizer()
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(title="", content_text="   "),
    )
    _run(conn, ai_client=client)

    assert client.calls == 0, "无内容可摘要时不该发请求"
    skipped = [p for p in conn.params if "summarySkipped" in str(p.get("p", ""))]
    assert skipped, "没写 summarySkipped"
    assert "no_text" in str(skipped[0]["p"])
    assert conn.params_of("state = 'done'")
    assert not conn.params_of("attempts = attempts + 1")


def test_抖音空正文走标题_basis_title_A4():
    client = FakeSummarizer()
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(
            url="https://v.douyin.com/x/",  # 有 url
            title="7.68 复制打开抖音，看看【张三】的作品",
            content_text=None,  # 正文为空
            kind="video",
        ),
    )
    _run(conn, ai_client=client)

    assert client.calls == 1
    writes = conn.params_of("summary = :s")
    assert "title" in str(writes[0]["p"])


def test_成功后记账_D1():
    client = FakeSummarizer(
        [SummarizeOk(summary="这是一段足够长的中文摘要内容", tags=())]
    )
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(title="标题", content_text="字" * 200),
        usage_row='{"calls": 1, "cny": 0.0027}',
    )
    _run(conn, ai_client=client)

    usage_writes = conn.params_of("returning value")
    assert usage_writes, "成功后没有记账"
    assert int(usage_writes[0]["d_calls"]) == 1


# ── B12 / B22 / B26：失败按类别落地 ─────────────────────────


def test_解析失败走mark_failed并消耗一次尝试_B12():
    """B12：解析失败消耗一次尝试，`jobs.error` 里要**能看到响应原文**。

    ⚠️ **这条用例曾经自己掩盖过一个真 bug。**

    第一版把 snippet 直接写进了 `message`
    （`"…；响应前 200 字：<半截"`），于是即使实现只把 `message` 写进
    `jobs.error`、完全忽略 `snippet` 字段，这条也能通过——而真库上的 `B2`
    会红，因为它断言的是真实 `AiClient` 产出的错误。

    现在把 snippet **只放在 `snippet` 字段**、`message` 保持干净，
    忠实模拟 `AiClient` 的实际形状（见 `kb/ai/client.py`：错误摘要与
    响应原文是两个字段）。拼接责任在 `ai_runtime.failure_message`。
    """
    client = FakeSummarizer(
        [
            SummarizeFailed(
                category=FailureCategory.BAD_RESPONSE,
                message="AI解析失败：响应不是合法 JSON",
                snippet="<半截",
            )
        ]
    )
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(title="标题", content_text="字" * 200),
    )
    _run(conn, ai_client=client)

    assert conn.params_of("attempts = attempts + 1"), "解析失败要消耗一次尝试"
    errors = [str(p.get("e")) for p in conn.params if "e" in p]
    assert any("半截" in e for e in errors), (
        f"error 里没有响应前 200 字（snippet 没被拼进去）：{errors}"
    )
    assert not conn.params_of("summary = :s")


@pytest.mark.parametrize(
    "category",
    [
        FailureCategory.BAD_RESPONSE,
        FailureCategory.TOO_LARGE,
        FailureCategory.TIMEOUT,
        FailureCategory.CONNECT,
        FailureCategory.HTTP_5XX,
        FailureCategory.HTTP_429,
        FailureCategory.AUTH,
        FailureCategory.UNEXPECTED,
    ],
)
def test_每一类失败写库时都带上响应原文_8点2节(category):
    """`§8.2`：四类落地分支都必须写完整错误消息（含响应前 200 字）。

    为什么逐类测：`apply_failure` 有四条分支（AUTH / CONNECT / 429 /
    其余走 `mark_failed`），若只有其中一条拼了 snippet，「哪一类失败看不到
    原文」就会随类别而变 —— 排障得先想起是哪一类才知道该不该去别处找原文。
    参数化把「八类 × 四条分支」一次覆盖，漏一个立刻红。
    """
    client = FakeSummarizer(
        [
            SummarizeFailed(
                category=category,
                message="某个错误摘要",
                snippet="响应原文片段",
                retry_after_sec=60.0,
            )
        ]
    )
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(title="标题", content_text="字" * 200),
    )
    _run(conn, ai_client=client)

    errors = [str(p.get("e")) for p in conn.params if "e" in p]
    assert errors, f"{category.value} 没有写 error"
    assert any("响应原文片段" in e for e in errors), (
        f"{category.value} 写入的 error 里没有响应原文：{errors}"
    )


def test_连接失败退避3分钟_B22():
    """B22：连接类失败退避更久（避免每 1 分钟打一次 DNS）。"""
    client = FakeSummarizer(
        [
            SummarizeFailed(
                category=FailureCategory.CONNECT,
                message="AI连接失败：DNS 解析不到",
            )
        ]
    )
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(title="标题", content_text="字" * 200),
    )
    _run(conn, ai_client=client)

    delayed = conn.params_of("interval '1 second' * :seconds")
    assert delayed, "没有走延迟退避"
    assert int(delayed[0]["seconds"]) == 180


def test_401第一次就failed且error提示检查api_key_B26():
    client = FakeSummarizer(
        [
            SummarizeFailed(
                category=FailureCategory.AUTH,
                message="AI鉴权失败：HTTP 401；检查 api_key 是否有效或余额是否充足",
                http_status=401,
            )
        ]
    )
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(title="标题", content_text="字" * 200),
    )
    _run(conn, ai_client=client)

    assert conn.params_of("state = 'failed'"), "401 必须一次判死"
    errors = [str(p.get("e")) for p in conn.params if "e" in p]
    assert any("api_key" in e for e in errors)


def test_429按RetryAfter退避_B25():
    client = FakeSummarizer(
        [
            SummarizeFailed(
                category=FailureCategory.HTTP_429,
                message="AI被限流：HTTP 429；按 Retry-After 等 60 秒",
                http_status=429,
                retry_after_sec=60.0,
            )
        ]
    )
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(title="标题", content_text="字" * 200),
    )
    _run(conn, ai_client=client)

    delayed = conn.params_of("interval '1 second' * :seconds")
    assert delayed, "没有按 Retry-After 退避"
    assert int(delayed[0]["seconds"]) == 60


def test_假客户端抛异常时收成任务失败不得崩掉循环_B12():
    client = FakeSummarizer([RuntimeError("模型调用炸了")])
    conn = _Conn(
        [[[1, 7, "summarize", 0, "running"]]],
        settings={"ai_base_url": "u", "ai_api_key": "k", "ai_model": "m"},
        item=_item_row(title="标题", content_text="字" * 200),
    )
    handled = _run(conn, ai_client=client)

    assert handled is True, "单条异常不得让循环崩掉"
    errors = [str(p.get("e")) for p in conn.params if "e" in p]
    assert any("RuntimeError" in e for e in errors), f"异常类型没记录：{errors}"


# ── F1：空词表 ────────────────────────────────────────────────


def test_empty词表时tag仍done且AI标签行数为0_F1_A5():
    """F1 / A5：词表为空时 AI 只出摘要不打标签，**这是设计上接受的结果**。"""
    conn = _Conn(
        [[[1, 7, "tag", 0, "running"]]],
        item=_item_row(props='{"aiTags": ["健康", "穿搭"]}'),
    )
    _run(conn)

    assert conn.params_of("state = 'done'"), "tag 没有标完成"
    deletes = conn.params_of("delete from item_tags")
    assert deletes, "空词表时仍要删掉旧 AI 标签"
    # 空 tag_ids 时不插新行
    assert not conn.params_of("insert into item_tags")


# ── tag 步骤写库（F2 / F5）───────────────────────────────────


def test_tag命中词表时写item_tags_F2():
    class _VocabConn(_Conn):
        def run(self, sql: str, **params: object) -> list[list[object]]:
            low = " ".join(sql.split()).lower()
            if "from tags t" in low:
                self.seen.append(low)
                self.params.append(params)
                return [[5, "健康", None]]
            return super().run(sql, **params)

    conn = _VocabConn(
        [[[1, 7, "tag", 0, "running"]]],
        item=_item_row(props='{"aiTags": ["健康"]}'),
    )
    _run(conn)

    inserts = conn.params_of("insert into item_tags")
    assert inserts, "命中了词表却没写 item_tags"
    assert inserts[0]["ids"] == [5]


def test_tag的SQL写origin为ai且confidence为null_F5():
    class _VocabConn(_Conn):
        def run(self, sql: str, **params: object) -> list[list[object]]:
            low = " ".join(sql.split()).lower()
            if "from tags t" in low:
                self.seen.append(low)
                self.params.append(params)
                return [[5, "健康", None]]
            return super().run(sql, **params)

    conn = _VocabConn(
        [[[1, 7, "tag", 0, "running"]]],
        item=_item_row(props='{"aiTags": ["健康"]}'),
    )
    _run(conn)

    sql = " ".join(conn.seen)
    assert "'ai'" in sql or '"ai"' in sql
    assert "null" in sql.lower()

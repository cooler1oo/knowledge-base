"""worker 主循环：单条任务的处理与异常兜底。

三条设计约束，每条都有用例：

1. **单条任务失败不得中断循环**。一条坏数据打死整个 worker，等于库里剩下的
   任务永远没人做。异常必须在 `run_once` 内部收掉。
2. **未实现的步骤不能假装做了**。`summarize`/`ocr`/`tag`/`index` 在 R5 之后才有，
   现在领到就标 done 是谎报；正确做法是标回 pending 并写明「尚未实现」。
3. **队列空不空转日志**。每 2 秒写一行「没有任务」会把日志刷满，
   真出问题时翻不到有用信息。
"""

from __future__ import annotations

import pytest

from kb.extract.normalize import ExtractResult, MediaItem
from kb.extract.router import ExtractOutcome
from kb.worker import loop


class _Pool:
    """假连接池。真实 Pool 用 `connection()` 上下文管理器，这里照抄形状。"""

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


def _as_pool(p: _Pool) -> object:
    return p


class _Extractor:
    """假抓取器。按 URL 决定成功或失败，不连网。"""

    def __init__(self, ok: bool = True, raises: Exception | None = None) -> None:
        self.ok = ok
        self.raises = raises
        self.calls: list[str] = []

    async def __call__(self, url: str) -> ExtractOutcome:
        """返回真实契约类型 `ExtractOutcome`，不是 dict。

        之前这里返回 dict，测试绿了但实现要 `.ok` 属性——假实现与真契约不一致
        会让测试证明不了任何事。用真类型，测试才有意义。
        """
        self.calls.append(url)
        if self.raises is not None:
            raise self.raises
        if not self.ok:
            return ExtractOutcome(
                ok=False, route="parsehub", kind="link", error="被风控拦截"
            )
        data = ExtractResult(
            kind="video",
            title="抓到的标题",
            content_text="",
            raw_url=url,
            platform="douyin",
            media=[
                MediaItem(
                    role="video",
                    url="https://example.invalid/v.mp4",
                    ext="mp4",
                    duration_sec=445,
                )
            ],
        )
        return ExtractOutcome(ok=True, route="parsehub", kind="video", data=data)


class _ScriptedConn:
    """按调用序列返回预设结果，记录所有 SQL。"""

    def __init__(
        self,
        claim_rows: list[list[object]],
        item_url: str | None = "https://v.douyin.com/abc/",
    ) -> None:
        self._claims = list(claim_rows)
        self._item_url = item_url
        self.seen: list[str] = []
        self.params: list[dict[str, object]] = []

    def run(self, sql: str, **params: object) -> list[list[object]]:
        text = sql.lower()
        self.seen.append(text)
        self.params.append(params)
        if "interval '15 minutes'" in text:
            return []
        if "with picked" in text:
            return self._claims.pop(0) if self._claims else []
        # jobs.counts：get_item 内部会调它算处理状态
        if "from jobs" in text and "count(*)" in text:
            return [[1, 0, 0, 0]]
        # items 详情：get_item 要 12 列
        if "from items i" in text and "i.id = :i" in text:
            if self._item_url is None:
                return []
            return [
                [
                    1,
                    "link",
                    self._item_url,
                    "标题",
                    None,
                    "v.douyin.com",
                    None,
                    None,
                    "unread",
                    __import__("datetime").datetime(2026, 11, 27),
                    None,
                    "{}",
                ]
            ]
        return []


def _fetch_job(item_url: str = "https://v.douyin.com/abc/") -> list[object]:
    return [[1, 7, "fetch", 0, "running"]]


class _FakeSummarizer:
    """最小假 AI 客户端，只为了断言「有没有被调用」。

    `calls` 是**普通实例属性**而不是 property，也不是 `MagicMock`：
    `MagicMock` 对任何属性访问都返回新 Mock，`calls == 0` 在「从未被调用」
    与「被调用了但没设返回值」两种情况下都能通过（设计 §6.9）。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.seen: list[tuple[str, str, tuple]] = []

    def summarize(
        self, title: str, text: str, vocabulary: tuple[tuple[int, str, str], ...] = ()
    ) -> object:
        self.calls += 1
        self.seen.append((title, text, vocabulary))
        from kb.ai.outcomes import SummarizeOk

        return SummarizeOk(summary="这是一段足够长的中文摘要内容")


# ── 正常路径 ──────────────────────────────────────────────────


def test_处理一条_fetch_任务后_job_标_done():
    conn = _ScriptedConn([_fetch_job()])
    extractor = _Extractor(ok=True)

    handled = loop.run_once(_as_pool(_Pool(conn)), extractor)

    assert handled is True
    joined = " ".join(conn.seen)
    assert "state = 'done'" in joined
    assert extractor.calls, "抓取器没被调用"


def test_抓取成功后调用_apply_extract_写回_items():
    conn = _ScriptedConn([_fetch_job()])

    loop.run_once(_as_pool(_Pool(conn)), _Extractor(ok=True))

    joined = " ".join(conn.seen)
    assert "update items" in joined


def test_队列空时返回_False():
    conn = _ScriptedConn([])

    assert loop.run_once(_as_pool(_Pool(conn)), _Extractor()) is False


def test_队列空时不写任何_job_状态():
    """没领到任务却去标状态，会把别人的任务弄坏。"""
    conn = _ScriptedConn([])

    loop.run_once(_as_pool(_Pool(conn)), _Extractor())

    joined = " ".join(conn.seen)
    assert "state = 'done'" not in joined
    assert "attempts = attempts + 1" not in joined


# ── 失败路径 ──────────────────────────────────────────────────


def test_抓取失败时_job_退回_pending_并记录原因():
    conn = _ScriptedConn([_fetch_job()])
    extractor = _Extractor(ok=False)

    handled = loop.run_once(_as_pool(_Pool(conn)), extractor)

    assert handled is True
    joined = " ".join(conn.seen)
    assert "attempts = attempts + 1" in joined
    # 失败原因要带进 mark_failed 的参数
    errors = [p.get("e") for p in conn.params if "e" in p]
    assert any("被风控拦截" in str(x) for x in errors), f"失败原因没记录：{errors}"


def test_抓取失败时也写回_items_记下原因():
    """条目上要能看到「处理失败」，只记在 jobs 里用户看不到。"""
    conn = _ScriptedConn([_fetch_job()])

    loop.run_once(_as_pool(_Pool(conn)), _Extractor(ok=False))

    joined = " ".join(conn.seen)
    assert "update items" in joined


def test_抓取器抛异常不得让循环崩掉():
    """单条坏数据不能打死整个 worker——记录后继续下一条。"""
    conn = _ScriptedConn([_fetch_job()])

    handled = loop.run_once(
        _as_pool(_Pool(conn)), _Extractor(raises=RuntimeError("网络炸了"))
    )

    assert handled is True, "异常应该被收成失败，而不是抛出去"
    joined = " ".join(conn.seen)
    assert "attempts = attempts + 1" in joined


def test_抓取器抛异常时异常类型要进_error_():
    conn = _ScriptedConn([_fetch_job()])

    loop.run_once(_as_pool(_Pool(conn)), _Extractor(raises=TimeoutError("超时")))

    errors = [str(p.get("e")) for p in conn.params if "e" in p]
    assert any("TimeoutError" in e for e in errors), f"异常类型没记录：{errors}"


# ── 未实现的步骤 ──────────────────────────────────────────────
#
# `summarize` 与 `tag` 已在本轮实现（进 `SUPPORTED_STEPS`），所以这两条用例的
# 前提消失了。改用 `ocr`——它仍是未实现步骤（AI-10 明说不入队这两个步骤）。
#
# 为什么不直接删掉这两条：「未实现步骤不能假装完成」是一条长期有效的约束，
# 下一轮实现 `ocr` 时它还要守门。删掉等于把这条规则从测试里抹去。


def test_未实现的步骤不再假装完成_标回pending并说明():
    """`ocr` / `index` 在后续轮次才有。

    现在领到就标 done 是谎报——条目会显示「已完成」但摘要标签都是空的。
    正确做法：标回 pending + 写明原因，等后续轮次接上。
    """
    conn = _ScriptedConn([[[2, 8, "ocr", 0, "running"]]])

    handled = loop.run_once(_as_pool(_Pool(conn)), _Extractor())

    assert handled is True
    joined = " ".join(conn.seen)
    assert "state = 'done'" not in joined, "未实现的步骤被标成完成了"
    errors = [str(p.get("e")) for p in conn.params if "e" in p]
    assert any("尚未实现" in e for e in errors), f"没说明原因：{errors}"


def test_未实现步骤不去调抓取器也不调AI客户端():
    """`ocr` 阶段既没有 URL 可抓，也不该调 AI 客户端。

    `ai_client.calls == 0` 这条顺带成为「未支持步骤不消耗 AI 调用」的守门用例——
    `ocr` 若哪天被误加进 `SUPPORTED_STEPS` 并把 `ai_client` 传进去，
    这条会立刻红。
    """
    conn = _ScriptedConn([[[2, 8, "ocr", 0, "running"]]])
    extractor = _Extractor()
    ai_client = _FakeSummarizer()

    loop.run_once(_as_pool(_Pool(conn)), extractor, ai_client)

    assert extractor.calls == []
    assert ai_client.calls == 0, "未支持的步骤不得消耗 AI 调用"


def test_summarize步骤不调抓取器_C3():
    """`summarize` 是已实现步骤，**更**不该调抓取器。

    这条比「未实现步骤不调抓取器」更强：未实现步骤只是不该调；
    `summarize` 是**没有 URL 可抓**，调了等于白费一次网络往返。
    这是 AI-11 修复后本轮更需要保留的意图。
    """
    conn = _ScriptedConn([[[2, 8, "summarize", 0, "running"]]])
    extractor = _Extractor()

    loop.run_once(_as_pool(_Pool(conn)), extractor)

    assert extractor.calls == []


def test_summarize步骤不会因为url为空被写成无链接可抓_AI_11():
    """AI-11：URL 判空只对 `fetch` 生效。

    修 AI-11 之前，`summarize` 加进 `SUPPORTED_STEPS` 会让纯文字条目
    命中「无链接可抓」并被标 `done`——摘要没写、状态显示已完成（谎报）。
    """
    conn = _ScriptedConn([[[2, 8, "summarize", 0, "running"]]], item_url=None)

    loop.run_once(_as_pool(_Pool(conn)), _Extractor())

    errors = [str(p.get("e")) for p in conn.params if "e" in p]
    assert not any("无链接可抓" in e for e in errors), f"命中了 URL 判空分支：{errors}"


# ── 连接归还 ──────────────────────────────────────────────────


def test_每条任务都从池里取连接():
    pool = _Pool(_ScriptedConn([_fetch_job()]))

    loop.run_once(_as_pool(pool), _Extractor())

    assert pool.acquired == 1, "没从池里取连接，可能用了全局连接"


# ── 分派阶段的兜底异常 ────────────────────────────────────────


class _BrokenStepConn(_ScriptedConn):
    """让某个步骤的实现抛异常，验证 `run_once` 的兜底。"""

    def run(self, sql: str, **params: object) -> list[list[object]]:
        result = super().run(sql, **params)
        # 走到 mark_done（步骤实现的第一步写库操作之一）时抛
        if "state = 'done'" in sql.lower():
            raise RuntimeError("库挂了")
        return result


def test_步骤实现抛异常时收成任务失败不得让循环崩掉():
    """`steps.*` 内部未捕获的异常必须在 `run_once` 收掉。

    这条守的是「单条坏数据不能打死整个 worker」——与抓取器抛异常那条
    （`test_抓取器抛异常不得让循环崩掉`）同一个理由，只是发生在分派层。
    少了这层兜底，一次 `KeyError` 会让剩余队列永远没人处理。
    """
    conn = _BrokenStepConn([_fetch_job()])

    handled = loop.run_once(_as_pool(_Pool(conn)), _Extractor(ok=True))

    assert handled is True, "异常应该被收成失败，而不是抛出去"
    errors = [str(p.get("e")) for p in conn.params if "e" in p]
    assert any("RuntimeError" in e for e in errors), f"异常类型没记录：{errors}"


# ── run_forever 的循环行为 ────────────────────────────────────


def test_run_forever_把_ai_client_透传给_run_once():
    """`run_forever` 是唯一把 `ai_client` 传到 `run_once` 的地方。

    这条挡住「`__main__` 构造了客户端但忘了透传」——那种情况下
    worker 会一直走「AI 客户端未装配」的推迟分支，而日志里看不出问题。

    探针用 `*args` 收参数而不是抄一份固定签名：`run_once` 后面追加可选的
    `media_stage`（R8）时，这条用例不该因为签名多一个可选项就红
    ——它要验的是「`ai_client` 有没有被透传」，不是「参数列表有几个」。
    """
    seen: list[object] = []
    client = _FakeSummarizer()

    class _Stopping(BaseException):
        """继承 `BaseException` 而不是 `Exception`。

        `run_forever` 用 `except Exception` 兜住 `on_tick` 的失败（心跳写库
        失败不该影响干活），所以用普通 Exception 做「跳出循环」的信号会被
        它吞掉，测试直接挂死。
        """

    def _on_tick() -> None:
        raise _Stopping

    original = loop.run_once

    def _spy(*args: object) -> bool:
        # 第三个位置参数就是 ai_client（§4.2.1 的形状由
        # `tests/test_worker_ai_steps.py` 单独钉住）
        seen.append(args[2] if len(args) > 2 else None)
        return False

    loop.run_once = _spy  # type: ignore[assignment]
    try:
        with pytest.raises(_Stopping):
            loop.run_forever(
                _as_pool(_Pool(_ScriptedConn([]))),
                _Extractor(),
                idle_seconds=0.0,
                on_tick=_on_tick,
                ai_client=client,
            )
    finally:
        loop.run_once = original  # type: ignore[assignment]

    assert seen == [client], f"ai_client 没被透传：{seen}"

    assert seen == [client], f"ai_client 没被透传：{seen}"


def test_run_forever_单轮异常不退出循环():
    """数据库级异常记一笔再继续，不让进程退出（模块 docstring 的约束）。"""
    ticks: list[int] = []

    class _Stopping(BaseException):
        """继承 `BaseException` 而不是 `Exception`。

        `run_forever` 用 `except Exception` 兜住 `on_tick` 的失败（心跳写库
        失败不该影响干活），所以用普通 Exception 做「跳出循环」的信号会被
        它吞掉，测试直接挂死。
        """

    def _on_tick() -> None:
        ticks.append(1)
        if len(ticks) >= 3:
            raise _Stopping

    def _boom(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("库连不上")

    original = loop.run_once
    loop.run_once = _boom  # type: ignore[assignment]
    try:
        with pytest.raises(_Stopping):
            loop.run_forever(
                _as_pool(_Pool(_ScriptedConn([]))),
                _Extractor(),
                idle_seconds=0.0,
                on_tick=_on_tick,
            )
    finally:
        loop.run_once = original  # type: ignore[assignment]

    assert len(ticks) == 3, "单轮异常让循环提前退出了"


def test_run_forever_心跳失败不影响干活():
    """心跳写失败不该让 worker 停止处理任务。"""
    ticks: list[int] = []

    class _Stopping(BaseException):
        """继承 `BaseException` 而不是 `Exception`。

        `run_forever` 用 `except Exception` 兜住 `on_tick` 的失败（心跳写库
        失败不该影响干活），所以用普通 Exception 做「跳出循环」的信号会被
        它吞掉，测试直接挂死。
        """

    def _on_tick() -> None:
        ticks.append(1)
        if len(ticks) >= 2:
            raise _Stopping
        raise RuntimeError("心跳写库失败")

    try:
        with pytest.raises(_Stopping):
            loop.run_forever(
                _as_pool(_Pool(_ScriptedConn([]))),
                _Extractor(),
                idle_seconds=0.0,
                on_tick=_on_tick,
            )
    finally:
        pass

    assert len(ticks) == 2, "心跳失败让循环退出了"

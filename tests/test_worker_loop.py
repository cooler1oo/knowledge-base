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


def test_未实现的步骤不假装完成_标回_pending_并说明():
    """summarize/ocr/tag/index 在 R5 之后才有。

    现在领到就标 done 是谎报——条目会显示「已完成」但摘要标签都是空的。
    正确做法：标回 pending + 写明原因，等后续轮次接上。
    """
    conn = _ScriptedConn([[[2, 8, "summarize", 0, "running"]]])

    handled = loop.run_once(_as_pool(_Pool(conn)), _Extractor())

    assert handled is True
    joined = " ".join(conn.seen)
    assert "state = 'done'" not in joined, "未实现的步骤被标成完成了"
    errors = [str(p.get("e")) for p in conn.params if "e" in p]
    assert any("尚未实现" in e for e in errors), f"没说明原因：{errors}"


def test_未实现步骤不去调抓取器():
    """summarize 阶段没有 URL 可抓，调了只会白费一次网络往返。"""
    conn = _ScriptedConn([[[2, 8, "summarize", 0, "running"]]])
    extractor = _Extractor()

    loop.run_once(_as_pool(_Pool(conn)), extractor)

    assert extractor.calls == []


# ── 连接归还 ──────────────────────────────────────────────────


def test_每条任务都从池里取连接():
    pool = _Pool(_ScriptedConn([_fetch_job()]))

    loop.run_once(_as_pool(pool), _Extractor())

    assert pool.acquired == 1, "没从池里取连接，可能用了全局连接"

"""回收站清扫线程（M4-03 T03）。

## 要守的四条

1. **启动先跑一次**：停机多天后起来不清，用户看到「半年前的东西还在回收站」
2. **分批循环**：一次删几千行把 worker 占住几十秒，整条队列停摆
3. **索引清理失败不中断**：库里的过期行已删，索引残留由 rebuild 兜底
4. **`stop.set()` 能立刻结束**：不能等满一小时

## 时间相关的部分不在这里验

`purge_expired` 用 SQL 的 `now() - make_interval(days => ...)` 算保留期，
那条的正确性在真库用例（`tests/test_trash_e2e_pg.py`）里验。本文件用假连接，
验的是「调用顺序、分批、异常处置」。
"""

from __future__ import annotations

import threading

import pytest

from kb.worker import purge


class FakePool:
    """每次 `connection()` 给同一个假连接。

    `purge_expired` 的返回值由 `batches` 驱动：第一轮返回第一批，第二轮返回
    第二批……用完后返回空列表（表示删完了）。
    """

    def __init__(self, batches: list[list[int]]) -> None:
        self._batches = list(batches)
        self.opened = 0

    def connection(self) -> object:
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            self.opened += 1
            yield self

        return _cm()


class FakeIndexer:
    def __init__(self, fail_with: BaseException | None = None) -> None:
        self.deleted: list[list[int]] = []
        self.fail_with = fail_with

    def delete_ids(self, ids: list[int]) -> int:
        if self.fail_with is not None:
            raise self.fail_with
        self.deleted.append(list(ids))
        return len(ids)


@pytest.fixture
def fake_expired(monkeypatch):
    """把 `items.purge_expired` 替换成按 `batches` 依次返回的假实现。"""

    def _install(batches: list[list[int]]) -> dict[str, object]:
        calls: dict[str, object] = {"batches": list(batches), "seen": []}

        def fake(conn, **kwargs):
            calls["seen"].append(dict(kwargs))  # type: ignore[union-attr]
            batch = calls["batches"]  # type: ignore[index]
            if isinstance(batch, list) and batch:
                return batch.pop(0)
            return []

        monkeypatch.setattr(purge.items_store, "purge_expired", fake)
        return calls

    return _install


# ── purge_once ──────────────────────────────────────────────


def test_删完一批就停(monkeypatch, fake_expired) -> None:
    """返回的条数小于 `limit` → 说明删完了，不再开连接。"""
    calls = fake_expired([[1, 2, 3]])
    pool = FakePool([])

    removed = purge.purge_once(pool, limit=200)  # type: ignore[arg-type]

    assert removed == [1, 2, 3]
    assert pool.opened == 1, "删完就该停，不该再查一次"
    assert len(calls["seen"]) == 1  # type: ignore[arg-type]


def test_满一批就继续下一批(monkeypatch, fake_expired) -> None:
    """**分批不能省**：一次删几千行会把连接占住几十秒，队列停摆。

    返回条数 == limit 说明还有积压，继续。
    """
    fake_expired([[1, 2], [3, 4], [5]])
    pool = FakePool([])

    removed = purge.purge_once(pool, limit=2)  # type: ignore[arg-type]

    assert removed == [1, 2, 3, 4, 5]
    assert pool.opened == 3, "三批三连接"


def test_空回收站不多开连接(monkeypatch, fake_expired) -> None:
    fake_expired([])
    pool = FakePool([])

    assert purge.purge_once(pool) == []  # type: ignore[arg-type]
    assert pool.opened == 1, "没有可删的也只需查一次"


def test_默认保留期来自items常量(monkeypatch, fake_expired) -> None:
    """**只有一处字面量**：`TRASH_RETENTION_DAYS` 在 `items.py`，这里 import。

    两处各写 30 的话，改了其中一处就会出现「worker 按 30 天清、界面显示
    按 60 天算」这种没人会核对的不一致。
    """
    from kb.store import items as items_store

    calls = fake_expired([])
    purge.purge_once(FakePool([]))  # type: ignore[arg-type]

    seen = calls["seen"]
    assert seen[0]["older_than_days"] == items_store.TRASH_RETENTION_DAYS  # type: ignore[index]


def test_显式传保留期时用它(monkeypatch, fake_expired) -> None:
    calls = fake_expired([])
    purge.purge_once(FakePool([]), older_than_days=7)  # type: ignore[arg-type]

    assert calls["seen"][0]["older_than_days"] == 7  # type: ignore[index]


# ── 索引清理 ────────────────────────────────────────────────


def test_删完顺手清索引(monkeypatch, fake_expired) -> None:
    """覆盖「软删时索引删除永久失败」的残留（设计 §4.2 第 2 条路径）。

    ⚠️ `limit=2` 是必须的：只有「本批 == limit」才继续下一批。
    默认 limit=200 时，返回 `[1,2]` 已经 < 200，循环一轮就结束——
    那样这条用例验不到「多批都清索引」。
    """
    fake_expired([[1, 2], [3]])
    indexer = FakeIndexer()

    purge.purge_once(FakePool([]), indexer=indexer, limit=2)  # type: ignore[arg-type]

    assert indexer.deleted == [[1, 2], [3]]


def test_索引清理失败不中断清扫(monkeypatch, fake_expired) -> None:
    """索引失败只记日志，**库里的过期行照删**。

    反过来（失败就中断）的后果：Meili 长期挂着时，回收站里过期的内容
    永远删不掉——而用户看得见的是「回收站涨个不停」。
    索引残留由 `rebuild` 的差集兜底。
    """
    fake_expired([[1, 2], [3]])
    indexer = FakeIndexer(fail_with=RuntimeError("Meili 挂了"))

    removed = purge.purge_once(FakePool([]), indexer=indexer, limit=2)  # type: ignore[arg-type]

    assert removed == [1, 2, 3], "索引失败不该让清扫停在这里"


def test_indexer为None时只删库不报错(monkeypatch, fake_expired) -> None:
    """`indexer is None` 意味着装配漏传。

    与 `run_index` 的处理不同（那里 defer）：清扫是**兜底**机制，晚一轮清索引
    没有后果；而**不删库**会让过期的内容永远留在回收站里，那才是用户看得见的。
    """
    fake_expired([[1, 2]])

    removed = purge.purge_once(FakePool([]), indexer=None)  # type: ignore[arg-type]
    assert removed == [1, 2]


# ── purge_loop ─────────────────────────────────────────────


def test_启动时先跑一次再等节拍(monkeypatch) -> None:
    """**这条是本节最重要的**（设计 §1.3 的明确要求）。

    停机多天再起来，若只靠节拍要等一小时才开始清。那一小时里运维看日志
    会以为清扫没生效，而实际上它只是还没开始。

    做法：`stop` 一开始就 set，那 `stop.wait()` 立刻返回 True 退出循环——
    这样「跑了几次」就能精确断言：若启动时不跑，这里会是 0 次。
    """
    ran: list[int] = []
    monkeypatch.setattr(purge, "purge_once", lambda *a, **k: ran.append(1) or [])

    stop = threading.Event()
    stop.set()  # 立刻要求退出
    purge.purge_loop(FakePool([]), stop, interval_seconds=3600)  # type: ignore[arg-type]

    assert len(ran) == 1, f"启动时该跑一次，实际跑了 {len(ran)} 次"


def test_stop_set后立刻结束不等满间隔(monkeypatch) -> None:
    """`stop.wait(3600)` 在 stop 已 set 时立刻返回——不会真的等一小时。

    这条与上一条一起守住「退出要及时」：worker 收到 SIGTERM 后不该卡在
    一个小时的 sleep 里。
    """
    monkeypatch.setattr(purge, "purge_once", lambda *a, **k: [])

    stop = threading.Event()
    stop.set()
    # 若实现用 `time.sleep(3600)` 而不是 `stop.wait()`，这条会挂住 3600 秒
    purge.purge_loop(FakePool([]), stop, interval_seconds=3600)  # type: ignore[arg-type]


def test_清扫抛异常不退出循环(monkeypatch) -> None:
    """清扫失败不该让 worker 退出（与心跳同一立场）。

    做法：第一次抛，第二次正常；`stop` 在第二次之后才 set。
    若实现让异常穿出去，`purge_loop` 会抛且调用次数是 1。
    """
    calls: list[int] = []
    stop = threading.Event()

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("数据库瞬断")
        stop.set()  # 第二次成功后要求退出
        return []

    monkeypatch.setattr(purge, "purge_once", flaky)
    purge.purge_loop(FakePool([]), stop, interval_seconds=0.01)  # type: ignore[arg-type]

    assert len(calls) == 2, f"异常后应继续下一轮，实际调了 {len(calls)} 次"


def test_purge_once返回本轮删掉的全部id(monkeypatch, fake_expired) -> None:
    """多批时返回值要累积，不是最后一批。

    调用方据此打日志「清理了 N 条」——只回最后一批的话，
    日志会永远显示「清理了 1 条」而实际删了几百条。
    """
    fake_expired([[1], [2], [3]])
    removed = purge.purge_once(FakePool([]), limit=1)  # type: ignore[arg-type]

    assert removed == [1, 2, 3]


# ── 常量 ────────────────────────────────────────────────────


def test_节拍是一小时() -> None:
    """3600 秒。更密只是在空跑 SQL——保留期 30 天，晚一小时清掉没有用户可见区别。"""
    assert purge.PURGE_INTERVAL_SECONDS == 3600


def test_批量上限是200() -> None:
    """几千行一次删会把 worker 的连接占住几十秒，那期间主循环领不到任务。"""
    assert purge.PURGE_BATCH_SIZE == 200


# ── 装配（源码守卫）────────────────────────────────────────
#
# `purge_loop` 要被 `__main__.py` 真正启动才算数。用例验不了「线程真的跑了」
# （那要起完整进程），只能读源码断言「那两行在」——与 T01/T02 的守卫同一手法。


def test_清扫线程在worker启动时被拉起来() -> None:
    """`__main__.py` 必须 import `purge_loop` 并起一个线程。

    只写 `kb/worker/purge.py` 而不接进启动流程的话，那 40 行代码是死代码，
    回收站永远不会自动清理——而这种「模块在、没人调」的缺口，用例全绿也发现不了。
    """
    import inspect

    from kb.worker import __main__ as worker_main

    src = inspect.getsource(worker_main)
    assert "from .purge import purge_loop" in src, "没 import purge_loop"
    assert "purge_loop" in src and "Thread(" in src, "没起清扫线程"
    assert "daemon=True" in src, "线程要 daemon，否则 SIGTERM 时进程退不掉"

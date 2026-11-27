"""worker 的任务领取与状态流转（纯 SQL 层，不连库）。

为什么不连库：这些函数的正确性全在 SQL 谓词里（`SKIP LOCKED`、`run_after <= now()`、
`attempts + 1 >= 3`），用假连接断言「SQL 里有这个谓词」比连真库跑更直接，
而且能毫秒级跑完。真库行为由 `tests/test_worker_pg.py` 另验。

假连接按 SQL 特征分派返回值，与 `test_items_core.py` 同一风格。
"""

from __future__ import annotations

import pg8000.native

from kb.store import jobs


class _Script:
    """按需注入的返回值。哪个用例要什么就改什么。"""

    def __init__(self) -> None:
        # claim_one 的 returning 是 5 列：id, item_id, step, attempts, state
        self.claim: list[list[object]] = []
        self.failed_state: list[list[object]] = [["pending"]]
        self.recovered: list[list[object]] = []
        # 刚标 running 的任务不在恢复范围内（体现为恢复语句返回空）
        self.recover_allowed = True


class _Conn:
    def __init__(self, script: _Script) -> None:
        self._script = script
        self.seen: list[str] = []
        self.params: list[dict[str, object]] = []

    def run(self, sql: str, **params: object) -> list[list[object]]:
        text = sql.lower()
        self.seen.append(text)
        self.params.append(params)
        # 顺序要紧：恢复语句里同时含 "state = 'pending'" 与 "state = 'running'"，
        # 必须先判它，否则会被失败重试的分支误接
        if "interval '15 minutes'" in text:
            return self._script.recovered
        if "with picked" in text:
            return self._script.claim
        if "attempts = attempts + 1" in text:
            return self._script.failed_state
        return []


def _conn(script: _Script | None = None) -> _Conn:
    return _Conn(script or _Script())


def _as_conn(c: _Conn) -> pg8000.native.Connection:
    """假连接转成类型标注要求的 Connection。测试专用。"""
    return c  # type: ignore[return-value]


# ── 取任务 ────────────────────────────────────────────────────


def test_取任务把状态从_pending_改成_running():
    script = _Script()
    script.claim = [[42, 7, "fetch", 0, "running"]]
    conn = _conn(script)

    job = jobs.claim_one(_as_conn(conn))

    assert job is not None
    assert job["id"] == 42
    assert job["itemId"] == 7
    assert job["step"] == "fetch"
    assert job["attempts"] == 0
    assert job["state"] == "running"


def test_没有待办任务时返回_None():
    conn = _conn()
    assert jobs.claim_one(_as_conn(conn)) is None


def test_取任务必须用_SKIP_LOCKED_防两个_worker_抢同一条():
    """不用 SKIP LOCKED 时两个 worker 会同时拿到同一条，同一条被处理两遍。"""
    conn = _conn()
    jobs.claim_one(_as_conn(conn))

    sql = conn.seen[0]
    assert "for update skip locked" in sql, "缺少 SKIP LOCKED，两个 worker 会抢同一条"


def test_取任务只取_run_after_已到的():
    """run_after 在未来的任务不该被取走，否则失败退避形同虚设。"""
    conn = _conn()
    jobs.claim_one(_as_conn(conn))

    assert "run_after <= now()" in conn.seen[0]


def test_取任务按_run_after_排序_先到期的先做():
    conn = _conn()
    jobs.claim_one(_as_conn(conn))

    assert "order by run_after" in conn.seen[0]


# ── 标完成 ────────────────────────────────────────────────────


def test_标完成把状态改成_done():
    conn = _conn()
    jobs.mark_done(_as_conn(conn), job_id=42)

    sql = conn.seen[0]
    assert "state = 'done'" in sql
    assert conn.params[0]["j"] == 42


# ── 标失败：指数退避 ──────────────────────────────────────────


def test_第一次失败退回_pending():
    """attempts 从 0 到 1，还会重试。"""
    script = _Script()
    script.failed_state = [["pending"]]
    conn = _conn(script)

    state = jobs.mark_failed(_as_conn(conn), job_id=1, error="被风控拦截")

    assert state == "pending"


def test_第三次失败标_failed_不再重试():
    """attempts 到 3 必须停手，否则坏任务无限重试打爆上游。"""
    script = _Script()
    script.failed_state = [["failed"]]
    conn = _conn(script)

    state = jobs.mark_failed(_as_conn(conn), job_id=1, error="x")

    assert state == "failed"
    assert "attempts + 1 >= 3" in conn.seen[0]


def test_失败按指数退避_推迟_run_after():
    """1 分钟 × 2^attempts。写成固定间隔会让上游被打爆或被饿死。"""
    conn = _conn()
    jobs.mark_failed(_as_conn(conn), job_id=1, error="x")

    sql = conn.seen[0]
    assert "power(2, attempts)" in sql
    assert "interval '1 minute'" in sql


def test_失败原因写进_error_列():
    """界面靠它显示「处理失败，可重试」，空着等于没记。"""
    conn = _conn()
    jobs.mark_failed(_as_conn(conn), job_id=1, error="FallbackError: 被风控拦截")

    assert conn.params[0]["e"] == "FallbackError: 被风控拦截"
    assert "error = :e" in conn.seen[0]


def test_标失败用_returning_拿新状态_不在_Python_里重算():
    """SQL 里已经算过一次 attempts+1，Python 再算一次必然有分歧的那天。"""
    conn = _conn()
    jobs.mark_failed(_as_conn(conn), job_id=1, error="x")

    assert "returning state" in conn.seen[0]


# ── 恢复卡死 ──────────────────────────────────────────────────


def test_超过_15_分钟的_running_回退成_pending():
    script = _Script()
    script.recovered = [[1]]
    conn = _conn(script)

    n = jobs.recover_stale(_as_conn(conn))

    assert n == 1
    sql = conn.seen[0]
    assert "state = 'running'" in sql
    assert "interval '15 minutes'" in sql


def test_刚刚_running_的不动():
    """worker 正常在跑时不能被别的 worker 抢走。"""
    conn = _conn()
    assert jobs.recover_stale(_as_conn(conn)) == 0


def test_恢复用_returning_数条数_不额外查一次():
    conn = _conn()
    jobs.recover_stale(_as_conn(conn))

    assert "returning id" in conn.seen[0]

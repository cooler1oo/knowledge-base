"""`jobs` 的四个新函数：`defer` / `defer_until` / `fail_hard` / `mark_failed_delayed`。

**为什么是三个新函数而不是给 `mark_failed` 加开关**（设计 §6.6）：
`mark_failed` 的 SQL 里 `attempts = attempts + 1`、`state = case when attempts + 1 >= 3`、
`run_after = now() + 2^attempts` **三处都依赖「这次算一次失败」**。加一个
`count_attempt: bool = True` 等于在一条 SQL 里塞两套互斥分支，而且默认值
`True` 意味着「忘记传」时就静默消耗一次尝试——`E1` 的断言会变成靠调用方自觉。

所以 `defer` 的 SQL 里**根本不出现 `attempts`**。这是「推迟不计次」在代码层面
可被一眼确认的证据，也是下面 `test_defer_的SQL里不出现attempts` 这条用例守的东西。

**分两层测**：SQL 形状用假连接（本机能跑，覆盖率高），真库行为用
`@pytest.mark.pg`（本机无库时跳过）。只测真库的话，本机 26 个 pg 用例全跳过，
新增代码行覆盖率会掉到 0；只测假连接的话，「推迟后 `attempts` 真的没变」
这条断言没有库来证明。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kb.store import jobs as jobs_store
from kb.store import usage as usage_store


class _Recorder:
    """假连接：记下每条 SQL 与参数，按需返回预设行。"""

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


# ── defer：推迟不计次（E1 / E3）────────────────────────────────


def test_defer_把state设回pending并写run_after_E1():
    conn = _Recorder()
    jobs_store.defer(conn, 7, 1800, "已推迟：AI 未配置（缺 ai_api_key）")

    sql = _joined(conn)
    assert "state = 'pending'" in sql
    assert "run_after" in sql
    assert "error = :e" in sql or "error=" in sql.replace(" ", "")


def test_defer_的SQL里不出现attempts_E1():
    """这是「推迟不消耗 attempts」的唯一可机器验证的证据。

    写 `attempts = attempts`（看似无害的等价写法）也会让这条红——
    `tests/test_worker_loop.py:162` 那条「队列空时不写任何 job 状态」用的
    正是 `"attempts = attempts + 1" not in joined`，两者互为守门。
    """
    conn = _Recorder()
    jobs_store.defer(conn, 7, 1800, "已推迟：AI 未配置")
    assert "attempts" not in _joined(conn), "defer 的 SQL 里出现 attempts 就整条作废"


def test_defer_用秒数参数不用分钟字面量_避开recover_stale的判据():
    """`interval '1 second' * :seconds` 而不是 `interval 'N minutes'`。

    现有假连接 `_ScriptedConn` 靠 `interval '15 minutes'` 识别 `recover_stale`。
    `defer` 若也写 `interval 'N minutes'`，两条语句在假连接眼里会撞成一条。
    """
    conn = _Recorder()
    jobs_store.defer(conn, 7, 1800, "已推迟")
    sql = _joined(conn)
    assert "interval '1 second' * :seconds" in sql
    assert "minutes" not in sql
    assert conn.params[0]["seconds"] == 1800


def test_defer_的reason进error参数():
    conn = _Recorder()
    jobs_store.defer(conn, 7, 1800, "已推迟：预算用完")
    assert conn.params[0]["e"] == "已推迟：预算用完"


# ── defer_until：推到下月 1 日 00:05（D3）──────────────────────


def test_defer_until_把run_after设成给定时点_D3():
    conn = _Recorder()
    when = datetime(2026, 12, 1, 0, 5, tzinfo=timezone.utc)
    jobs_store.defer_until(conn, 7, when, "已推迟：本月额度已用完")

    sql = _joined(conn)
    assert "run_after = :when" in sql
    assert "attempts" not in sql
    assert conn.params[0]["when"] == when


def test_defer_until_也是pending且不写attempts_D3():
    conn = _Recorder()
    jobs_store.defer_until(conn, 7, datetime(2026, 12, 1), "已推迟")
    assert "state = 'pending'" in _joined(conn)


# ── fail_hard：一次判死（B26）──────────────────────────────────


def test_fail_hard_一次就failed_B26():
    """B26：401/403 属于不可自愈错误。重试 3 次只是白等 3 分钟，
    且用户看不到真因（余额不足 vs 密钥过期）。"""
    conn = _Recorder()
    jobs_store.fail_hard(conn, 7, "AI鉴权失败：HTTP 401；检查 api_key")

    sql = _joined(conn)
    assert "state = 'failed'" in sql
    # 用 case 或直接常量都行；不允许出现「>= 3 才 failed」的形态
    assert ">= 3" not in sql, "fail_hard 不该走「满 3 次才判死」的逻辑"
    assert "attempts = attempts + 1" in sql, "语义是「算一次并直接判死」"


def test_fail_hard_不改run_after_B26():
    conn = _Recorder()
    jobs_store.fail_hard(conn, 7, "x")
    assert "run_after" not in _joined(conn).replace("run_after = run_after", "")


# ── mark_failed_delayed：只换退避间隔（B22 / B25）──────────────


def test_mark_failed_delayed_复用指数退避的算法规避两套并存():
    """`attempts+1` 与 `>= 3 判死` 的形状与 `mark_failed` 完全一致，
    只把 `run_after` 的间隔换成入参。两套退避算法并存是最容易出分歧的地方。
    """
    conn = _Recorder(rows=[["pending"]])
    state = jobs_store.mark_failed_delayed(conn, 7, "AI连接失败", 180)

    sql = _joined(conn)
    assert "attempts = attempts + 1" in sql
    assert "attempts + 1 >= 3" in sql
    assert "interval '1 second' * :seconds" in sql
    assert conn.params[0]["seconds"] == 180
    assert state == "pending"


def test_mark_failed_delayed_第三次返回failed():
    conn = _Recorder(rows=[["failed"]])
    assert jobs_store.mark_failed_delayed(conn, 7, "x", 60) == "failed"


def test_mark_failed_delayed_无返回行时按pending兜底():
    """pg8000 在极少数情况下（行被并发删掉）可能返回空。不能让调用方拿到 None。"""
    conn = _Recorder(rows=[])
    assert jobs_store.mark_failed_delayed(conn, 7, "x", 60) == "pending"


# ── 与既有函数的一致性 ─────────────────────────────────────────


def test_mark_failed_delayed_与mark_failed_的set子句形状一致():
    """两条 SQL 的 `set` 部分（除 `run_after`）必须逐字相同。

    这条挡住「有人改了 `mark_failed` 的退避算法却忘了同步延迟版」。
    """
    plain = _Recorder()
    jobs_store.mark_failed(plain, 7, "x")
    delayed = _Recorder()
    jobs_store.mark_failed_delayed(delayed, 7, "x", 60)

    def _normalized(conn: _Recorder) -> str:
        sql = _joined(conn)
        # `mark_failed` 的间隔带一层括号（`now() + (interval ... * power(...))`），
        # 延迟版没有。归一化时把这层差异一起去掉，只比语义部分。
        sql = sql.replace("interval '1 minute' * power(2, attempts)", "INTERVAL")
        sql = sql.replace("interval '1 second' * :seconds", "INTERVAL")
        sql = sql.replace("(INTERVAL)", "INTERVAL")
        return sql

    assert _normalized(plain) == _normalized(delayed)


def test_recover_stale_仍然只认15分钟():
    """新增 defer 之后，`recover_stale` 的判据不能被连带改掉——
    它是「worker 被 kill 后任务不永远卡在 running」的唯一保障。
    """
    conn = _Recorder()
    jobs_store.recover_stale(conn)
    assert "interval '15 minutes'" in _joined(conn)


# ── 真库行为（本机无库时跳过）──────────────────────────────────


@pytest.fixture
def pg_conn(pg_cfg):
    """每个用例一个事务，结束回滚。真库行为的用例都用它。"""
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
    """造一条条目 + 一条 job，返回 (item_id, job_id)。"""
    item_id = pg_conn.run(
        "insert into items (kind, title) values ('text', 'x') returning id"
    )[0][0]
    job_id = pg_conn.run(
        "insert into jobs (item_id, step, state) values (:i, 'summarize', 'running')"
        " returning id",
        i=item_id,
    )[0][0]
    return int(str(item_id)), int(str(job_id))


def _job_row(conn, job_id: int) -> dict[str, object]:
    row = conn.run(
        "select state, attempts, run_after, error from jobs where id = :j", j=job_id
    )[0]
    return {
        "state": str(row[0]),
        "attempts": int(row[1]),
        "run_after": row[2],
        "error": None if row[3] is None else str(row[3]),
    }


@pytest.mark.pg
def test_defer_推迟后state为pending且attempts不变_E1(pg_conn, pg_item):
    """E1：`attempts=0`、`run_after ≥ now + 29 分钟`、`error` 含「已推迟：」。"""
    _item_id, job_id = pg_item
    jobs_store.defer(pg_conn, job_id, 1800, "已推迟：AI 未配置（缺 ai_api_key）")

    row = _job_row(pg_conn, job_id)
    assert row["state"] == "pending"
    assert row["attempts"] == 0, "推迟不得消耗一次尝试"
    assert "已推迟：" in str(row["error"])


@pytest.mark.pg
def test_defer_的run_after至少29分钟后_E1(pg_conn, pg_item):
    _item_id, job_id = pg_item
    jobs_store.defer(pg_conn, job_id, 1800, "已推迟")
    run_after = _job_row(pg_conn, job_id)["run_after"]
    assert isinstance(run_after, datetime)
    delta = run_after - datetime.now(timezone.utc)
    assert delta >= timedelta(minutes=29), f"推迟太短：{delta}"


@pytest.mark.pg
def test_连续三次defer任务仍是pending且attempts仍为0_E3(pg_conn, pg_item):
    """E3：未配置时有 5 条待摘要，全部保持 pending，**没有任何任务变 failed**。

    连推三次是为了证明「反复推迟不会累积成一次失败」——用 `mark_failed`
    做这件事的话，第三次就 `failed` 了。
    """
    _item_id, job_id = pg_item
    for _ in range(3):
        jobs_store.defer(pg_conn, job_id, 1800, "已推迟：AI 未配置")

    row = _job_row(pg_conn, job_id)
    assert row["state"] == "pending"
    assert row["attempts"] == 0


@pytest.mark.pg
def test_defer_until_把run_after设到下月1日0点5分_D3(pg_conn, pg_item):
    """D3：`run_after` 设成「下月 1 日 00:05」（**数据库时区**的口径）。

    ⚠️ **这条改过两次，都是我的断言写错，不是 `defer_until` 的问题。**

    **错误一版**：断言 `.hour <= 0 and .minute <= 10`。`pg8000` 读回
    `timestamptz` 时返回**会话时区**的 datetime。平板会话时区是 PRC（UTC+8），
    传入 UTC 00:05 被渲染成 `08:05+08`，`.hour` 就是 8 → 红。

    **错误二版**：改成 `at time zone current_setting('TimeZone')` 再断言
    `(0, 5)`，仍得 08:05。因为我传进去的还是 **UTC 00:05** —— 它在 PRC 的
    本地钟点就是 08:05。断言本身错。

    **错误三版**：用 `datetime.now().astimezone()` 走生产函数。但平板容器
    时区是 **UTC**，而数据库是 **PRC**，于是算出的是「UTC 视角的下月 1 日
    00:05」= PRC 的 1 日 08:05 → 还是红。

    这一版终于对：**`when` 必须按数据库时区算**，因为业务口径「下月 1 日
    0 点后重试」说的是数据库眼里的一天。生产路径 `loop._db_now(conn)` 正是
    从库里取 `now()`，本用例照同一条路走。

    这也是那 8 小时预算失效窗口的同一个根因（见
    `test_usage_store.py::test_月份口径必须由数据库提供_不能靠进程本地时钟`）。
    """
    _item_id, job_id = pg_item
    # 走生产口径：数据库的 now() → 下月 1 日 00:05（数据库时区）
    db_now = pg_conn.run("select now()")[0][0]
    assert isinstance(db_now, datetime)
    when = usage_store.next_month_start(db_now)

    jobs_store.defer_until(pg_conn, job_id, when, "已推迟：本月额度已用完")

    row = _job_row(pg_conn, job_id)
    assert row["state"] == "pending"
    assert row["attempts"] == 0
    run_after = row["run_after"]
    assert isinstance(run_after, datetime)

    # ① 绝对时刻逐字相等 —— defer_until 的真正契约，与时区无关
    assert run_after == when, f"run_after 不是传入的时刻：{run_after!r} != {when!r}"

    # ② 按数据库时区渲染：下月 1 日 00:05
    local = pg_conn.run(
        "select (run_after at time zone current_setting('TimeZone'))"
        " from jobs where id = :j",
        j=job_id,
    )[0][0]
    assert isinstance(local, datetime)
    assert local.day == 1, f"不是 1 号：{local!r}"
    assert (local.hour, local.minute) == (0, 5), f"不是 00:05：{local!r}"
    assert (local.month, local.year) == (when.month, when.year)


@pytest.mark.pg
def test_defer_until_真库run_after与传入时刻的差值小于1秒_D3(pg_conn, pg_item):
    """D3 的补充：跨时区也成立的「就是那一时刻」判据。

    用 `now()` 的日历算出下月 1 日，再断言差值在 1 秒内。
    这条不依赖任何具体时区的钟点数字，因此在 UTC 与 UTC+8 上都对。
    """
    _item_id, job_id = pg_item
    row = pg_conn.run(
        "select date_trunc('month', now()) + interval '1 month' + interval '5 minutes'"
    )[0]
    when = row[0]
    assert isinstance(when, datetime)

    jobs_store.defer_until(pg_conn, job_id, when, "已推迟：本月额度已用完")

    run_after = _job_row(pg_conn, job_id)["run_after"]
    assert isinstance(run_after, datetime)
    assert abs((run_after - when).total_seconds()) < 1.0


@pytest.mark.pg
def test_fail_hard_一次就failed且attempts为1_B26(pg_conn, pg_item):
    _item_id, job_id = pg_item
    jobs_store.fail_hard(pg_conn, job_id, "AI鉴权失败：HTTP 401；检查 api_key")

    row = _job_row(pg_conn, job_id)
    assert row["state"] == "failed", "401 必须第一次就判死"
    assert row["attempts"] == 1, "算一次失败，但不是靠重试攒到 3 次"
    assert "api_key" in str(row["error"])


@pytest.mark.pg
def test_mark_failed_delayed_按给定秒数退避_B22(pg_conn, pg_item):
    """B22：连接失败退避 3 分钟（不是 1 分钟）。"""
    _item_id, job_id = pg_item
    state = jobs_store.mark_failed_delayed(pg_conn, job_id, "AI连接失败", 180)

    assert state == "pending"
    row = _job_row(pg_conn, job_id)
    assert row["attempts"] == 1
    delta = row["run_after"] - datetime.now(timezone.utc)
    assert delta >= timedelta(minutes=2), f"退避不到 3 分钟：{delta}"
    assert delta <= timedelta(minutes=5), f"退避远超 3 分钟：{delta}"


@pytest.mark.pg
def test_mark_failed_delayed_三次后failed_B22(pg_conn, pg_item):
    _item_id, job_id = pg_item
    jobs_store.mark_failed_delayed(pg_conn, job_id, "x", 1)
    jobs_store.mark_failed_delayed(pg_conn, job_id, "x", 1)
    state = jobs_store.mark_failed_delayed(pg_conn, job_id, "x", 1)

    row = _job_row(pg_conn, job_id)
    assert state == "failed"
    assert row["state"] == "failed"
    assert row["attempts"] == 3

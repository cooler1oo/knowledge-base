"""`usage`：月度用量与预算口径。

**这个文件的每一条断言都围绕同一件事：`add()` 必须是**一条** upsert。**

`kb/store/` 走 `pg8000.native`，这条路径**不做事务管理**（`native.py` 与
`core.py` 里 `begin`/`commit`/`rollback`/`autocommit` 均无出现，
`CoreConnection.__init__` 也不接受 `autocommit` 参数），而现有
`settings.set_value` 是 `value = excluded.value`（**整值覆盖**）。

两者叠加的后果：两个 worker 并发记账时，后写的整块覆盖先写的 → 用量少记
→ 预算闸门以为还有额度 → 花超了才发现。**这是「意外账单」的直接成因。**

所以 Python 侧绝不能 `read → 加 → set_value`。下面 `test_连续两次累加后calls精确等于2`
断言的是**精确等值**而不是 `> 0`：`> 0` 在「丢了一次更新」时同样通过，
等于把最可能的失败模式放过去了。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kb.store import usage as usage_store


class _Recorder:
    """假连接：记下 SQL 与参数，按调用序返回预置行。

    两种模式：
    - `rows=[...]`（默认）：**每次调用都返回同一份**。适合「一条 SQL 就结束」
      的正常路径。
    - `script=[[...], [...], ...]`：**按调用序弹出一份，弹完返回空**。
      适合多步路径（如 `add` 的兜底读），并让「多出来的调用」暴露成空结果
      而不是静默重复同一份 —— 这是区分的意义。

    为什么必须区分：`test_add只有一条SQL` 断言 `len(seen) == 1`，而 `add()`
    在 `returning` 为空时会兜底再发一条 `read`。用同一个非空 `_rows` 时，
    「兜底有没有被触发」在 `seen` 的长度上才看得出来；若假连接允许多次返回
    非空，就无法区分「驱动异常」与「正常一次」。
    """

    def __init__(
        self,
        rows: list[list[object]] | None = None,
        *,
        script: list[list[list[object]]] | None = None,
    ) -> None:
        self.seen: list[str] = []
        self.params: list[dict[str, object]] = []
        self._script = list(script) if script is not None else None
        self._rows = rows or []

    def run(self, sql: str, **params: object) -> list[list[object]]:
        self.seen.append(" ".join(sql.split()))
        self.params.append(params)
        if self._script is not None:
            # 弹完返回空：多余的调用不会被静默满足，会在断言里暴露
            return self._script.pop(0) if self._script else []
        return self._rows


def _joined(conn: _Recorder) -> str:
    return " | ".join(conn.seen).lower()


# ── month_key / next_month_start（D3 / D12）────────────────────


def test_month_key格式是_YYYY_MM_8节1条():
    assert usage_store.month_key(datetime(2026, 11, 27, 14, 32)) == "2026-11"
    assert usage_store.month_key(datetime(2026, 1, 5)) == "2026-01"


def test_next_month_start是下月1日0点5分_D3():
    """D3：`run_after` 为下月 1 日 00:00–00:10。取 00:05 留 5 分钟余量，
    避开「月初第一秒的时钟偏差」这类问题。
    """
    result = usage_store.next_month_start(datetime(2026, 11, 27, 14, 32))
    assert (result.year, result.month, result.day) == (2026, 12, 1)
    assert (result.hour, result.minute) == (0, 5)


def test_next_month_start在12月时跨年_D12():
    """D12：进入下月意味着换一个新的 `ai_usage_YYYY_MM` 键。12 月跨到次年 1 月，
    写错年份会让任务被推到 2027 年 12 月，一年后才重试。
    """
    result = usage_store.next_month_start(datetime(2026, 12, 15, 9, 0))
    assert (result.year, result.month, result.day) == (2027, 1, 1)


def test_month_key与next_month_start取本地时区_8点5节():
    """`month_key()` 与 `next_month_start()` 都取**传入 `now` 携带的时区**。

    注意：代码里不读 `TZ` 环境变量（`E6` 禁止 `kb/ai` 碰 `os`，而这里读它
    也不该——部署侧设 `TZ` 是部署检查的事）。函数接受传入的 `now`，
    naive 就按 naive 处理，aware 就保持时区。

    ⚠️ 这两个函数本身时区无关（只做日历算术），**真正的风险在调用方**：
    见 `test_月份口径必须由数据库提供_不能靠进程本地时钟`。
    """
    aware = datetime(2026, 11, 30, 23, 59, tzinfo=timezone(timedelta(hours=8)))
    assert usage_store.month_key(aware) == "2026-11"
    assert usage_store.next_month_start(aware).month == 12


def test_月份口径必须由数据库提供_不能靠进程本地时钟():
    """**平板真库实测到的最隐蔽的一个 bug 的守门用例。**

    事实（2026-11-27 在平板上实测）：

    ```
    Python now().astimezone() = 2026-11-27 12:30:45+00:00   ← 容器时区 UTC
    DB TimeZone               = PRC
    DB now()                  = 2026-11-27 20:30:45+08:00   ← UTC+8
    ```

    危害：`month_key(now)` 决定闸门读哪个 `ai_usage_YYYY_MM` 键，而
    `usage.add` 的写入发生在数据库侧。用进程本地时间算键，在**每月 1 日
    00:00–08:00（PRC）** 这 8 小时窗口里两侧月份不同：

    - 闸门读 `ai_usage_2026-11`（UTC 还是 11 月）
    - 记账写 `ai_usage_2026-12`（PRC 已是 12 月）

    闸门读到 0 → **预算在这些时段完全不生效**。每月一次、每次 8 小时、
    无任何日志，只能靠账单发现。

    这条断言把「时区错位会产生不同的月份键」这件事钉住——它同时是上面
    那段危害分析的**可执行证据**：同一个绝对时刻在两个时区下确实落到
    不同月份。
    """
    # 同一个绝对时刻：UTC 2026-11-30 23:30 == PRC 2026-12-01 07:30
    instant = datetime(2026, 11, 30, 23, 30, tzinfo=timezone.utc)
    utc_view = instant.astimezone(timezone.utc)
    prc_view = instant.astimezone(timezone(timedelta(hours=8)))

    assert usage_store.month_key(utc_view) == "2026-11"
    assert usage_store.month_key(prc_view) == "2026-12"
    # 这就是那个 8 小时窗口：两个月份键不同，闸门与记账会脱节
    assert usage_store.month_key(utc_view) != usage_store.month_key(prc_view)

    # 口径差异也会传染到 run_after：UTC 视角「下月」是 12 月，PRC 视角是 1 月
    assert usage_store.next_month_start(utc_view).month == 12
    assert usage_store.next_month_start(prc_view).month == 1


@pytest.mark.pg
def test_worker_的_now_取自数据库而不是进程时钟_月份口径一致(pg_conn):
    """**生产路径的守门用例**：`loop._db_now(conn)` 必须返回数据库的时区。

    判据：`_db_now` 的 utcoffset 与数据库 `now()` 的 utcoffset 相同。
    平板容器时区是 UTC、PG 是 PRC，两者差 8 小时，这条会立刻暴露
    「worker 用了进程时钟」这种写法。
    """
    from kb.worker import loop

    db_now = pg_conn.run("select now()")[0][0]
    worker_now = loop._db_now(pg_conn)

    assert worker_now.utcoffset() == db_now.utcoffset(), (
        "worker 取的时刻时区与数据库不一致："
        f"{worker_now.utcoffset()} != {db_now.utcoffset()}"
    )
    # 月份口径一致才是真正要防的后果
    assert usage_store.month_key(worker_now) == usage_store.month_key(db_now)


# ── budget_cny（D5 / D13 / D16）───────────────────────────────


def test_budget_cny未设置时默认20_D5_D13():
    """D5 / D13：`ai_budget_cny` 未设置时按 20 元执行，**不返回 null**。"""
    conn = _Recorder(rows=[])
    assert usage_store.budget_cny(conn) == 20.0


def test_budget_cny读设置值():
    conn = _Recorder(rows=[[5]])
    assert usage_store.budget_cny(conn) == 5.0


def test_budget_cny为0时返回0不是默认值_D16():
    """D16：`ai_budget_cny=0` 是用户明确表达「一分钱都不想花」，
    不能被「0 是假值」的默认值逻辑吃掉。
    """
    conn = _Recorder(rows=[[0]])
    assert usage_store.budget_cny(conn) == 0.0


def test_budget_cny为负数时按0处理():
    conn = _Recorder(rows=[[-1]])
    assert usage_store.budget_cny(conn) == 0.0


def test_budget_cny写成字符串也能认():
    conn = _Recorder(rows=[["8.5"]])
    assert usage_store.budget_cny(conn) == 8.5


def test_budget_cny是json_字符串也能认():
    """`settings.value` 是 jsonb，读出来可能是 `'8.5'`（带引号的 JSON 字符串）。"""
    conn = _Recorder(rows=[['"8.5"']])
    assert usage_store.budget_cny(conn) == 8.5


def test_budget_cny值损坏时回落默认值():
    """手改坏的值不能让预算闸门失去意义：回默认 20 比「无限额度」安全。"""
    conn = _Recorder(rows=[["不是数字"]])
    assert usage_store.budget_cny(conn) == 20.0


def test_warn_line是预算的八成():
    assert usage_store.warn_line(20.0) == 16.0
    assert usage_store.warn_line(0.0) == 0.0
    assert usage_store.warn_line(9.99) == 7.992


# ── read（D7 / D14）──────────────────────────────────────────


def test_无记录时按0读取不报错_D7_D14():
    """D7 / D14：当月无任何调用时 `settings` 里没有这个键，
    读取必须按全零处理，**不报错、不返回 null**。
    """
    conn = _Recorder(rows=[])
    state = usage_store.read(conn, "2026-11")
    assert state.calls == 0
    assert state.prompt_tokens == 0
    assert state.completion_tokens == 0
    assert state.cny == 0.0
    assert state.budget_warned is False
    assert state.budget_warned_at is None


def test_read解析已有记录():
    conn = _Recorder(
        rows=[
            [
                '{"calls": 5, "promptTokens": 100, "completionTokens": 20,'
                ' "cny": 1.2345, "budgetWarned": true,'
                ' "budgetWarnedAt": "2026-11-27T14:32:08+08:00"}'
            ]
        ]
    )
    state = usage_store.read(conn, "2026-11")
    assert state.calls == 5
    assert state.prompt_tokens == 100
    assert state.completion_tokens == 20
    assert state.cny == pytest.approx(1.2345)
    assert state.budget_warned is True
    assert state.budget_warned_at == "2026-11-27T14:32:08+08:00"


def test_read在json坏了时按全零处理():
    """库里的值被人手改坏时不能整条流水线停掉：按零处理，闸门会放行，
    最多多花一点，而不是 worker 卡死。
    """
    conn = _Recorder(rows=[["不是 json"]])
    state = usage_store.read(conn, "2026-11")
    assert state.calls == 0
    assert state.cny == 0.0


def test_read用正确的键名_D12():
    conn = _Recorder(rows=[])
    usage_store.read(conn, "2026-11")
    assert conn.params[0]["k"] == "ai_usage_2026-11"


def test_read在值不是对象时按全零处理():
    """`ai_usage_*` 被人改成数组或数字时不能崩：按零处理，闸门放行。"""
    conn = _Recorder(rows=[["[1, 2, 3]"]])
    state = usage_store.read(conn, "2026-11")
    assert state.calls == 0
    assert state.cny == 0.0


def test_read在pg8000直接给dict时也认():
    """pg8000 对 jsonb 有时给 Python 对象，有时给字符串，两种都要认。"""
    conn = _Recorder(rows=[[{"calls": 7, "cny": 1.5, "budgetWarned": True}]])
    state = usage_store.read(conn, "2026-11")
    assert state.calls == 7
    assert state.cny == 1.5
    assert state.budget_warned is True


def test_read在budgetWarnedAt不是字符串时置空():
    """防御性：`props` 里塞了什么都有可能（用户可手改 jsonb）。"""
    conn = _Recorder(rows=[[{"budgetWarned": True, "budgetWarnedAt": 12345}]])
    assert usage_store.read(conn, "2026-11").budget_warned_at is None


def test_read在字段类型乱七八糟时收成0():
    conn = _Recorder(
        rows=[[{"calls": "很多", "promptTokens": None, "cny": "不是数字"}]]
    )
    state = usage_store.read(conn, "2026-11")
    assert state.calls == 0
    assert state.prompt_tokens == 0
    assert state.cny == 0.0


# ── add：一条 SQL 完成全部累加（§4.7）─────────────────────────


def _add(conn: _Recorder | None = None, **overrides: object) -> _Recorder:
    """跑一次 `add`。

    默认给假连接一条 `returning value` 的行——这是**正常路径**：`insert ... on
    conflict ... returning value` 在成功时必然返回恰好一行。空返回只在驱动异常时
    出现，那条路径单独用 `test_add在returning为空时兜底读一次` 覆盖，
    不让它污染「累加只有一条 SQL」这条断言。
    """
    c = conn or _Recorder(rows=[['{"calls": 1, "cny": 0.0027}']])
    kwargs: dict[str, object] = {
        "month": "2026-11",
        "calls": 1,
        "prompt_tokens": 1200,
        "completion_tokens": 150,
        "delta_cny": 0.0027,
        "warn_line": 16.0,
    }
    kwargs.update(overrides)
    usage_store.add(c, **kwargs)  # type: ignore[arg-type]
    return c


def test_add在returning为空时兜底读一次不抛异常():
    """驱动返回空 `returning` 属于异常状态。兜底读一遍，不抛——
    记账失败不该让已经成功的摘要变成失败任务。

    用 `script` 明确表达「第一次空、第二次有」，而不是靠共享的 `_rows`：
    后者在 `len(seen)` 上分不出「正常的 1 条」与「兜底的 2 条」。
    """
    conn = _Recorder(script=[[], [['{"calls": 1, "cny": 0.001}']]])
    state = usage_store.add(
        conn,
        month="2026-11",
        calls=1,
        prompt_tokens=1,
        completion_tokens=1,
        delta_cny=0.001,
        warn_line=16.0,
    )
    assert len(conn.seen) == 2, f"兜底路径应当补一次读，实际：{conn.seen}"
    assert conn.seen[1].lower().startswith("select")
    assert state.calls == 1


def test_add只有一条SQL_4节7条():
    """**这条是整份设计里最重要的一条断言。**

    两条语句（先读再写）在 `pg8000.native` 的无事务路径上会丢更新。
    断言「只有一条 SQL」把这件事钉在测试里：有人把 `add` 改成读-改-写时，
    这条会立刻红。

    断言消息里把 `conn.seen` 打出来：这条曾经在「多个测试文件一起跑」时
    间歇红过（`_Recorder` 返回空 rows → 触发兜底读 → 2 条）。下次红的时候
    能直接看到发了哪几条 SQL，不用再猜。
    """
    conn = _add()
    assert len(conn.seen) == 1, (
        f"累加必须一条 SQL 完成，实际发了 {len(conn.seen)} 条：{conn.seen}"
    )
    sql = _joined(conn)
    assert "on conflict" in sql
    # 这条额外证明「没走兜底」：兜底会是一条纯 select
    assert "select" not in sql.split("on conflict")[0], f"第一条不该是读：{conn.seen}"


def test_add的每个占位符都带显式转型_防42P08():
    """**平板上真库实测到的 42P08 的守门用例。**

    `jsonb_build_object` 的签名是 `VARIADIC "any"`，PG 无法从 `any` 推断参数
    类型；`round(x, 4)` 又有 `numeric`/`double` 两个重载，参数类型未定时选不出。
    两者都会让 PG 在参数解析阶段直接报
    `42P08 could not determine data type of parameter $N`。

    **本机的假连接永远发现不了这个 bug**（只记字符串、不做 PG 解析），所以
    这条静态断言是本机能做到的最接近的守门：每个 `:name` 的**每一次**出现
    都必须紧跟 `::type`。

    写成「每次出现都检查」而不是「只查首次」：PG 只按首次定类型，但要求
    全部带转型之后，「哪个占位符漏了转型」不用靠数出现次序去判。
    """
    import re

    from kb.store.usage import _ADD_SQL

    names = ("k", "d_calls", "d_prompt", "d_completion", "d_cny", "warn_line")
    for name in names:
        hits = re.findall(rf":{name}(::\w+)?", _ADD_SQL)
        assert hits, f"{_ADD_SQL} 里没有 :{name}"
        bare = sum(1 for hint in hits if not hint)
        assert bare == 0, (
            f":{name} 有 {bare} 次裸写（共 {len(hits)} 次）。"
            f"裸写的占位符若出现在 jsonb_build_object / round 里会触发 42P08。"
        )


def test_add不使用set_value整值覆盖_4节7条():
    """现有 `set_value` 是 `value = excluded.value`（整块替换）。
    用它做累加等于每次覆盖，并发时后写的吃掉先写的。
    """
    conn = _add()
    sql = _joined(conn)
    assert "excluded.value" not in sql, "整值覆盖会丢更新"
    assert "jsonb_build_object" in sql, "累加必须在 SQL 里算"


def test_add的四个累加字段都在SQL里_D1():
    conn = _add()
    sql = _joined(conn)
    for field in ("calls", "prompttokens", "completiontokens", "cny"):
        assert field in sql, f"SQL 里没有累加 {field}"


def test_add用round_numeric_4_保留四位小数_D1():
    """浮点 `round()` 会累出 `16.010000000000002`，D10 ② 的「逐字不变」被噪声连坐。"""
    conn = _add()
    sql = _joined(conn)
    assert "round(" in sql
    assert ", 4)" in sql
    assert "numeric" in sql


def test_add写updated_at_D10b():
    """`D10` ③ 要断言 `updated_at` 有变。不写它，「整行没更新」就抓不到。"""
    conn = _add()
    assert "updated_at = now()" in _joined(conn)


def test_add不写任何写入计数器_D10a():
    """`D10a` 明确禁止。重复写同一个 `budgetWarned: true` 在最终状态上不可观测，
    为测它加 `warnCount` 会让每次调用多一处一致性风险。
    """
    conn = _add()
    sql = _joined(conn)
    for counter in ("warncount", "writcount", "writecount", "addcount"):
        assert counter not in sql, f"D10a 禁止写入计数器，出现 {counter}"


def test_add的month进键名_D12():
    conn = _add(month="2026-12")
    assert conn.params[0]["k"] == "ai_usage_2026-12"


def test_add的warn_line作为参数传入不在SQL里写死_D9():
    """`warn_line` = `budget × 0.8`，由调用方算好传入。
    写死在 SQL 里的话，改预算就得改 SQL。
    """
    conn = _add(warn_line=8.0)
    assert conn.params[0]["warn_line"] == 8.0
    assert "16" not in _joined(conn)


def test_add的budgetWarned分支显式取旧时间戳_D10():
    """`D10` ② 要求「已提醒时间戳冻结」。**这是最容易写错的一行**：
    每次重写 `now()` 会让时间戳一直变，而最终状态看起来完全正常。
    """
    conn = _add()
    sql = _joined(conn)
    assert "budgetwarnedat" in sql, "没在 SQL 里处理 budgetWarnedAt"
    # 冻结分支必须引用旧值，不能写 now()
    assert "value->'budgetwarnedat'" in sql or "->'budgetwarnedat'" in sql


def test_add的两个when顺序决定语义_D9_D10():
    """「已提醒过」的 `when` 必须在「首次跨线」之前。

    顺序反了的话，每次累加都会命中「首次跨线」分支，时间戳被反复重写——
    而 `budgetWarned` 始终是 true，最终状态看不出来。

    只在 `on conflict` 之后的部分里比顺序：`insert` 分支自己也有一次
    「是否跨线」的判断（首次调用就跨过 80% 时要在 insert 里直接写标记，
    见 `D11`），那不是本条要验的对象。
    """
    conn = _add()
    sql = _joined(conn)
    update_part = sql[sql.find("on conflict") :]
    first = update_part.find("budgetwarned' = 'true'")
    second = update_part.find(">= :warn_line")
    assert first != -1 and second != -1
    assert first < second, "顺序反了：已提醒分支必须在首次跨线分支之前"


def test_add返回新值供调用方直接用():
    """`returning value` 让调用方不必再读一次（多一次往返就是多一个丢更新的窗口）。"""
    conn = _Recorder(rows=[['{"calls": 1, "cny": 0.0027}']])
    _add(conn)
    sql = _joined(conn)
    assert "returning value" in sql


# ── 预算判断（D3 / D5 / D16）──────────────────────────────────


def test_can_spend_已花加预估达到上限就拦住_D3():
    """D3 的核心不等式：`已花 + 预估 >= 上限` → 不发请求。

    注意是 `>=` 而不是 `>`：`0.999 + 0.001 == 1.0` 恰好落在线上，
    用 `>` 会放行，`D3` 的「零次 HTTP 请求」失败。
    """
    assert usage_store.can_spend(spent=0.999, estimate=0.001, budget=1.0) is False


def test_can_spend_低于上限时放行():
    assert usage_store.can_spend(spent=0.9, estimate=0.001, budget=1.0) is True


def test_can_spend_预算为0时恒为拦_D16():
    """D16：`ai_budget_cny=0` → 所有调用被拦，零请求。"""
    assert usage_store.can_spend(spent=0.0, estimate=0.001, budget=0.0) is False


def test_can_spend_已花正好等于上限时拦():
    assert usage_store.can_spend(spent=20.0, estimate=0.001, budget=20.0) is False


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


def _pg_add(conn, **overrides: object):
    kwargs: dict[str, object] = {
        "month": "2026-11",
        "calls": 1,
        "prompt_tokens": 1200,
        "completion_tokens": 150,
        "delta_cny": 0.0027,
        "warn_line": 16.0,
    }
    kwargs.update(overrides)
    return usage_store.add(conn, **kwargs)  # type: ignore[arg-type]


@pytest.mark.pg
def test_连续两次累加后calls精确等于2_D1(pg_conn):
    """D1：**精确等值**，不是 `> 0`。

    `> 0` 在「第二次覆盖掉第一次」时同样通过，那正是最可能的失败模式。
    """
    _pg_add(pg_conn)
    _pg_add(pg_conn)
    state = usage_store.read(pg_conn, "2026-11")
    assert state.calls == 2


@pytest.mark.pg
def test_累加后promptTokens与completionTokens精确等于两次之和_D1(pg_conn):
    _pg_add(pg_conn, prompt_tokens=1200, completion_tokens=150)
    _pg_add(pg_conn, prompt_tokens=800, completion_tokens=100)
    state = usage_store.read(pg_conn, "2026-11")
    assert state.prompt_tokens == 2000
    assert state.completion_tokens == 250


@pytest.mark.pg
def test_金额保留4位小数_D1(pg_conn):
    """D1：金额精度是 4 位小数，不出现浮点噪声。

    ⚠️ **这条改过一版：第一版喂两次 `delta_cny=0.00005` 并断言合计 `0.0001`，
    平板真库上跑红（实得 `0.0002`）。**

    为什么实得 `0.0002` 是实现应有的行为：

    ```
    第 1 次：round(0 + 0.00005, 4)      = 0.0001   ← 0.00005 进位到 4 位
    第 2 次：round(0.0001 + 0.00005, 4) = round(0.00015, 4) = 0.0002
    ```

    SQL 是「**每累加一次就 round 到 4 位**」，所以每次独立进位。
    「两次各 0.00005 合计 0.0001」只在「累加完再统一 round」时成立。

    **现在改为只调一次。** 理由：这条用例的名字是「金额保留 4 位小数」，
    它要证明的是「一个 5 位小数的输入被正确舍入到 4 位」，与累加语义无关。
    调一次能干净地说明这一点；调两次会把「舍入」和「累加」两件事混在一条
    断言里，红的时候分不清是哪个环节的问题。

    命中进位的边界值 `0.00005` 是最好的样本：`round(0.00005, 4)` 必须是
    `0.0001`（进位）而不是 `0.0`（截断）。
    """
    _pg_add(pg_conn, delta_cny=0.00005)
    state = usage_store.read(pg_conn, "2026-11")

    assert state.cny == pytest.approx(0.0001, abs=1e-12), (
        f"5 位小数应进位到 4 位（0.00005 → 0.0001），实际 {state.cny!r}"
    )
    # 不能出现 0.000100000000000002 这类浮点噪声
    assert round(state.cny, 4) == state.cny, f"出现了 4 位以外的精度：{state.cny!r}"


@pytest.mark.pg
def test_每次累加都独立round到4位_单次误差有界_D1(pg_conn):
    """**把上面那条踩到的行为显式钉住**，避免下次有人「修」成另一种口径。

    每次 add 都对结果 round 到 4 位，所以 `0.00005` 加两次得到 `0.0002`
    而不是 `0.0001`。单次误差上界 = 0.00005 元，有界且与调用次数无关。

    为什么要专门测「看起来像 bug 的行为」：如果哪天有人把 SQL 改成
    「累加完不 round」或「先 round 增量再累加」，这条会红，改动者必须
    显式面对「精度口径变了」这件事，而不是悄悄改掉。
    """
    _pg_add(pg_conn, delta_cny=0.00005)
    assert usage_store.read(pg_conn, "2026-11").cny == pytest.approx(0.0001, abs=1e-12)

    _pg_add(pg_conn, delta_cny=0.00005)
    # 第二次：round(0.0001 + 0.00005, 4) = round(0.00015, 4) = 0.0002
    assert usage_store.read(pg_conn, "2026-11").cny == pytest.approx(0.0002, abs=1e-12)


@pytest.mark.pg
def test_同一进程内两次add不丢更新_4节7条(pg_conn):
    """同一连接上连加 5 次，`calls` 必须精确等于 5。

    读-改-写实现在单连接串行场景下也能通过（因为没并发），所以这条不是
    并发测试，它是「**一条 SQL** 这件事在真库上的行为验证」：
    整值覆盖的 `set_value` 会让结果变成 1。
    """
    for _ in range(5):
        _pg_add(pg_conn)
    assert usage_store.read(pg_conn, "2026-11").calls == 5


@pytest.mark.pg
def test_无记录时按0读取不报错_真库_D7_D14(pg_conn):
    state = usage_store.read(pg_conn, "2099-01")
    assert state.calls == 0
    assert state.cny == 0.0


@pytest.mark.pg
def test_累计15点99时budgetWarned键不存在_D8(pg_conn):
    """D8：未跨 80% 时**键本身不存在**（既不 false 也不 true）。"""
    _pg_add(pg_conn, delta_cny=15.99, warn_line=16.0)
    raw = pg_conn.run("select value from settings where key = 'ai_usage_2026-11'")[0][0]
    assert "budgetWarned" not in str(raw)


@pytest.mark.pg
def test_跨过80线后budgetWarned为true且写入budgetWarnedAt_D9(pg_conn):
    _pg_add(pg_conn, delta_cny=15.99, warn_line=16.0)
    _pg_add(pg_conn, delta_cny=0.02, warn_line=16.0)

    state = usage_store.read(pg_conn, "2026-11")
    assert state.cny == pytest.approx(16.01)
    assert state.budget_warned is True
    assert state.budget_warned_at, "首次跨线必须写时间戳"


@pytest.mark.pg
def test_连写10次后budgetWarnedAt逐字不变_D10_2(pg_cfg):
    """D10 ②：抓住「每次都重写提醒时间戳」。

    ## ⚠️ 这条用例原本是**假绿**，本轮修掉了

    第一版复用 `pg_conn` fixture，而它有 `begin` —— 整条用例跑在**一个事务**里。
    Postgres 的 `now()` 是 `transaction_timestamp()`，**同事务内恒定不变**
    （平板实测两次取到 `2026-11-27 21:15:51.662995+08:00`，完全相同）。

    后果：哪怕把实现改成「每次都写 `now()`」（正是本条要防的 bug），
    时间戳也不会变，断言照样通过。**变异测试证实**：把 `usage.py` 冻结分支
    的 `settings.value->'budgetWarnedAt'` 改成 `to_jsonb(now()::text)` 之后，
    旧版本这条用例**仍然 passed**。

    ## 修法：不复用 fixture，让每次 `add` 各自成事务

    生产形态就是每次 `add` 一个事务（`Pool.connection()` 每次取连接、
    `pg8000.native` 无事务管理、自动提交）。只有在这种形态下 `now()` 才会
    前进，测试才抓得到「重写」。

    - 自己 `connect(pg_cfg)`，**不显式 `begin`** → 每条 `run` 自动提交
    - `finally` 里删掉该 key，不给其他用例留脏数据

    **为什么不能用 `pg_sleep` 解决**：同事务内 `now()` 是事务**开始**时刻，
    sleep 多久都不变。不存在「在单事务内验证时间流动」的办法 —— 这是
    `transaction_timestamp()` 的定义决定的，不是等待时间不够。
    """
    from kb.store.db import connect as pg_connect

    key = usage_store.USAGE_KEY_PREFIX + "2026-11"
    conn = pg_connect(pg_cfg)
    try:
        # 从干净状态起：其他用例可能留下同月数据
        conn.run("delete from settings where key = :k", k=key)

        usage_store.add(
            conn,
            month="2026-11",
            calls=1,
            prompt_tokens=100,
            completion_tokens=50,
            delta_cny=16.01,
            warn_line=16.0,
        )
        first = usage_store.read(conn, "2026-11").budget_warned_at
        assert first, "首次跨线必须写时间戳"

        for _ in range(10):
            usage_store.add(
                conn,
                month="2026-11",
                calls=1,
                prompt_tokens=100,
                completion_tokens=50,
                delta_cny=0.01,
                warn_line=16.0,
            )

        state = usage_store.read(conn, "2026-11")
        assert state.budget_warned is True
        assert state.budget_warned_at == first, (
            f"提醒时间戳被反复重写了：{first!r} → {state.budget_warned_at!r}"
        )
    finally:
        conn.run("delete from settings where key = :k", k=key)
        conn.close()


@pytest.mark.pg
def test_D10_2的判据在跨事务下真的会变_变异测试前置(pg_cfg):
    """**给 D10_2 自身做有效性检查**：证明「跨事务时 `now()` 确实会前进」。

    如果不成立，D10_2 会重新变成假绿且没人发现。这条把前提钉住：
    同一个连接、两次独立 `add`（各自成事务）→ 两次 `now()` **必须不同**。

    这条比「再写一遍 D10_2」有价值：它验证的是**测试手段**有效，
    而不是**被测行为**正确。前者失效会让后者永远绿。
    """
    from kb.store.db import connect as pg_connect

    key = usage_store.USAGE_KEY_PREFIX + "1999-01"  # 用一个没人用的月份
    conn = pg_connect(pg_cfg)
    try:
        conn.run("delete from settings where key = :k", k=key)
        stamps: list[object] = []
        for _ in range(2):
            usage_store.add(
                conn,
                month="1999-01",
                calls=1,
                prompt_tokens=1,
                completion_tokens=1,
                delta_cny=0.001,
                warn_line=16.0,
            )
            stamps.append(
                conn.run("select updated_at from settings where key = :k", k=key)[0][0]
            )
        assert stamps[0] != stamps[1], (
            f"跨事务（各自提交）时 now() 没有前进，D10_2 的时间断言会失效：{stamps}"
        )
    finally:
        conn.run("delete from settings where key = :k", k=key)
        conn.close()


@pytest.mark.pg
def test_连写10次后spentCny严格递增_D10_3(pg_cfg):
    """D10 ③：连写 10 次后 `spentCny` **严格**递增。

    ## 关于 `updated_at`：本条**不再断言它**，理由是变异测试实测出来的

    第一版断言过 `updated_at` 非递减 → 假绿（同事务下全等也满足非递减）。
    改成跨事务后我又做了变异测试：**把 `usage.py` 里的 `updated_at = now()`
    整行删掉，这条仍然绿**。

    查清原因是 `settings` 表有一个 `before update` 触发器：

    ```sql
    create trigger settings_touch before update on settings
      for each row execute function touch_updated_at();   -- new.updated_at = now()
    ```

    **触发器无条件写 `updated_at`**，所以 SQL 里那一行对最终值没有影响。
    任何「断言 updated_at 被更新」的用例都测不到「SQL 有没有写它」，
    测到的只是触发器在工作 —— 而触发器本身已由
    `tests/test_store_pg.py::test_updated_at_trigger_overwrites_stale_value` 覆盖。

    结论：`updated_at` 的正确性**不由这条用例负责**。在这里断言它只会
    给出「看似在测、其实测的是别的东西」的假安全感 —— 与本轮 D10_2 修的
    是同一类缺陷。

    （`usage.py` 里保留 `updated_at = now()` 不是错误，只是与触发器重复。
    保留的价值是可读性：一眼看出这条语句会更新行的时间戳。）
    """
    from kb.store.db import connect as pg_connect

    key = usage_store.USAGE_KEY_PREFIX + "2026-11"
    conn = pg_connect(pg_cfg)
    try:
        conn.run("delete from settings where key = :k", k=key)

        def _one_add(delta: float) -> None:
            usage_store.add(
                conn,
                month="2026-11",
                calls=1,
                prompt_tokens=10,
                completion_tokens=5,
                delta_cny=delta,
                warn_line=16.0,
            )

        _one_add(1.0)
        spends = [usage_store.read(conn, "2026-11").cny]

        for _ in range(9):
            _one_add(0.01)
            spends.append(usage_store.read(conn, "2026-11").cny)

        # 严格递增：每次多花 0.01，10 次必须单调上升。
        # 这条能抓住「累加失效」——若 `coalesce(...) + :d_cny` 错写成覆盖，
        # 序列会变成 [1.0, 0.01, 0.01, ...]，sorted 检查立刻红。
        assert spends == sorted(spends), f"spentCny 必须单调递增：{spends}"
        assert spends[-1] > spends[0], f"spentCny 没有增长：{spends}"
        # 精确值：1.0 + 9×0.01 = 1.09（每次 round 到 4 位，累加无损）
        assert spends[-1] == pytest.approx(1.09, abs=1e-9), (
            f"10 次累加的结果不对：{spends[-1]!r}（应 1.09）"
        )
    finally:
        conn.run("delete from settings where key = :k", k=key)
        conn.close()


@pytest.mark.pg
def test_一次跨过80和100时两个标记同时成立_D11(pg_conn):
    _pg_add(pg_conn, delta_cny=20.5, warn_line=16.0)
    state = usage_store.read(pg_conn, "2026-11")
    assert state.budget_warned is True
    assert usage_store.can_spend(spent=state.cny, estimate=0.001, budget=20.0) is False


@pytest.mark.pg
def test_新月份键里没有budgetWarned_D12(pg_conn):
    """D12：跨月天然重置（换了新键）。旧月份键里的标记保持 true 不被清除。"""
    _pg_add(pg_conn, month="2026-11", delta_cny=16.01, warn_line=16.0)
    _pg_add(pg_conn, month="2026-12", delta_cny=0.01, warn_line=16.0)

    old = usage_store.read(pg_conn, "2026-11")
    new = usage_store.read(pg_conn, "2026-12")
    assert old.budget_warned is True, "旧月份的标记被清除了"
    assert new.budget_warned is False
    assert new.cny == pytest.approx(0.01)


@pytest.mark.pg
def test_budget_cny真库未设置时默认20_D5(pg_conn):
    assert usage_store.budget_cny(pg_conn) == 20.0


@pytest.mark.pg
def test_budget_cny真库为0时返回0_D16(pg_conn):
    pg_conn.run(
        "insert into settings (key, value) values ('ai_budget_cny', '0'::jsonb)"
        " on conflict (key) do update set value = excluded.value"
    )
    assert usage_store.budget_cny(pg_conn) == 0.0


@pytest.mark.pg
def test_add真库返回累加后的新值(pg_conn):
    state = _pg_add(pg_conn, delta_cny=0.5)
    assert state.calls == 1
    assert state.cny == pytest.approx(0.5)

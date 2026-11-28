"""jobs 表的通用读写：入队、计数、明细、领取、状态流转。

为什么要单独一个文件：`items.py` 里的入库+入队是一条 CTE（原子性需要），
但「排队里还有几条」「这个 item 的 job 卡在哪一步」这些读操作与 items 无关，
放进来才不会让 items.py 变成一个什么都管的文件。

状态机与取任务 SQL 照技术方案 §6.4。四个必须有的谓词，删掉任何一个都会出事：

| 谓词 | 删掉的后果 |
|---|---|
| `FOR UPDATE SKIP LOCKED` | 两个 worker 抢到同一条，同一条被处理两遍 |
| `run_after <= now()` | 失败退避形同虚设，刚失败的任务立刻被重取 |
| `attempts + 1 >= 3` 才 failed | 坏任务无限重试，打爆上游站点 |
| `state = 'running' AND updated_at < now() - 15 分钟` | worker 被 kill 后任务永远卡在 running |

`mark_failed` 用 `RETURNING state` 拿新状态而不是在 Python 里重算：
SQL 已经算过一次 `attempts + 1`，两处各算一次必有分歧的那天。
"""

from __future__ import annotations

from datetime import datetime

import pg8000.native

VALID_STEPS = ("fetch", "ocr", "summarize", "tag", "index")

# 崩溃恢复阈值。worker 被 kill 时任务停在 running，超过这个时长视为死掉。
STALE_MINUTES = 15

# 失败重试上限。到第 3 次标 failed，不再重试。
MAX_ATTEMPTS = 3


def enqueue(conn: pg8000.native.Connection, item_id: int, step: str) -> bool:
    """插一个待执行任务。已有同步骤未完成任务时什么都不做，返回 False。

    冲突目标必须带 `where state in ('pending','running')`：库里的唯一索引
    `jobs_pending_uniq` 是部分索引，不写这个谓词 PG 找不到仲裁索引。
    """
    rows = conn.run(
        "insert into jobs (item_id, step) values (:i, :s) "
        "on conflict (item_id, step) where state in ('pending','running') "
        "do nothing returning id",
        i=item_id,
        s=step,
    )
    return bool(rows)


def counts(conn: pg8000.native.Connection, item_id: int) -> tuple[int, int, int, int]:
    """返回 (total, pending, running, failed)。一次往返算完，不在 Python 里数行。"""
    rows = conn.run(
        "select count(*), "
        "count(*) filter (where state = 'pending'), "
        "count(*) filter (where state = 'running'), "
        "count(*) filter (where state = 'failed') "
        "from jobs where item_id = :i",
        i=item_id,
    )
    row = rows[0]
    return int(row[0]), int(row[1]), int(row[2]), int(row[3])


def rows(conn: pg8000.native.Connection, item_id: int) -> list[dict[str, object]]:
    """某条内容的全部任务，按入队顺序。详情接口直接吐出它，排障时看卡在哪一步。"""
    result = conn.run(
        "select step, state, error, attempts, run_after from jobs "
        "where item_id = :i order by id",
        i=item_id,
    )
    return [
        {
            "step": str(row[0]),
            "state": str(row[1]),
            "error": None if row[2] is None else str(row[2]),
            "attempts": int(row[3]),
            "runAfter": row[4].isoformat()
            if isinstance(row[4], datetime)
            else str(row[4]),
        }
        for row in result
    ]


def claim_one(conn: pg8000.native.Connection) -> dict[str, object] | None:
    """领一条到期的待办任务并原子地标成 running。没有就返回 None。

    `FOR UPDATE SKIP LOCKED` 是这里唯一不能省的写法：它让并发的第二个 worker
    跳过已被锁住的行，直接去拿下一条，而不是等待或抢到同一条。
    """
    picked = conn.run(
        "with picked as ("
        " select id from jobs"
        " where state = 'pending' and run_after <= now()"
        " order by run_after"
        " limit 1"
        " for update skip locked"
        ") "
        "update jobs set state = 'running', updated_at = now() "
        "from picked where jobs.id = picked.id "
        "returning jobs.id, jobs.item_id, jobs.step, jobs.attempts, jobs.state",
    )
    if not picked:
        return None
    row = picked[0]
    return {
        "id": int(row[0]),
        "itemId": int(row[1]),
        "step": str(row[2]),
        "attempts": int(row[3]),
        "state": str(row[4]),
    }


def mark_done(conn: pg8000.native.Connection, job_id: int) -> None:
    """标成功。终态，不再被领取。"""
    conn.run(
        "update jobs set state = 'done', error = null, updated_at = now() "
        "where id = :j",
        j=job_id,
    )


def mark_failed(conn: pg8000.native.Connection, job_id: int, error: str) -> str:
    """标失败并返回落到的新状态：`pending`（还会重试）或 `failed`（已放弃）。

    退避是 `1 分钟 × 2^attempts`：第 1 次失败等 1 分钟，第 2 次等 2 分钟。
    固定间隔要么让上游被打爆，要么让一次临时故障把任务饿死。

    `attempts` 的加一写在 SQL 里，新状态也用 `RETURNING` 拿回来——
    不在 Python 里重算一遍，两处算术迟早会不一致。
    """
    rows = conn.run(
        "update jobs set "
        " attempts = attempts + 1, "
        " state = case when attempts + 1 >= 3 then 'failed' else 'pending' end, "
        " run_after = now() + (interval '1 minute' * power(2, attempts)), "
        " error = :e, "
        " updated_at = now() "
        "where id = :j "
        "returning state",
        j=job_id,
        e=error,
    )
    return str(rows[0][0]) if rows else "pending"


def recover_stale(conn: pg8000.native.Connection) -> int:
    """把卡在 running 太久（worker 被 kill）的任务退回 pending，返回条数。

    attempts 不加：这是崩溃不是失败，把崩溃算成一次失败会让任务少试一次。
    """
    rows = conn.run(
        "update jobs set state = 'pending', updated_at = now() "
        "where state = 'running' and updated_at < now() - interval '15 minutes' "
        "returning id",
    )
    return len(rows)


# ── 推迟 / 一次判死 / 自定义退避（AI-04 / AI-05 / B22 / B25 / B26）────────
#
# 为什么是三个窄函数而不是给 `mark_failed` 加一个 `count_attempt: bool = True`：
# `mark_failed` 的 SQL 里 `attempts = attempts + 1`、`state = case when
# attempts + 1 >= 3`、`run_after = now() + 2^attempts` **三处都依赖「这次算一次
# 失败」**。加参数等于在一条 SQL 里塞两套互斥分支，而默认值 True 意味着
# 「忘记传」时静默消耗一次尝试——`E1` 的断言会变成靠调用方自觉。
#
# 所以 `defer` / `defer_until` 的 SQL 里**根本不出现 `attempts`**。
# 这是「推迟不计次」在代码层面可一眼确认的证据，也是
# `tests/test_jobs_defer.py::test_defer_的SQL里不出现attempts_E1` 守的东西。
#
# 间隔一律写 `interval '1 second' * :seconds`，**不写 `interval 'N minutes'`**：
# `recover_stale` 靠 `interval '15 minutes'` 作为判据，两处都写分钟字面量会让
# 假连接把两条语句看成一类。


def defer(
    conn: pg8000.native.Connection, job_id: int, seconds: float, reason: str
) -> None:
    """把任务往后推 `seconds` 秒。**`attempts` 不变**——推迟不是失败。

    两个使用方：
    - 未配置 AI（`AI-04`）：推 1800 秒（30 分钟）
    - 预算超限的通用推迟（目前不用，留给将来的「服务端要求稍后」）

    原因写进 `jobs.error`，前缀固定 `已推迟：`。`derive_status` 只看 `state`，
    所以带 `error` 的 `pending` 任务不会让条目显示成「处理失败」，
    同时用户能在详情页看到「为什么还没出摘要」。
    """
    conn.run(
        "update jobs set "
        " state = 'pending', "
        " run_after = now() + interval '1 second' * :seconds, "
        " error = :e, "
        " updated_at = now() "
        "where id = :j",
        j=job_id,
        seconds=seconds,
        e=reason,
    )


def defer_until(
    conn: pg8000.native.Connection, job_id: int, when: datetime, reason: str
) -> None:
    """把任务推到指定时刻。`attempts` 不变。

    预算超限推到下月 1 日 00:05 用这个（`D3`）。与 `defer` 分开的原因是
    「推到某时刻」与「推多久」的入参类型不同，混成一个函数就要用
    `datetime | float` 联合类型，读代码的人得靠猜。
    """
    conn.run(
        "update jobs set "
        " state = 'pending', "
        " run_after = :when, "
        " error = :e, "
        " updated_at = now() "
        "where id = :j",
        j=job_id,
        when=when,
        e=reason,
    )


def fail_hard(conn: pg8000.native.Connection, job_id: int, error: str) -> None:
    """算一次失败并**直接判死**（`B26`：401/403）。

    为什么必须一次判死：密钥错或余额不足属于不可自愈错误。套用「满 3 次
    failed」的统一规则会让用户白等 3 分钟，且最终提示仍是「重试 3 次后失败」，
    看不出真正原因是余额。

    `attempts = attempts + 1` 保留：它确实消耗了一次尝试，界面上的
    「试了 1 次就失败」是事实。`run_after` 不动——已判死，不会被再领取。
    """
    conn.run(
        "update jobs set "
        " attempts = attempts + 1, "
        " state = 'failed', "
        " error = :e, "
        " updated_at = now() "
        "where id = :j",
        j=job_id,
        e=error,
    )


def mark_failed_delayed(
    conn: pg8000.native.Connection, job_id: int, error: str, seconds: float
) -> str:
    """与 `mark_failed` 同形态（`attempts+1`、满 3 次判死），只把退避间隔换成入参。

    **不新增语义**——这是它存在的核心理由。两套退避算法并存时，改了一处忘了
    另一处就是两周后才会被发现的分歧（现象是「连接失败后等的时间与设计不符」，
    而设计文档写的是 3 分钟）。

    使用方：连接失败退避 180 秒（`B22`，避免每 1 分钟打一次 DNS）、
    429 按 `Retry-After`（`B25`，服务端自己说了等多久，比我们猜的准）。
    """
    rows = conn.run(
        "update jobs set "
        " attempts = attempts + 1, "
        " state = case when attempts + 1 >= 3 then 'failed' else 'pending' end, "
        " run_after = now() + interval '1 second' * :seconds, "
        " error = :e, "
        " updated_at = now() "
        "where id = :j "
        "returning state",
        j=job_id,
        seconds=seconds,
        e=error,
    )
    return str(rows[0][0]) if rows else "pending"


# ── 上游阻塞判定（R6 检索：index 步的「难题二」）──────────────────────


def blocking_upstream(
    conn: pg8000.native.Connection,
    item_id: int,
    job_id: int,
    *,
    steps: tuple[str, ...],
    soon_seconds: float,
) -> str | None:
    """返回第一个「会阻塞本步骤」的上游步骤名，没有则 `None`。

    ## 三条判据，删掉任何一条都会出事

    | 判据 | 没有它会怎样 |
    |---|---|
    | `id <> :j` | 自己当前是 `running`，会把自己判成阻塞上游，**无限推迟** |
    | `state = 'running'` 也算阻塞 | 上游正在跑（抓取 10–20 秒、AI 调用 3–10 秒），此时索引会写出半成品 |
    | `run_after <= now() + soon_seconds` | 见下 |

    ## `soon_seconds` 是设计 §4.2 对 §6.4 的实质修正

    `summarize` 在 AI 未配置时会 `defer 1800 秒`
    （`ai_runtime.DEFER_SECONDS_NOT_CONFIGURED`），状态仍是 `pending`。
    字面执行「任何 pending 上游都等」的话，索引要等它 30 分钟——
    **新存进来的内容半小时内搜不到**，而这半小时里标题和正文其实已经在库里了。
    用户对这一现象的唯一解释是「搜索坏了」。

    改成「只等 `soon_seconds`（90 秒）内要到期的上游」之后：

    - 正常流：`summarize` 与 `tag` 都是秒级到分钟级，落在窗口内 → 索引等它们
      → 写出的文档字段是齐的。
    - AI 未配置：`summarize` 的下次执行在 30 分钟后 → 索引立刻写出
      title/content_text/kind/saved_at → 新内容秒级可搜；等 AI 配好、
      `summarize` 与 `tag` 完成后再入队一次 `index`，upsert 补上 summary 与 tags。

    代价是多一次写入（upsert 幂等，无正确性影响）。

    ## ⚠️ 这里的 SQL 里**不出现 `run_after` 之外的时间措辞**

    `interval '1 second' * :soon` 而不是 `interval '90 seconds'`：
    `tests/test_worker_index_step.py` 用「语句里不出现 attempts」验证
    「推迟不计次」，而 `recover_stale` 靠 `interval '15 minutes'` 被假连接识别。
    参数化秒数避免三段不同类型的语句在假连接眼里撞成一条。
    """
    if not steps:
        return None
    rows = conn.run(
        "select step from jobs"
        " where item_id = :i"
        "   and id <> :j"
        "   and step = any(cast(:steps as text[]))"
        "   and ( state = 'running'"
        "         or (state = 'pending'"
        "             and run_after <= now() + interval '1 second' * :soon) )"
        " order by id"
        " limit 1",
        i=item_id,
        j=job_id,
        steps=list(steps),
        soon=float(soon_seconds),
    )
    if not rows:
        return None
    return str(rows[0][0])

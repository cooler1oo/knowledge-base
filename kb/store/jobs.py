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

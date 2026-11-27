"""jobs 表的通用读写：入队、计数、明细。

为什么要单独一个文件：`items.py` 里的入库+入队是一条 CTE（原子性需要），
但「排队里还有几条」「这个 item 的 job 卡在哪一步」这些读操作与 items 无关，
放进来才不会让 items.py 变成一个什么都管的文件。

状态机与取任务的完整 SQL 在技术方案 §6.4；本轮只用到入队与查询，取任务属于 worker。
"""

from __future__ import annotations

from datetime import datetime

import pg8000.native

VALID_STEPS = ("fetch", "ocr", "summarize", "tag", "index")


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

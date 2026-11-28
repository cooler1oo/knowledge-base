"""回收站自动清扫线程（M4-03 T03）。

软删的东西在库里躺 30 天后被物理删掉。这个模块是那件事的唯一执行者。

## 为什么不用调度库

设计 §1.3 的三方案对比结论是「worker 内一个节拍线程」：

| 方案 | 代价 | 判定 |
|---|---|---|
| 一个节拍线程（本模块，约 40 行） | 零新增依赖；缺点是「固定间隔」表达不了 cron | **采用** |
| `apscheduler` | 新增依赖 + 一个 jobstore 配置面。价值是「多条 cron + 错过补偿」，而这里只有一条「每小时看一眼」 | 淘汰：技术方案 §6.5 的 22:30 备份才是它的场景，那时一并引入 |
| `pg_cron` | 要装 PG 扩展，`shared_preload_libraries` 在平板 proot 里改一次要重启并重验 G0' 全套 | 淘汰：环境代价远大于收益 |
| 手工跑 `python -m kb.worker.purge` | 零代码 | 淘汰：`AGENTS.md` 的立场是「能机器执行的不靠自觉」 |

## 启动时先跑一次，之后每 `PURGE_INTERVAL_SECONDS` 一次

**启动先跑一次不能省**：worker 停机多天再起来，若只靠节拍，要等一小时才
开始清理。那一小时里运维看日志会以为清扫没生效，而实际上它只是还没开始。

## 三条护栏都在 SQL 里（见 `items.purge_expired`）

| 护栏 | 没有它会怎样 |
|---|---|
| 保留期判断 | 会删掉用户昨天刚删的东西 |
| 跳过有 `running` 任务的条目 | 正在抓取的条目被删行，worker 拿着已不存在的 `item_id` 继续写——`update` 与 `mark_done` 都命中 0 行，**全程不报错** |
| `limit` + 循环 | 一次删几千行把 worker 占住几十秒，整条队列停摆 |

## 删完顺手清索引，但**失败不重试**

`purge_expired` 返回被删的 id 列表。清扫这一刻 worker 手里有 `Indexer`，
顺手把这批 id 从索引里删掉——覆盖「软删时 `run_index` 的索引删除永久失败」
这种残留（设计 §4.2 第 2 条路径）。

**失败只记日志、不重试**：索引清理的最终兜底是 `python -m kb.worker.rebuild`
的差集。为一条已经不存在于库里的索引文档做重试队列，复杂度远大于收益。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from kb.store import items as items_store
from kb.store.db import Pool

log = logging.getLogger(__name__)

#: 两次清扫之间的间隔。一小时足够——保留期是 30 天，晚一小时清掉没有用户可见
#: 的区别，而更密的节拍只是在空跑 SQL。
PURGE_INTERVAL_SECONDS = 3600

#: 一次删多少条。**不能省**：几千行一次删会把 worker 的连接占住几十秒，
#: 那期间主循环领不到任务，整条队列停摆。
PURGE_BATCH_SIZE = 200


def purge_once(
    pool: Pool,
    indexer: Any = None,
    *,
    older_than_days: int | None = None,
    limit: int = PURGE_BATCH_SIZE,
) -> list[int]:
    """跑一轮清扫，返回被删掉的 id 列表。

    循环直到「这一批不满 `limit`」——不满说明已经删完了。这样单次调用能清空
    任意积压，而每批之间开一个新连接，不会长时间占着池里的连接。

    `indexer is None` 时只删库不删索引：那意味着装配漏传（与 `run_index`
    同一情形的处理不同——那里 defer，这里继续删库）。理由：清扫是**兜底**
    机制，它晚一轮再清索引没有后果；而**不删库**会让过期的内容永远留在
    回收站里，那才是用户看得见的问题。
    """
    if older_than_days is None:
        older_than_days = items_store.TRASH_RETENTION_DAYS

    removed: list[int] = []
    while True:
        with pool.connection() as conn:
            ids = items_store.purge_expired(
                conn, older_than_days=older_than_days, limit=limit
            )

        if not ids:
            break

        removed.extend(ids)
        log.info(
            "回收站自动清理 %d 条（id 前 10 个：%s），保留期 %d 天",
            len(ids),
            ids[:10],
            older_than_days,
        )

        if indexer is not None:
            try:
                indexer.delete_ids(ids)
            except Exception as exc:  # noqa: BLE001 索引清理失败不重试，rebuild 兜底
                # 不用 `except MeiliDown`：这里连 `MeiliRejected`（请求写错）
                # 也不该中断清扫——库里的过期行已经删了，索引残留由 rebuild 清。
                log.warning(
                    "清理索引失败 %d 条（不影响库清理，rebuild 会兜底）：%s",
                    len(ids),
                    exc,
                )

        if len(ids) < limit:
            break

    if not removed:
        log.debug("回收站无需清理（保留期 %d 天）", older_than_days)
    return removed


def purge_loop(
    pool: Pool,
    stop: threading.Event,
    indexer: Any = None,
    *,
    interval_seconds: float = PURGE_INTERVAL_SECONDS,
) -> None:
    """节拍线程主体。**启动时先跑一次**，之后每 `interval_seconds` 一次。

    与 `kb/worker/__main__.py::_heartbeat_loop` 同一形状（`stop.wait()` 返回
    `True` 即退出），所以 `stop.set()` 能让它立刻结束而不是等满一小时。

    「先跑一次」写在这里而不是调用方：那样「启动时要不要清」就成了调用方的
    选择，而它不该是选择——停机多天后起来不清，用户看到的是「回收站里躺着
    半年前的东西」。

    异常一律吞掉并记日志：清扫失败不该让 worker 退出（与心跳同一立场）。
    """
    while True:
        try:
            purge_once(pool, indexer)
        except Exception:  # noqa: BLE001 清扫失败不退出 worker，下一轮再试
            log.exception("回收站清扫失败，%s 秒后重试", int(interval_seconds))

        if stop.wait(interval_seconds):
            return

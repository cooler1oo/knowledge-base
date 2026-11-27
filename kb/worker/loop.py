"""worker 主循环：领一条、分派一条、标一条。

不做的事（有意）：

- **不并发**。单人自用，一条一条做足够；并发会引入"两个 worker 抢同一条"的
  复杂度，而 `FOR UPDATE SKIP LOCKED` 只是为了将来真开多个实例时不出错。
- **不在本文件实现具体步骤**。三个步骤（`fetch` / `summarize` / `tag`）的实现
  在 `kb/worker/steps.py`。这里只做「领 → 判 → 分派 → 兜底」。
  `ocr` / `index` 仍未实现，领到就标回 `pending` 并写明「尚未实现」——
  标成 done 是谎报，条目会显示「已完成」但摘要标签全是空的。

**分派必须在 URL 判空之前**（`AI-11`）：原实现是「判步骤是否支持 → 取 URL 判空
→ 执行」，URL 判空对所有已支持的步骤一视同仁。`summarize` 一旦被支持，
纯文字条目（`url` 为空）会先撞「无链接可抓」并标 `done`——摘要没写、
状态显示已完成。现在 URL 判空收进 `steps.run_fetch`，结构上就不可能误伤。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Coroutine
from datetime import datetime
from typing import Any, Callable

from kb.ai.outcomes import SummarizerPort
from kb.extract.router import ExtractOutcome
from kb.store import jobs as jobs_store
from kb.worker import ai_runtime, steps

log = logging.getLogger(__name__)

# 本包真正能执行的步骤。`ocr` / `index` 领到就退回队列（见模块 docstring）。
#
# `AI-10` 明说本轮**不把这两个步骤放进队列**：它们没实现，入队会让
# `mark_failed` 在第 3 次把每条内容标成「处理失败」，而摘要其实已经成功了。
# 这是「宁可少一个功能，不可谎报一个状态」的直接应用。
SUPPORTED_STEPS = ("fetch", "summarize", "tag")

# 一轮没活干时的等待秒数。太短会空转刷日志，太长会让新任务等太久。
IDLE_SECONDS = 2.0


def _db_now(conn: Any) -> datetime:
    """从**数据库**取当前时刻（带时区）。

    ## 为什么不能用 `datetime.now()`

    平板实测的事实（2026-11-27）：

    ```
    Python now().astimezone() = 2026-11-27 12:30:45+00:00   ← 容器时区 UTC
    DB TimeZone               = PRC
    DB now()                  = 2026-11-27 20:30:45+08:00   ← 数据库时区 UTC+8
    ```

    proot 容器里没有 `/etc/localtime`，Python 拿到 UTC；而 PG 用 PRC。
    两者不同意味着**同一个瞬间在两侧属于不同日期**。

    后果不是「时间显示差 8 小时」这么轻：`store/usage.month_key(now)` 用它算
    「本月」键，而 `usage.add` 的 `updated_at` 用 SQL 的 `now()`。每月 1 日
    00:00–08:00（PRC）这 8 小时窗口里，两边算出的月份**不同** ——
    闸门去读 `ai_usage_2026-11`，记账写进 `ai_usage_2026-12`，
    闸门读到 0 → **预算在每月开头 8 小时完全不生效**。

    这类 bug 每月出现一次、每次 8 小时，且没有任何日志。所以月份口径
    必须只有一个权威源：**数据库自己的 `now()`**。

    ## 为什么不是「部署时设 TZ=Asia/Shanghai」

    那要求部署侧永远配对，而 `AGENTS.md` 的立场是「能机器执行的就不靠约定」。
    从库里取 `now()` 让口径天然一致，不依赖任何环境变量。
    """
    rows = conn.run("select now()")
    value = rows[0][0]
    if isinstance(value, datetime):
        return value
    raise TypeError(f"数据库 now() 返回的不是 datetime：{type(value).__name__}")


def _local_now() -> datetime:
    """进程本地时间。**只在拿不到数据库连接时兜底**，正常路径不用它。"""
    return datetime.now().astimezone()


def run_once(
    pool: Any,
    extractor: Callable[[str], Coroutine[Any, Any, ExtractOutcome]],
    ai_client: SummarizerPort | None = None,
) -> bool:
    """处理一条任务。做了事返回 True，队列空返回 False。

    `ai_client` 加在**第三个位置且默认 `None`**（`§4.2.1`）：现有 11 处
    `loop.run_once(pool, extractor)` 两个位置参数的调用**一行都不用改**。
    改成必填或插在 `extractor` 之前都会让那 11 处红。

    所有异常都在这里收掉：调用方（`run_forever`）的循环不能因为一条坏数据退出。
    唯一会往外抛的是「数据库本身连不上」——那种情况继续转也没意义。
    """
    with pool.connection() as conn:
        # 先捞回被 kill 的 worker 留下的 running 任务，再做新的
        jobs_store.recover_stale(conn)

        job = jobs_store.claim_one(conn)
        if job is None:
            return False

        job_id = int(str(job["id"]))
        item_id = int(str(job["itemId"]))
        step = str(job["step"])

        if step not in SUPPORTED_STEPS:
            # 未实现的步骤：退回队列而不是标完成。标完成会让条目显示「已完成」
            # 但摘要/标签都是空的，属于谎报。
            jobs_store.mark_failed(
                conn, job_id, f"步骤 {step} 尚未实现（后续轮次才有），先退回队列"
            )
            return True

        # 分派在 URL 判空之前（AI-11）。每个分支自己决定要不要 URL。
        try:
            if step == "fetch":
                steps.run_fetch(conn, item_id, job_id, extractor=extractor)
            elif step == "summarize":
                steps.run_summarize(
                    conn,
                    item_id,
                    job_id,
                    ai_client=ai_client,
                    runtime=ai_runtime,
                    # 月份/日历口径以数据库为准（见 _db_now 的注释）。
                    # 这条查的是 `select now()`，与 PG 自己记账用的同一口径，
                    # 所以「闸门读哪个月」与「记账写哪个月」永不脱节。
                    now=_db_now(conn),
                )
            else:  # step == "tag"
                steps.run_tag(conn, item_id, job_id)
        except Exception as exc:  # noqa: BLE001 单条坏数据不能打死整个 worker
            reason = f"{type(exc).__name__}: {exc}"
            log.exception("步骤执行异常 item=%s job=%s step=%s", item_id, job_id, step)
            jobs_store.mark_failed(conn, job_id, reason)
        return True


def run_forever(
    pool: Any,
    extractor: Callable[[str], Coroutine[Any, Any, ExtractOutcome]],
    idle_seconds: float = IDLE_SECONDS,
    on_tick: Callable[[], None] | None = None,
    ai_client: SummarizerPort | None = None,
) -> None:
    """一直跑。`on_tick` 每轮调一次，用来写心跳。

    `ai_client` 放在 `on_tick` **之后**：`kb/worker/__main__.py` 用关键字传
    `idle_seconds`，而把 `ai_client` 插在 `extractor` 之后（第三个位置）会让
    `run_forever(pool, extractor, 2.0)` 这类写法把浮点数当客户端传进去。

    队列空时 sleep，不是忙等——忙等会把 CPU 占满且日志刷屏。
    """
    while True:
        try:
            handled = run_once(pool, extractor, ai_client)
        except Exception:  # noqa: BLE001 数据库级异常记一笔再继续，不让进程退出
            log.exception("worker 单轮失败，2 秒后重试")
            handled = True  # 避免立刻进入 sleep=False 的紧循环

        if on_tick is not None:
            try:
                on_tick()
            except Exception:  # noqa: BLE001 心跳写失败不该影响干活
                log.exception("心跳写入失败")

        if not handled:
            time.sleep(idle_seconds)

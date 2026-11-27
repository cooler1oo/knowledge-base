"""worker 主循环：领一条、做一条、标一条。

不做的事（有意）：

- **不并发**。单人自用，一条一条做足够；并发会引入"两个 worker 抢同一条"的
  复杂度，而 `FOR UPDATE SKIP LOCKED` 只是为了将来真开多个实例时不出错。
- **不在这里实现 summarize / ocr / tag / index**。那是 R5 之后的事。
  现阶段领到这些步骤就标回 pending 并写明「尚未实现」——标成 done 是谎报，
  条目会显示「已完成」但摘要标签全是空的。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Coroutine
from typing import Any, Callable

from kb.extract.router import ExtractOutcome
from kb.store import items as items_store
from kb.store import jobs as jobs_store

log = logging.getLogger(__name__)

# 本包真正能执行的步骤。其余步骤领到就退回队列（见模块 docstring）。
SUPPORTED_STEPS = ("fetch",)

# 一轮没活干时的等待秒数。太短会空转刷日志，太长会让新任务等太久。
IDLE_SECONDS = 2.0


def _outcome_to_kwargs(item_url: str, outcome: ExtractOutcome) -> dict[str, Any]:
    """把抓取结果翻成 `items.apply_extract` 要的参数。

    翻译放在 worker 而不是 store：store 不该知道怎么从一个 URL 抓东西，
    也不该知道 `ExtractOutcome` 这个类型存在。两边只靠「一条记录的字段」
    这个朴素契约耦合。
    """
    if outcome.ok and outcome.data is not None:
        data = outcome.data
        return {
            "ok": True,
            "kind": data.kind,
            "title": data.title,
            "content_text": data.content_text,
            "platform": data.platform,
            "media": [
                {
                    "url": m.url,
                    "ext": m.ext,
                    "durationSec": m.duration_sec,
                }
                for m in data.media
            ],
            "error": "",
        }
    # 抓不到时也算「处理完了」，但要把原因写下来，界面才能显示「处理失败，可重试」
    return {"ok": False, "error": outcome.error or "抓取失败（无原因）"}


def _url_of(conn: Any, item_id: int) -> str:
    """取条目的 URL。抓 fetch 步必须有它，取不到就只能算失败。"""
    detail = items_store.get_item(conn, item_id)
    if detail is None:
        return ""
    return detail.url or ""


def run_once(
    pool: Any,
    extractor: Callable[[str], Coroutine[Any, Any, ExtractOutcome]],
) -> bool:
    """处理一条任务。做了事返回 True，队列空返回 False。

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
                conn, job_id, f"步骤 {step} 尚未实现（R5 之后才有），先退回队列"
            )
            return True

        url = _url_of(conn, item_id)
        if not url:
            # 纯文字条目没有 URL，fetch 步无意义。这是正常情况，不是失败。
            items_store.apply_extract(
                conn, item_id, ok=False, error="无链接可抓（纯文字条目）"
            )
            jobs_store.mark_done(conn, job_id)
            return True

        try:
            outcome: ExtractOutcome = asyncio.run(extractor(url))
        except Exception as exc:  # noqa: BLE001 抓取层的任何异常都要收成任务失败，见模块 docstring
            reason = f"{type(exc).__name__}: {exc}"
            log.warning("抓取异常 item=%s url=%s: %s", item_id, url, reason)
            items_store.apply_extract(conn, item_id, ok=False, error=reason)
            jobs_store.mark_failed(conn, job_id, reason)
            return True

        items_store.apply_extract(conn, item_id, **_outcome_to_kwargs(url, outcome))

        if outcome.ok:
            jobs_store.mark_done(conn, job_id)
            log.info("抓取完成 item=%s kind=%s", item_id, outcome.kind)
        else:
            jobs_store.mark_failed(conn, job_id, outcome.error or "抓取失败")
            log.info("抓取失败 item=%s: %s", item_id, outcome.error)
        return True


def run_forever(
    pool: Any,
    extractor: Callable[[str], Coroutine[Any, Any, ExtractOutcome]],
    idle_seconds: float = IDLE_SECONDS,
    on_tick: Callable[[], None] | None = None,
) -> None:
    """一直跑。`on_tick` 每轮调一次，用来写心跳。

    队列空时 sleep，不是忙等——忙等会把 CPU 占满且日志刷屏。
    """
    while True:
        try:
            handled = run_once(pool, extractor)
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

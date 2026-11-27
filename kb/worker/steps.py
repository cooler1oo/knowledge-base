"""三个步骤各自的分支：`run_fetch` / `run_summarize` / `run_tag`。

**为什么从 `loop.py` 拆出来**（设计 §3.1）：`loop.py` 原本是「领 → 判 → 做 → 标」
一体。加两个步骤后它会变成三个 if 分支塞在 60 行里。拆出来才能让
「URL 判空只属于 `fetch`」在**结构上**成立——`AI-11` 的根因就是判空写在分派之前，
对所有已支持的步骤一视同仁。

三个入口的签名按 `§4.2.1` 定死，`ai_client` **只出现在 `run_summarize` 里**：

```python
run_fetch(conn, item_id, job_id, *, extractor)
run_summarize(conn, item_id, job_id, *, ai_client, runtime, now)
run_tag(conn, item_id, job_id)
```

这是「11 处现有 `run_once` 调用一行都不用改」的机制：`run_fetch` 与 `run_tag`
不接收 `ai_client`，所以 `ai_client=None` 对它们完全无影响。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from kb.ai import prompts
from kb.ai.outcomes import (
    AiOutcome,
    SummarizeFailed,
    SummarizeOk,
    SummarizeSkipped,
)
from kb.ai.validate import validate_tags
from kb.store import items as items_store
from kb.store import jobs as jobs_store
from kb.store import tags as tags_store
from kb.worker import ai_runtime

log = logging.getLogger(__name__)

#: 数据库连接的**类型别名**。理由见 `ai_runtime.Conn`：`layer_rules.toml`
#: 只允许 `kb/store/` 出现驱动 import，而本层只是把 `conn` 转手传给 store 函数。
Conn = Any


# ── fetch ──────────────────────────────────────────────────────


def _outcome_to_kwargs(item_url: str, outcome: Any) -> dict[str, Any]:
    """把抓取结果翻成 `items.apply_extract` 要的参数。

    翻译放在 worker 而不是 store：store 不该知道怎么从一个 URL 抓东西，
    也不该知道 `ExtractOutcome` 这个类型存在。两边只靠「一条记录的字段」
    这个朴素契约耦合。
    """
    del item_url  # 签名保留是为了将来「按 URL 分支」时有位置可放
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
                    "url": media.url,
                    "ext": media.ext,
                    "durationSec": media.duration_sec,
                }
                for media in data.media
            ],
            "error": "",
        }
    # 抓不到时也算「处理完了」，但要把原因写下来，界面才能显示「处理失败，可重试」
    return {"ok": False, "error": outcome.error or "抓取失败（无原因）"}


def run_fetch(
    conn: Conn,
    item_id: int,
    job_id: int,
    *,
    extractor: Any,
) -> None:
    """抓取一步。**URL 判空只在这里做**（`AI-11`）。

    成功判据是 `ExtractOutcome.ok`（`AI-07`）。成功时：

    1. `apply_extract` 写回 `items`
    2. `mark_done`
    3. `enqueue(item_id, "summarize")`

    **顺序不能反**（设计 §0 第 9 条）：反过来在 `mark_done` 失败时会留下一条
    已完成的上游 + 一条待办的下游，重跑上游会产生两条 `pending`，被
    `jobs_pending_uniq` 挡成一条——结果正确但排障时看不出发生过重跑。
    按本顺序最坏情况是「上游 done、下游缺失」，`C1` 会在 60 秒内直接暴露。
    """
    detail = items_store.get_item(conn, item_id)
    url = "" if detail is None else (detail.url or "")

    if not url:
        # 纯文字条目没有 URL，fetch 步无意义。这是正常情况，不是失败。
        items_store.apply_extract(
            conn, item_id, ok=False, error="无链接可抓（纯文字条目）"
        )
        jobs_store.mark_done(conn, job_id)
        return

    try:
        outcome: Any = asyncio.run(extractor(url))
    except Exception as exc:  # noqa: BLE001 抓取层的任何异常都要收成任务失败
        reason = f"{type(exc).__name__}: {exc}"
        log.warning("抓取异常 item=%s url=%s: %s", item_id, url, reason)
        items_store.apply_extract(conn, item_id, ok=False, error=reason)
        jobs_store.mark_failed(conn, job_id, reason)
        return

    items_store.apply_extract(conn, item_id, **_outcome_to_kwargs(url, outcome))

    if not outcome.ok:
        jobs_store.mark_failed(conn, job_id, outcome.error or "抓取失败")
        log.info("抓取失败 item=%s: %s", item_id, outcome.error)
        # C2：失败时不入队 summarize——没有内容可摘要
        return

    jobs_store.mark_done(conn, job_id)
    jobs_store.enqueue(conn, item_id, "summarize")
    log.info(
        "抓取完成 item=%s job=%s step=fetch kind=%s", item_id, job_id, outcome.kind
    )


# ── summarize ─────────────────────────────────────────────────


def run_summarize(
    conn: Conn,
    item_id: int,
    job_id: int,
    *,
    ai_client: Any,
    runtime: Any = ai_runtime,
    now: datetime,
) -> None:
    """摘要 + 标签名（一次模型调用，`AI-01`）。

    **判定顺序定死**（设计 §4.2.1，写反会报错报错因）：

    ```
    ai_client is None ?
      ├─ 否 → 进 AI 层（配置已在 load_config 阶段检查过，保证齐备）
      └─ 是 → load_config(conn)
                ├─ 返回 AiConfig（配置齐备）→ defer + "AI 客户端未装配"（装配 bug）
                └─ 返回 None（配置缺失）    → defer + "AI 未配置（缺 xxx）"（正常推迟）
    ```

    先判 `ai_client` 再查配置：「配置齐备但没客户端」只可能来自装配错误，
    而「配置缺失」无论客户端在不在都是正常推迟。两者混成一句 error，
    排障时无法区分「该去填配置」还是「该去查 `__main__.py`」。

    ⚠️ 注意「配置缺失」这条路径**即使在注入了真实例时也走**（否则 AI 层会拿到
    空 base_url 白发一次请求）。所以这里的顺序是：先查配置，再判客户端。
    """
    # ① 配置：读一次，两条 defer 分支都要用
    config = runtime.load_config(conn)
    if config is None:
        _defer_not_configured(conn, job_id, runtime.load_values(conn))
        return

    # ② 客户端：配置齐备却没客户端 = 装配 bug
    if ai_client is None:
        _defer_no_client(conn, job_id)
        return

    # ③ 取内容，决定这次拿什么文本
    detail = items_store.get_item(conn, item_id)
    if detail is None:
        jobs_store.mark_failed(conn, job_id, "条目不存在，无法摘要")
        return

    choice = prompts.choose_input(detail.title, detail.content_text)
    if choice.kind == "none":
        # B15：正文标题都空 → 零次调用，写 summarySkipped='no_text' 并标 done
        items_store.mark_summary_skipped(conn, item_id, "no_text")
        jobs_store.mark_done(conn, job_id)
        log.info("跳过摘要 item=%s job=%s step=summarize basis=none", item_id, job_id)
        return

    # ④ 预算闸门：用**实际送出去的字符数**算预估，不是正文长度
    messages = prompts.build_messages(
        title=detail.title,
        text=choice.text,
        vocabulary=(),
        basis=choice.basis,
    )
    chars_in = prompts.estimate_input_chars(messages)
    decision = runtime.check_budget(conn, config, now, chars_in)
    if not decision.allowed:
        if decision.retry_at is not None:
            jobs_store.defer_until(
                conn, job_id, decision.retry_at, runtime.budget_defer_reason(decision)
            )
        else:
            jobs_store.defer(
                conn,
                job_id,
                ai_runtime.DEFER_SECONDS_NOT_CONFIGURED,
                runtime.budget_defer_reason(decision),
            )
        return

    # ⑤ 调模型
    vocabulary = tags_store.vocabulary(conn)
    _bind_client_config(ai_client, config)
    try:
        outcome: AiOutcome = ai_client.summarize(detail.title, choice.text, vocabulary)
    except Exception as exc:  # noqa: BLE001 单条异常必须收成任务失败，不让循环崩
        reason = f"{type(exc).__name__}: {exc}"
        log.warning("AI 调用异常 item=%s job=%s: %s", item_id, job_id, reason)
        jobs_store.mark_failed(conn, job_id, reason)
        return

    _apply_summarize_outcome(
        conn, item_id, job_id, outcome, choice, config, runtime, now
    )


def _defer_not_configured(conn: Conn, job_id: int, values: dict[str, object]) -> None:
    """`AI-04`：未配置 → 推迟 30 分钟，`attempts` 不变，零次请求。

    错误消息里**要写出缺哪个键**（`E1` 断言含 `ai_api_key`）：用户看到
    「AI 未配置」还得自己去猜是哪个键，而 `§8.2` 定的形态是「缺 ai_api_key」。
    写这句 error 才能让用户在详情页看到「为什么还没出摘要」，而
    `derive_status` 只看 `state`，所以 `pending` + `error` 不会显示成失败。
    """
    jobs_store.defer(
        conn,
        job_id,
        ai_runtime.DEFER_SECONDS_NOT_CONFIGURED,
        ai_runtime.missing_keys_message(values),
    )
    log.info("AI 未配置，推迟 job=%s", job_id)


def _bind_client_config(ai_client: Any, config: Any) -> None:
    """把每轮现读的配置装进客户端。

    为什么不在启动时构造一个带配置的客户端：配置存在 `settings` 表里，
    用户可以随时改（`E4` 就是「之后补上三键配置」）。客户端持有启动那一刻的
    快照，用户改完配置必须重启 worker 才生效——那是一个静默的坑。

    只在客户端**确实持有**配置属性时替换（测试里的 `FakeSummarizer` 没有），
    所以这一句对假实现是空操作。
    """
    if hasattr(ai_client, "config"):
        ai_client.config = config


def _defer_no_client(conn: Conn, job_id: int) -> None:
    """配置齐备但 `ai_client` 为 `None`：装配 bug，不是「未配置」。

    写「未装配」而不是「未配置」，才能让人看出是漏传参数而不是没填配置。
    """
    jobs_store.defer(
        conn,
        job_id,
        ai_runtime.DEFER_SECONDS_NOT_CONFIGURED,
        "已推迟：AI 客户端未装配（配置已存在，检查 worker 启动时是否传 ai_client）",
    )
    log.warning("AI 客户端未装配 job=%s（配置齐备）", job_id)


def _apply_summarize_outcome(
    conn: Conn,
    item_id: int,
    job_id: int,
    outcome: AiOutcome,
    choice: prompts.InputChoice,
    config: Any,
    runtime: Any,
    now: datetime,
) -> None:
    """三种结果形态各自的动作（`§4.3`）。

    `SummarizeOk`：记账 → 写 summary → 写 aiTags → 标 done → 入队 tag
    `SummarizeSkipped`：写 summarySkipped → 标 done（**不入队 tag**）
    `SummarizeFailed`：按类别落地（`runtime.apply_failure`）

    顺序里 `mark_done` 在 `enqueue` 之前，理由与 `run_fetch` 相同。
    """
    if isinstance(outcome, SummarizeOk):
        # 记账在写库之前：钱已经花出去了，先记下来。
        # 记账失败不让任务失败——重跑会再花一次钱。
        try:
            runtime.record_usage(conn, config, usage=outcome.usage, now=now)
        except Exception:  # noqa: BLE001 记账失败不该让摘要变成失败任务
            log.exception("记账失败 item=%s job=%s", item_id, job_id)

        items_store.apply_summary(conn, item_id, outcome.summary, choice.basis)
        items_store.set_ai_tags_raw(conn, item_id, outcome.tags)
        jobs_store.mark_done(conn, job_id)
        jobs_store.enqueue(conn, item_id, "tag")
        log.info(
            "摘要完成 item=%s job=%s step=summarize basis=%s tags=%s",
            item_id,
            job_id,
            choice.basis,
            len(outcome.tags),
        )
        return

    if isinstance(outcome, SummarizeSkipped):
        items_store.mark_summary_skipped(conn, item_id, outcome.reason)
        jobs_store.mark_done(conn, job_id)
        log.info(
            "摘要跳过 item=%s job=%s step=summarize reason=%s",
            item_id,
            job_id,
            outcome.reason,
        )
        return

    assert isinstance(outcome, SummarizeFailed)
    log.warning(
        "摘要失败 item=%s job=%s category=%s status=%s",
        item_id,
        job_id,
        outcome.category.value,
        outcome.http_status,
    )
    runtime.apply_failure(conn, job_id, outcome)


# ── tag ───────────────────────────────────────────────────────


def run_tag(conn: Conn, item_id: int, job_id: int) -> None:
    """读 `props.aiTags` → 校验词表 → 整体替换 `item_tags` → 标 done。

    **不接受 `ai_client`**（设计 §4.2.1 的 C 路径）：`aiTags` 是 `summarize`
    那次调用已经带回来的，这一步全程不调模型。所以客户端缺席时它照常执行——
    若 defer 它，「已成功拿到的标签」会被挂起 30 分钟，而这期间
    `items.summary` 已经写好了，界面显示「有摘要但没标签」，用户无法理解原因。

    **成功后不入队 `index`**（`AI-10`）：`index` 没实现，入队会让
    `mark_failed` 在第 3 次把每条内容标成「处理失败」。
    """
    raw = items_store.get_ai_tags_raw(conn, item_id)
    vocabulary = tags_store.vocabulary(conn)
    validated = validate_tags(raw, vocabulary)

    tags_store.replace_ai_tags(conn, item_id, validated.tag_ids)
    if validated.ambiguous:
        items_store.set_ai_tag_ambiguous(conn, item_id, True)
    jobs_store.mark_done(conn, job_id)
    log.info(
        "标签落库 item=%s job=%s step=tag tags=%s ambiguous=%s",
        item_id,
        job_id,
        len(validated.tag_ids),
        validated.ambiguous,
    )

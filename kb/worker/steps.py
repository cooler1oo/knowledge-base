"""四个步骤各自的分支：`run_fetch` / `run_summarize` / `run_tag` / `run_index`。

**为什么从 `loop.py` 拆出来**（设计 §3.1）：`loop.py` 原本是「领 → 判 → 做 → 标」
一体。加两个步骤后它会变成三个 if 分支塞在 60 行里。拆出来才能让
「URL 判空只属于 `fetch`」在**结构上**成立——`AI-11` 的根因就是判空写在分派之前，
对所有已支持的步骤一视同仁。

四个入口的签名按 `§4.2.1` 定死，`ai_client` **只出现在 `run_summarize` 里**：

```python
run_fetch(conn, item_id, job_id, *, extractor)
run_summarize(conn, item_id, job_id, *, ai_client, runtime, now)
run_tag(conn, item_id, job_id)
run_index(conn, item_id, job_id, *, indexer, upstream)
```

这是「11 处现有 `run_once` 调用一行都不用改」的机制：`run_fetch` 与 `run_tag`
不接收 `ai_client`，所以 `ai_client=None` 对它们完全无影响。
`indexer` 同理只在 `run_index` 里出现。

## 入队链路（设计 §4.4）

```
入库 → fetch → summarize → tag → index
         │         │          │
         │         └─ 成功/跳过都入队 index
         └─ 成功入队 summarize      成功入队 index
```

`enqueue` 的 `on conflict (item_id, step) where state in ('pending','running')
do nothing` 让两处 `index` 入队塌成一条。
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
from kb.media import persist, plan
from kb.search.config import (
    DEFER_MEILI_DOWN_SEC,
    DEFER_MEILI_UNAUTHORIZED_SEC,
    UPSTREAM_SOON_SEC,
    UPSTREAM_WAIT_SEC,
)
from kb.search.models import MeiliRejected, MeiliUnauthorized, MeiliUnavailable
from kb.store import attachments as attachments_store
from kb.store import items as items_store
from kb.store import jobs as jobs_store
from kb.store import search as search_store
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

    **`thumbUrl` / `width` / `height` 三个字段必须传**（设计 §0.2 缺口二）：
    原来的实现只传 `url` / `ext` / `durationSec`，于是封面的 URL 在 worker
    边界上就已经丢了——即便下游写对了，`thumb_url` 也到不了
    （`kb/media/plan.py` 靠 `thumbUrl` 合成 `role='cover'`）。
    真机实测抖音视频的 `media` 只有一条 `role='video'`，封面只在 `thumb_url` 里，
    漏传这一个字段的后果是**封面一张都下不到**。
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
            "media": [_media_to_dict(media) for media in data.media],
            "error": "",
        }
    # 抓不到时也算「处理完了」，但要把原因写下来，界面才能显示「处理失败，可重试」
    return {"ok": False, "error": outcome.error or "抓取失败（无原因）"}


#: 抓取层 `MediaItem` → 落库/下载层认的 dict。**camelCase 键**与
#: `store.items.apply_extract` 收的 `durationSec` 保持一致（worker 是唯一翻译者，
#: 两边命名必须对齐，否则「传了但没读到」这种错在两边都不会报）。
#:
#: `role` **必须在里面**：`kb/media/plan.py` 靠它区分「这条是图片还是视频」，
#: 而视频的 url 本轮**不下**（PRD M4-07 P2）。漏掉 role 的后果是
#: `_image_urls` 一条都匹配不到 → 图文内容永远没有封面和配图，而且
#: 日志里只有一句「specs 为空」，看起来像「这条内容本来就没图」。
#: **真机验证抓到的**：抖音图文（`v.douyin.com/5gjBAuYo7PE`）在这个 bug 下
#: 产出的 `specs` 是空的。
_MEDIA_KEYS = ("role", "url", "ext", "thumbUrl", "width", "height", "durationSec")


def _media_to_dict(media: Any) -> dict[str, object]:
    """单条媒体 → dict。`thumbUrl` 为 `None` 时**不放进 dict**。

    不放 `None` 而不是放：`plan.py` 用 `media.get('thumbUrl')` 判断有无封面，
    `None` 与「键不存在」在它那儿等价，但「不出现」让 `props` 的 jsonb 更小、
    也让「这条内容到底有没有封面」在库里读得出来。
    """
    out: dict[str, object] = {
        "role": media.role,
        "url": media.url,
        "ext": media.ext,
        "width": media.width,
        "height": media.height,
        "durationSec": media.duration_sec,
    }
    if media.thumb_url:
        out["thumbUrl"] = media.thumb_url
    return {key: out[key] for key in _MEDIA_KEYS if key in out}


def run_fetch(
    conn: Conn,
    item_id: int,
    job_id: int,
    *,
    extractor: Any,
    media_stage: Any = None,
) -> None:
    """抓取一步。**URL 判空只在这里做**（`AI-11`）。

    成功判据是 `ExtractOutcome.ok`（`AI-07`）。成功时：

    1. `apply_extract` 写回 `items`（元数据先落库——即使后面媒体全失败，
       这条内容已经可用）
    2. **媒体阶段**：`plan_attachments` → `store_media` → `replace_for_item`
    3. `mark_done`
    4. `enqueue(item_id, "summarize")`

    **顺序不能反**（设计 §0 第 9 条）：反过来在 `mark_done` 失败时会留下一条
    已完成的上游 + 一条待办的下游，重跑上游会产生两条 `pending`，被
    `jobs_pending_uniq` 挡成一条——结果正确但排障时看不出发生过重跑。
    按本顺序最坏情况是「上游 done、下游缺失」，`C1` 会在 60 秒内直接暴露。

    ## 媒体阶段失败怎么办

    **fetch 仍算成功。** 三层落地：单条媒体失败进 `props.mediaError`、
    全部失败同上、整个阶段抛未预期异常也同上且 `log.exception`。
    理由见 `items_store.set_media_props` 的 docstring：`derive_status` 看到任意
    job failed 就把条目显示成「处理失败」，而一张封面没下下来不该让用户去点重试。

    ## `media_stage` 参数

    类型是 `Any`（默认 `None` = 不下载媒体）而不是 `kb.media.MediaStage`：
    本层只把它转手传给 `kb.media.persist.store_media`，不需要知道
    `Downloader` / `ImageEncoder` / `Resolver` 的存在。`None` 时整个阶段跳过
    ——这让 11 处现有 `run_once(pool, extractor)` 的调用**一行都不用改**，
    也不会突然开始联网下载。
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

    kwargs = _outcome_to_kwargs(url, outcome)
    items_store.apply_extract(conn, item_id, **kwargs)

    if not outcome.ok:
        jobs_store.mark_failed(conn, job_id, outcome.error or "抓取失败")
        # C2：失败时不入队 summarize——没有内容可摘要。
        # 同时清掉上一次的封面：那张图属于上一版内容，留着会让用户看到错图。
        items_store.clear_media_props(conn, item_id)
        log.info("抓取失败 item=%s: %s", item_id, outcome.error)
        return

    _download_media(conn, item_id, kwargs.get("media") or [], media_stage=media_stage)

    jobs_store.mark_done(conn, job_id)
    jobs_store.enqueue(conn, item_id, "summarize")
    log.info(
        "抓取完成 item=%s job=%s step=fetch kind=%s", item_id, job_id, outcome.kind
    )


def _download_media(
    conn: Conn,
    item_id: int,
    media_items: list[dict[str, object]],
    *,
    media_stage: Any,
) -> None:
    """阶段二：规划 → 下载落盘 → 落库 → 写 props。**任何失败不污染 `jobs`。**

    ## 四步顺序

    1. `plan_attachments`（纯函数，不联网）
    2. `store_media`（联网 + 写盘，**不碰数据库**）
    3. `replace_for_item`（写 attachments，单条 WITH 语句）
    4. `primary_media_id` + `set_media_props`（写 items）

    第 2 步抛未预期异常时第 3/4 步跳过，但 `props.mediaError` 仍要写
    ——「整段炸了」与「单条 404」对用户是同一件事：没有封面可用。

    ## `media_stage is None` 时什么都不做

    这是「装配件未注入」。理由同 `run_fetch` 的 `media_stage` 参数说明：
    现有 11 处调用不该因为本轮新增媒体能力就突然开始联网。
    """
    if media_stage is None:
        return

    specs = plan.plan_attachments(media_items, kind="")
    if not specs:
        # 没有封面也没有配图。**显式清空**上一次的媒体状态：内容可能从
        # 视频改成了纯文字，旧封面留着会让卡片显示一张与内容无关的图。
        items_store.set_media_props(
            conn, item_id, primary_media=None, media_error="", media_truncated=0
        )
        return

    try:
        result = media_stage.run(specs, item_id=item_id)
    except Exception:  # noqa: BLE001 媒体阶段整段异常不污染 jobs，见 docstring
        log.exception("媒体阶段异常 item=%s", item_id)
        items_store.set_media_props(
            conn,
            item_id,
            primary_media=None,
            media_error="媒体下载阶段异常（详见 worker 日志）",
            media_truncated=0,
        )
        return

    if result.stored:
        written = attachments_store.replace_for_item(conn, item_id, list(result.stored))
        log.info(
            "媒体落库 item=%s 写入 %d 行（跳过 %d 条）",
            item_id,
            written,
            len(result.skipped),
        )
    primary = attachments_store.primary_media_id(conn, item_id)
    items_store.set_media_props(
        conn,
        item_id,
        primary_media=primary,
        media_error=persist.summarize_skipped(result.skipped),
        media_truncated=plan.dropped_images(media_items, specs),
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
        # 设计 §4.4：跳过也是终态，也要入队 index。
        #
        # 这条内容一个字都没有，凭文本搜不到它——但它必须出现在
        # **浏览器模式**（空 `q`，按 `saved_at_ts` 倒序）里。不入队的话
        # 用户刚存进来的条目在列表里看不到，界面显示「已完成」却是空的。
        jobs_store.enqueue(conn, item_id, "index")
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
        # 设计 §4.4：`summarize` 成功后入队 `index`。
        # 与 `tag` 成功后那次入队由 `jobs_pending_uniq` 塌成一条。
        jobs_store.enqueue(conn, item_id, "index")
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
        # 设计 §4.4：**跳过也是终态**——这条内容的摘要不会再有。
        # 不入队的话纯文字空内容永远不进索引，用户搜不到他刚存进来的东西。
        jobs_store.enqueue(conn, item_id, "index")
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
    """读 `props.aiTags` → 校验词表 → 整体替换 `item_tags` → 标 done → 入队 index。

    **不接受 `ai_client`**（设计 §4.2.1 的 C 路径）：`aiTags` 是 `summarize`
    那次调用已经带回来的，这一步全程不调模型。所以客户端缺席时它照常执行——
    若 defer 它，「已成功拿到的标签」会被挂起 30 分钟，而这期间
    `items.summary` 已经写好了，界面显示「有摘要但没标签」，用户无法理解原因。

    **成功后入队 `index`**（R6 起，设计 §4.4）：标签是这一步才写进
    `item_tags` 的，索引里的 `tags` 字段依赖它。与 `summarize` 那次入队
    由 `jobs_pending_uniq` 塌成一条，所以不会产生两条 `index` 任务。
    """
    raw = items_store.get_ai_tags_raw(conn, item_id)
    vocabulary = tags_store.vocabulary(conn)
    validated = validate_tags(raw, vocabulary)

    tags_store.replace_ai_tags(conn, item_id, validated.tag_ids)
    if validated.ambiguous:
        items_store.set_ai_tag_ambiguous(conn, item_id, True)
    jobs_store.mark_done(conn, job_id)
    jobs_store.enqueue(conn, item_id, "index")
    log.info(
        "标签落库 item=%s job=%s step=tag tags=%s ambiguous=%s",
        item_id,
        job_id,
        len(validated.tag_ids),
        validated.ambiguous,
    )


# ── index ─────────────────────────────────────────────────────


def run_index(
    conn: Conn,
    item_id: int,
    job_id: int,
    *,
    indexer: Any = None,
    upstream: tuple[str, ...] = (),
) -> None:
    """把一条内容写进检索索引。**失败分级是本步骤最重要的部分**（设计 §3.4）。

    ## 五步顺序

    1. 判上游阻塞（`jobs.blocking_upstream`）。有 → `defer 30 秒` + return。
    2. `search_store.index_source_row` 读一条文档（items 行 + 标签名数组）。
       返回 `None`（条目不存在或已软删）→ 标 done 什么都不写。
    3. `indexer.upsert([doc])` → Meili 接收（**不等任务**）。
    4. `jobs.mark_done`。

    **第 1 步之后才读数据**：上一步若推迟，读出来的就是过期快照。
    先判后读能保证写进索引的字段是最新的。

    ## 失败分级（判据写死在设计 §10，违反会返工）

    | 异常 | 处置 | 为什么 |
    |---|---|---|
    | `MeiliUnavailable` | `defer 60 秒`（不计 attempts） | 服务挂了不是这条内容的错，重试几次通常就好 |
    | `MeiliUnauthorized` | `defer 600 秒` + error 含 `KB_MEILI_KEY` | 密钥填错是可修的配置问题。判死之后要手工改库才能恢复，且用户永远看不到「去改密钥」 |
    | `MeiliRejected` | `mark_failed` | 请求体或索引设置写错了，重试 3 次也照样错。编程错误不该消耗用户的时间 |
    | 其它异常 | 抛出去，由 `run_once` 收成 `mark_failed` | 未预期错误应留下 traceback |

    **一律不用 `mark_failed` 处理「Meili 挂了」**：三次之后这条内容被标成
    「处理失败」，用户会对一堆本来没事的条目点重试，而真因是服务没起。

    ## `indexer is None` 时

    defer 而不是 mark_done。`None` 意味着 `__main__.py` 漏传装配件——
    那是装配 bug（与 `run_summarize` 的「AI 客户端未装配」同一类）。
    写「已推迟：索引器未装配」才能让人看出是漏传参数而不是 Meili 的问题。
    """
    blocked = jobs_store.blocking_upstream(
        conn,
        item_id,
        job_id,
        steps=upstream,
        soon_seconds=UPSTREAM_SOON_SEC,
    )
    if blocked is not None:
        # 推迟不计次（`defer` 的 SQL 里不出现 attempts）。
        # 上游正在跑时写索引会写出半成品；30 秒后上游的典型状态是已完成。
        jobs_store.defer(
            conn, job_id, UPSTREAM_WAIT_SEC, f"已推迟：等待 {blocked} 完成"
        )
        log.info("索引推迟 item=%s job=%s 等待 %s", item_id, job_id, blocked)
        return

    if indexer is None:
        jobs_store.defer(
            conn,
            job_id,
            UPSTREAM_WAIT_SEC,
            "已推迟：索引器未装配（检查 worker 启动时是否传 indexer）",
        )
        log.warning("索引器未装配 item=%s job=%s", item_id, job_id)
        return

    doc = search_store.index_source_row(conn, item_id)
    if doc is None:
        # 条目不存在或已软删。**标 done 而不是 failed**：这不是错误，
        # 是「这条内容不该在索引里」。rebuild 的差集会把它从索引里清掉。
        jobs_store.mark_done(conn, job_id)
        log.info("条目不存在或已删，跳过索引 item=%s job=%s", item_id, job_id)
        return

    try:
        indexer.upsert([doc])
    except MeiliUnauthorized as exc:
        # 600 秒而不是判死：密钥填错是配置问题，改完配置就该恢复。
        # error 里必须写 `KB_MEILI_KEY`，否则用户看到「索引服务拒绝访问」
        # 还得自己猜是哪个配置。
        jobs_store.defer(
            conn,
            job_id,
            DEFER_MEILI_UNAUTHORIZED_SEC,
            f"已推迟：索引服务拒绝访问，检查 KB_MEILI_KEY（{exc}）",
        )
        log.warning("索引服务拒绝访问 item=%s job=%s：%s", item_id, job_id, exc)
        return
    except MeiliUnavailable as exc:
        jobs_store.defer(
            conn,
            job_id,
            DEFER_MEILI_DOWN_SEC,
            f"已推迟：索引服务不可达，稍后重试（{exc}）",
        )
        log.warning("索引服务不可达 item=%s job=%s：%s", item_id, job_id, exc)
        return
    except MeiliRejected as exc:
        # 编程错误：请求体或索引设置写错。重试 3 次也照样错。
        jobs_store.mark_failed(conn, job_id, f"索引服务拒绝了请求：{exc}")
        log.error("索引请求被拒 item=%s job=%s：%s", item_id, job_id, exc)
        return

    jobs_store.mark_done(conn, job_id)
    log.info(
        "索引完成 item=%s job=%s step=index tags=%s", item_id, job_id, len(doc.tags)
    )

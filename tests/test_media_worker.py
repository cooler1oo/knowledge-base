"""worker 的媒体阶段接线：翻译、不污染 jobs、装配件缺席时的行为。

**这个文件不在设计的文件清单里，是本轮补的**（偏离清单第 3 条）。理由是
真机验证抓到一个只在这个边界上存在的 bug：

> worker 翻译 media 时漏了 `role` 键 → `kb/media/plan.py` 的 `_image_urls`
> 一条都匹配不到 → 抖音图文的 `specs` 静默变成空列表。日志里只有一句
> 「specs 为空」，看起来像「这条内容本来就没图」。

那个 bug 在任何单侧的纯逻辑用例里都验不到：`plan` 的用例手工构造 dict（带 `role`），
`persist` 的用例直接收 `MediaSpec`。**只有「抓取层 → worker 翻译 → plan」这条
接缝上才暴露。** 所以这一组必须有。

## 四条守的东西

1. **`_outcome_to_kwargs` 必须带 `thumbUrl` / `role` / `width` / `height`**
   （设计 §0.2 缺口二）。漏 `thumbUrl` = 封面一张都下不到；
   漏 `role` = 图文一类内容整体没有封面。
2. **`media_stage is None` 时什么都不做**。现有 11 处 `run_once(pool, extractor)`
   不该因为本轮新增能力就突然联网下载。
3. **媒体阶段失败不污染 `jobs`**。`mark_done` 照常，错误只进 `props.mediaError`。
4. **`primary_media` 由 `primary_media_id` 的结果决定**，不是写死。
"""

from __future__ import annotations

from datetime import datetime

from kb.extract.normalize import ExtractResult, MediaItem
from kb.extract.router import ExtractOutcome
from kb.media.models import MediaResult, MediaSpec, SkippedMedia, StoredBlob
from kb.worker import steps


class _Conn:
    """记录所有 SQL 与参数的假连接。按 SQL 内容返回预设结果。"""

    def __init__(
        self,
        *,
        item_url: str | None = "https://v.douyin.com/abc/",
        cover_id: int | None = 42,
    ) -> None:
        self._item_url = item_url
        self._cover_id = cover_id
        self.seen: list[str] = []
        self.params: list[dict[str, object]] = []

    def run(self, sql: str, **params: object) -> list[list[object]]:
        text = " ".join(sql.split()).lower()
        self.seen.append(text)
        self.params.append(params)

        # jobs.counts（get_item 内部调它算处理状态）
        if "from jobs" in text and "count(*)" in text:
            return [[1, 0, 0, 0]]
        # items 详情：get_item 要 12 列
        if "from items i" in text and "i.id = :i" in text:
            if self._item_url is None:
                return []
            return [
                [
                    1,
                    "link",
                    self._item_url,
                    "标题",
                    None,
                    "v.douyin.com",
                    None,
                    None,
                    "unread",
                    datetime(2026, 11, 28),
                    None,
                    "{}",
                ]
            ]
        # attachments.primary_media_id
        if "from attachments" in text and "role = 'cover'" in text:
            return [] if self._cover_id is None else [[self._cover_id]]
        return []

    def joined(self) -> str:
        return " | ".join(self.seen)

    def params_for(self, needle: str) -> list[dict[str, object]]:
        return [p for s, p in zip(self.seen, self.params) if needle in s]


class _Extractor:
    """假抓取器。返回真实契约类型 `ExtractOutcome`，不连网。

    用真类型而不是 dict：假实现与真契约不一致会让测试证明不了任何事
    （`tests/test_worker_loop.py` 里记着同一个教训）。
    """

    def __init__(self, media: list[MediaItem] | None = None, ok: bool = True) -> None:
        self._media = media if media is not None else []
        self._ok = ok
        self.calls: list[str] = []

    async def __call__(self, url: str) -> ExtractOutcome:
        self.calls.append(url)
        if not self._ok:
            return ExtractOutcome(
                ok=False, route="parsehub", kind="link", error="被拦截"
            )
        return ExtractOutcome(
            ok=True,
            route="parsehub",
            kind="video",
            data=ExtractResult(
                kind="video",
                title="抓到的标题",
                content_text="",
                raw_url=url,
                platform="douyin",
                media=self._media,
            ),
        )


class _Stage:
    """假媒体阶段。记录收到的 specs，返回预设结果。"""

    def __init__(
        self,
        *,
        stored: tuple[StoredBlob, ...] = (),
        skipped: tuple[SkippedMedia, ...] = (),
        raises: Exception | None = None,
    ) -> None:
        self._stored = stored
        self._skipped = skipped
        self._raises = raises
        self.seen: list[list[MediaSpec]] = []
        self.item_ids: list[int] = []

    def run(self, specs: list[MediaSpec], *, item_id: int) -> MediaResult:
        self.seen.append(list(specs))
        self.item_ids.append(item_id)
        if self._raises is not None:
            raise self._raises
        # 只返回「能存下来」的那部分（真实现也是这样：skipped 不进 stored）
        return MediaResult(stored=self._stored, skipped=self._skipped)


# ── 缺口二：翻译必须带齐四个字段 ────────────────────────────


def _video_outcome() -> ExtractOutcome:
    """真机结构：视频只有一条 media，封面在 thumb_url。"""
    return ExtractOutcome(
        ok=True,
        route="parsehub",
        kind="video",
        data=ExtractResult(
            kind="video",
            title="第12集 网络协议",
            content_text="",
            raw_url="https://www.douyin.com/video/1",
            platform="douyin",
            media=[
                MediaItem(
                    role="video",
                    url="https://v95-aw-cold.douyinvod.com/v.mp4",
                    ext="mp4",
                    thumb_url="https://p95-zjwztc-sign.douyinpic.com/t.jpeg?x-expires=1",
                    width=768,
                    height=576,
                    duration_sec=445,
                )
            ],
        ),
    )


def test_翻译带thumbUrl_缺口二():
    """**设计 §0.2 缺口二。**

    原实现只传 `url` / `ext` / `durationSec`，于是封面的 URL 在 worker 边界上
    就已经丢了——即便下游写对了，`thumb_url` 也到不了。真机实测抖音视频的
    `media` 只有一条 `role='video'`，封面**只在** `thumb_url` 里。
    """
    kwargs = steps._outcome_to_kwargs("https://x", _video_outcome())
    media = kwargs["media"]
    assert isinstance(media, list) and media
    assert (
        media[0]["thumbUrl"]
        == "https://p95-zjwztc-sign.douyinpic.com/t.jpeg?x-expires=1"
    )


def test_翻译带role_真机抓到的bug的回归用例():
    """**这一条就是真机抓到的那个 bug。**

    worker 翻译时漏了 `role` → `plan._image_urls` 匹配不到 → 图文的 `specs`
    静默变成空列表。日志里只有「specs 为空」，看起来像「这条内容本来就没图」。

    `plan` 那边有 `ext` 兜底，但**兜底不该是唯一防线**——这条钉住翻译本身。
    """
    kwargs = steps._outcome_to_kwargs("https://x", _video_outcome())
    media = kwargs["media"]
    assert isinstance(media, list) and media
    assert media[0]["role"] == "video"


def test_翻译带width与height():
    """宽高原样带下去：将来「按原始比例排列、不裁切」要靠它，
    而重新从 URL 猜尺寸是不可能的。
    """
    kwargs = steps._outcome_to_kwargs("https://x", _video_outcome())
    media = kwargs["media"]
    assert isinstance(media, list) and media
    assert (media[0]["width"], media[0]["height"]) == (768, 576)


def test_翻译保留原有三个字段():
    """`url` / `ext` / `durationSec` 不能因为加了新字段就丢。"""
    kwargs = steps._outcome_to_kwargs("https://x", _video_outcome())
    media = kwargs["media"]
    assert isinstance(media, list) and media
    assert media[0]["url"] == "https://v95-aw-cold.douyinvod.com/v.mp4"
    assert media[0]["ext"] == "mp4"
    assert media[0]["durationSec"] == 445


def test_thumb_url为空时不出现thumbUrl键():
    """图文类没有 `thumb_url`。放 `None` 与不放进 dict 在 `plan` 那儿等价，
    但不放让 `props` 的 jsonb 更小、也让「有没有封面」在库里读得出来。
    """
    outcome = ExtractOutcome(
        ok=True,
        route="parsehub",
        kind="image",
        data=ExtractResult(
            kind="image",
            title="图文",
            content_text="",
            raw_url="https://x",
            platform="douyin",
            media=[MediaItem(role="image", url="https://x.invalid/a.jpg", ext="jpg")],
        ),
    )
    kwargs = steps._outcome_to_kwargs("https://x", outcome)
    media = kwargs["media"]
    assert isinstance(media, list) and media
    assert "thumbUrl" not in media[0]


def test_翻译后的dict能直接喂给plan():
    """**跨文件契约的端到端断言。**

    这条把「worker 翻译」与「plan 判定」两边的键名钉在一起。
    两边各改各的（一边写 `thumb_url`、一边读 `thumbUrl`）在这条上会立刻红，
    而单侧的用例都验不出来。
    """
    from kb.media import plan

    kwargs = steps._outcome_to_kwargs("https://x", _video_outcome())
    specs = plan.plan_attachments(kwargs["media"], kind="video")  # type: ignore[arg-type]
    assert [s.role for s in specs] == ["cover"]
    assert specs[0].url.endswith("x-expires=1")


# ── media_stage 缺席：不该联网 ──────────────────────────────


def test_media_stage为None时不下载任何东西():
    """现有 11 处 `run_once(pool, extractor)` 的调用不该因为本轮新增能力
    就突然开始联网。`media_stage` 默认 `None` 这条不是保守，是必要。
    """
    conn = _Conn()
    steps.run_fetch(
        conn,  # type: ignore[arg-type]
        1,
        7,
        extractor=_Extractor(
            [MediaItem(role="video", url="https://x/v.mp4", ext="mp4")]
        ),
    )

    joined = conn.joined()
    assert "attachments" not in joined, "装配件缺席时不该碰 attachments 表"
    assert "state = 'done'" in joined, "fetch 仍要标 done"


def test_run_fetch的media_stage参数有默认值None():
    """签名断言：`run_fetch(conn, item_id, job_id, extractor=...)` 四个参数就能调。"""
    import inspect

    params = inspect.signature(steps.run_fetch).parameters
    assert "media_stage" in params
    assert params["media_stage"].default is None


def test_run_fetch仍不接受ai_client_4节2条1():
    """R8 加媒体阶段不该顺手把 `ai_client` 拉进 `run_fetch` 的签名。

    那是「`ai_client=None` 对 fetch 分支完全无影响」这条性质的载体
    （`tests/test_worker_ai_steps.py` 也断言了同一件事）。
    """
    import inspect

    assert "ai_client" not in inspect.signature(steps.run_fetch).parameters


# ── 有装配件时的四步顺序 ────────────────────────────────────


def test_有装配件时走完四步并写库():
    conn = _Conn(cover_id=42)
    stage = _Stage(
        stored=(
            StoredBlob(
                role="cover",
                storage_key="ab/" + "c" * 32 + ".jpg",
                mime="image/jpeg",
                size_bytes=100,
                width=640,
                height=480,
                duration_sec=None,
                sha256="0" * 64,
                sort_order=0,
            ),
        )
    )
    steps.run_fetch(
        conn,  # type: ignore[arg-type]
        1,
        7,
        extractor=_Extractor(
            [
                MediaItem(
                    role="video",
                    url="https://x/v.mp4",
                    ext="mp4",
                    thumb_url="https://cdn.example/t.jpg",
                )
            ]
        ),
        media_stage=stage,
    )

    assert stage.item_ids == [1], "item_id 没传给媒体阶段"
    assert [s.role for s in stage.seen[0]] == ["cover"]
    joined = conn.joined()
    assert "insert into attachments" in joined, "没落库"
    assert "primary_media = :pm" in joined, "没写 primary_media"
    assert "state = 'done'" in joined


def test_primary_media取自primary_media_id():
    """`items.primary_media` 的语义是「展示图那张附件」，不是「视频附件」。

    这条断言「写进库的值就是查出来的 cover id」——写死一个值或写成
    `stored[0]` 的行号都会在这条上暴露。
    """
    conn = _Conn(cover_id=4211)
    stage = _Stage(
        stored=(
            StoredBlob(
                role="cover",
                storage_key="ab/" + "c" * 32 + ".jpg",
                mime="image/jpeg",
                size_bytes=1,
                width=1,
                height=1,
                duration_sec=None,
                sha256="0" * 64,
                sort_order=0,
            ),
        )
    )
    steps.run_fetch(
        conn,  # type: ignore[arg-type]
        1,
        7,
        extractor=_Extractor(
            [
                MediaItem(
                    role="video",
                    url="https://x/v.mp4",
                    ext="mp4",
                    thumb_url="https://cdn.example/t.jpg",
                )
            ]
        ),
        media_stage=stage,
    )
    writes = conn.params_for("primary_media = :pm")
    assert writes, "没写 primary_media"
    assert writes[-1]["pm"] == 4211


def test_没有spec时清空上一次的媒体状态():
    """内容可能从视频改成纯文字。旧封面留着会让卡片显示一张与内容无关的图，
    所以「这次没有媒体」必须显式清空，而不是「什么都不写」。
    """
    conn = _Conn()
    steps.run_fetch(
        conn,  # type: ignore[arg-type]
        1,
        7,
        extractor=_Extractor(
            [MediaItem(role="video", url="https://x/v.mp4", ext="mp4")]
        ),
        media_stage=_Stage(),
    )

    joined = conn.joined()
    assert "attachments" not in joined, "没有 spec 不该碰 attachments"
    assert "primary_media = :pm" in joined, "没清空 primary_media"
    writes = conn.params_for("primary_media = :pm")
    assert writes[-1]["pm"] is None


# ── 失败口径：不污染 jobs ───────────────────────────────────


def test_媒体阶段抛异常时fetch仍标done():
    """**设计 §5.2 的核心口径。**

    `derive_status` 只要看到任意 job 是 `failed` 就把条目显示成「处理失败」。
    而封面下载失败与「正文没抓到」是完全不同量级的事——前者不影响这条内容可用。
    为了把前者也标红，用户会对一堆本来能读的条目点重试，每次重试都重新走一遍
    ParseHub（10–20 秒 + 上游风控风险），而本轮根本没有重试端点。
    """
    conn = _Conn()
    stage = _Stage(raises=RuntimeError("CDN 全挂了"))
    steps.run_fetch(
        conn,  # type: ignore[arg-type]
        1,
        7,
        extractor=_Extractor(
            [
                MediaItem(
                    role="video",
                    url="https://x/v.mp4",
                    ext="mp4",
                    thumb_url="https://cdn.example/t.jpg",
                )
            ]
        ),
        media_stage=stage,
    )

    joined = conn.joined()
    assert "state = 'done'" in joined, "媒体异常不该让 fetch 失败"
    assert "attempts = attempts + 1" not in joined, "不该进失败重试"


def test_媒体阶段抛异常时写mediaError而不是jobs_error():
    """错误要写进 `props.mediaError`，**不写 `jobs.error`**。

    `jobs.error` 会进 `mark_failed`，那是「处理失败」的触发器。
    而 `props.mediaError` 只让详情页显示「封面未能下载」，不影响条目可读。
    """
    conn = _Conn()
    stage = _Stage(raises=RuntimeError("CDN 全挂了"))
    steps.run_fetch(
        conn,  # type: ignore[arg-type]
        1,
        7,
        extractor=_Extractor(
            [
                MediaItem(
                    role="video",
                    url="https://x/v.mp4",
                    ext="mp4",
                    thumb_url="https://cdn.example/t.jpg",
                )
            ]
        ),
        media_stage=stage,
    )

    writes = conn.params_for("primary_media = :pm")
    assert writes, "没写 media 状态"
    assert writes[-1]["err"], "mediaError 是空的，用户看不到为什么没有封面"
    assert "异常" in str(writes[-1]["err"])


def test_单条跳过时mediaError汇总了原因():
    """静默忽略不可接受——用户会看到卡片没有封面却不知道为什么。"""
    conn = _Conn(cover_id=None)
    stage = _Stage(
        skipped=(
            SkippedMedia(
                role="cover", url="https://cdn.example/t.jpg", reason="HTTP 403"
            ),
        )
    )
    steps.run_fetch(
        conn,  # type: ignore[arg-type]
        1,
        7,
        extractor=_Extractor(
            [
                MediaItem(
                    role="video",
                    url="https://x/v.mp4",
                    ext="mp4",
                    thumb_url="https://cdn.example/t.jpg",
                )
            ]
        ),
        media_stage=stage,
    )

    writes = conn.params_for("primary_media = :pm")
    assert "403" in str(writes[-1]["err"])


def test_全部成功时mediaError是空串():
    """空串是「这次全部成功」的信号，`set_media_props` 据此删掉 `mediaError` 键。

    不删的话，修好一次之后界面会一直显示「封面未能下载」（`||` 只覆盖、
    不删旧键）。
    """
    conn = _Conn(cover_id=1)
    stage = _Stage(
        stored=(
            StoredBlob(
                role="cover",
                storage_key="ab/" + "c" * 32 + ".jpg",
                mime="image/jpeg",
                size_bytes=1,
                width=1,
                height=1,
                duration_sec=None,
                sha256="0" * 64,
                sort_order=0,
            ),
        )
    )
    steps.run_fetch(
        conn,  # type: ignore[arg-type]
        1,
        7,
        extractor=_Extractor(
            [
                MediaItem(
                    role="video",
                    url="https://x/v.mp4",
                    ext="mp4",
                    thumb_url="https://cdn.example/t.jpg",
                )
            ]
        ),
        media_stage=stage,
    )
    assert conn.params_for("primary_media = :pm")[-1]["err"] == ""


# ── 抓取失败：不进入媒体阶段 ────────────────────────────────


def test_抓取失败时不进入媒体阶段():
    """没有内容可下载。同时必须清掉上一次的封面——那张图属于上一版内容。"""
    conn = _Conn()
    stage = _Stage()
    steps.run_fetch(
        conn,  # type: ignore[arg-type]
        1,
        7,
        extractor=_Extractor(ok=False),
        media_stage=stage,
    )

    assert stage.seen == [], "抓取失败却进了媒体阶段"
    joined = conn.joined()
    assert "attempts = attempts + 1" in joined, "抓取失败要标 failed"
    assert "primary_media = null" in joined, "没清掉上一次的封面"


def test_纯文字条目不进入媒体阶段():
    conn = _Conn(item_url=None)
    stage = _Stage()
    steps.run_fetch(
        conn,  # type: ignore[arg-type]
        1,
        7,
        extractor=_Extractor(),
        media_stage=stage,
    )
    assert stage.seen == []
    assert "state = 'done'" in joined_or_empty(conn), "纯文字条目 fetch 该算完成"


def joined_or_empty(conn: _Conn) -> str:
    """`_Conn` 在 `item_url=None` 时仍会记录 SQL，这里只是让断言可读。"""
    return conn.joined()


# ── extractor 被真的调用 ─────────────────────────────────


def test_抓取器被调用一次():
    extractor = _Extractor()
    steps.run_fetch(
        _Conn(),  # type: ignore[arg-type]
        1,
        7,
        extractor=extractor,
        media_stage=_Stage(),
    )
    assert extractor.calls == ["https://v.douyin.com/abc/"]


def test_asyncio_run被用来跑异步抓取器():
    """抓取器是 `async def`，`run_fetch` 是同步函数。

    这条不是「验 asyncio 能用」，而是钉住「`run_fetch` 不许改成 async」——
    改了对 11 处现有同步调用全是破坏性变更，而 `loop.run_once` 是同步的。
    """
    import inspect

    assert not inspect.iscoroutinefunction(steps.run_fetch)

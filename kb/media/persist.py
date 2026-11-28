"""媒体落盘编排：下载 → 校验 → 压缩 → 写盘。**不碰数据库。**

## 为什么这一层不 import `kb.store`

输入是 `list[MediaSpec]`（字符串与整数），输出是 `MediaResult`（字符串与整数）。
落库由 worker 经 `kb.store.attachments` 做。这样本模块能脱离数据库单测，
而「下载失败」与「抓取失败」也不会共用一套降级口径——两者该分开：
抓取失败要标 `fetchError` 让用户重试，封面下载失败不该（设计 §5.2）。

## 失败口径：一条失败不影响其余

一张图 404 不该让另外 19 张一起丢。所以单条失败记进 `skipped` 并继续。
**不抛异常**，只有「编程错误」（`media_root` 建不出来）才抛。

## 阶段闸门为什么必须有

单条上限 20 秒 × 20 张 = 400 秒，而一次 fetch 任务把队列占住 400 秒是不可接受的
（用户会以为 worker 死了）。90 秒闸门到点后剩余条目记 `skipped` 并**正常返回**
——部分成功比「全有或全无」有用得多。

## 写盘为什么走 `.incoming/` 再 `os.replace`

`os.replace` 在同分区内是原子的，所以正式路径上出现的文件必然是完整的。
直接往正式路径写，worker 被 kill 会留下半张图，而 `/api/media` 会把它当正常图
返回（浏览器显示为截断的灰块，比 404 更难查）。

临时目录必须与正式文件**同分区**：跨文件系统的 `os.replace` 会退化成
「复制 + 删除」，既慢又不是原子的。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config, layout, plan, sniff
from .encode import EncodeError, ImageEncoder, PillowEncoder
from .fetch import Downloader, DownloadError, HttpxDownloader, content_type_of
from .guard import Resolver, check_url, system_resolver
from .models import DownloadedBlob, MediaResult, MediaSpec, SkippedMedia, StoredBlob

log = logging.getLogger(__name__)

#: `skipped` 里给人看的原因前缀。汇总成一句 `props.mediaError` 时按这个顺序取前几条。
_REASON_MAX = 3


@dataclass(frozen=True)
class MediaStage:
    """媒体阶段的三个注入点 + 落盘目录，打成一个包。

    ## 为什么打包而不是四个关键字参数

    `run_fetch` 只需要知道「有这么个东西，把它交给 `store_media`」，
    不该知道 `Downloader` / `ImageEncoder` / `Resolver` 三个 Protocol 存在
    ——那会把媒体层的类型泄漏到 worker 的签名里。打包之后
    `run_fetch(..., media_stage=stage)` 与 `run_fetch(..., media_stage=None)`
    两种形态就够用了，而 `None` 让 11 处现有 `run_once(pool, extractor)`
    的调用**一行都不用改**（也不会突然开始联网下载）。

    ## `frozen=True` 与默认值

    全部字段必填（除 `stage_budget_sec`），没有「用真实现」的默认值：
    给默认值会让「忘了注入」在生产里默默生效、在单测里默默联网。
    生产装配走 `production_stage()`，那是唯一出现真实实现的地方。
    """

    media_root: Path
    downloader: Downloader
    encoder: ImageEncoder
    resolver: Resolver
    stage_budget_sec: float = config.STAGE_BUDGET_SEC

    def run(self, specs: list[MediaSpec], *, item_id: int) -> MediaResult:
        """把五个字段原样转给 `store_media`。

        留着这一层而不是让 worker 直接调 `store_media(..., downloader=stage.downloader, ...)`：
        worker 里那行会变成五个关键字参数的展开，将来 `store_media` 加一个参数
        就要改 worker。这里加一行就够。
        """
        return store_media(
            specs,
            item_id=item_id,
            media_root=self.media_root,
            downloader=self.downloader,
            encoder=self.encoder,
            resolver=self.resolver,
            stage_budget_sec=self.stage_budget_sec,
        )


def production_stage(media_root: Path) -> MediaStage:
    """生产装配：httpx 下载 + Pillow 压缩 + 系统解析器。

    三个构造都不 import 外部库（`HttpxDownloader` 的 httpx 在 `get` 里、
    `PillowEncoder` 的 PIL 在 `normalize` 里），所以本函数在干净环境也能调
    ——缺包的失败会落在**真正下载那一刻**，带明确的错误进 `props.mediaError`，
    而不是让 worker 起不来。
    """
    return MediaStage(
        media_root=media_root,
        downloader=HttpxDownloader(),
        encoder=PillowEncoder(),
        resolver=system_resolver,
    )


def store_media(
    specs: list[MediaSpec],
    *,
    item_id: int,
    media_root: Path,
    downloader: Downloader,
    encoder: ImageEncoder,
    resolver: Resolver,
    clock: Callable[[], float] | None = None,
    stage_budget_sec: float = config.STAGE_BUDGET_SEC,
) -> MediaResult:
    """下载 → 校验 → 压缩 → 写盘，返回待落库的 `StoredBlob`。

    参数全部显式传入，没有默认的真实实现：`downloader` / `encoder` / `resolver`
    都是必填。给默认值会让「忘了注入」变成在生产里默默用真实现（然后单测联网），
    而必填会让漏传在第一次调用时就炸开。

    `clock` 是可选的（默认 `time.monotonic`）：只有阶段闸门用它，而闸门在
    「没有 clock 就按真实时间」这个默认下行为仍然正确。

    ## 磁盘闸门

    低于 `MIN_FREE_BYTES` 时**整段跳过**并返回全 skipped。判据用
    `shutil.disk_usage`（与 `kb/api/health.py` 的 `disk_free_gb` 同一做法）。
    这一步在 `media_root` 创建之前做——建目录本身也可能因磁盘满失败。
    """
    if stage_budget_sec <= 0:
        raise ValueError("stage_budget_sec 必须是正数")
    now = clock or _monotonic
    started = now()

    if not specs:
        return MediaResult(stored=(), skipped=())

    free = _free_bytes(media_root)
    if free is not None and free < config.MIN_FREE_BYTES:
        reason = f"磁盘剩余不足（{free // (1024 * 1024)} MB），未保存图片"
        log.warning("媒体阶段跳过 item=%s：%s", item_id, reason)
        return MediaResult(
            stored=(),
            skipped=tuple(
                SkippedMedia(role=s.role, url=s.url, reason=reason) for s in specs
            ),
        )

    _ensure_root(media_root)

    stored: list[StoredBlob] = []
    skipped: list[SkippedMedia] = []
    for spec in specs:
        if now() - started > stage_budget_sec:
            skipped.append(
                SkippedMedia(
                    role=spec.role,
                    url=spec.url,
                    reason=f"媒体阶段超时（超过 {stage_budget_sec:.0f} 秒）",
                )
            )
            continue
        try:
            blob = _one(
                spec,
                item_id=item_id,
                media_root=media_root,
                downloader=downloader,
                encoder=encoder,
                resolver=resolver,
                clock=now,
            )
        except EncodeError as exc:
            skipped.append(SkippedMedia(role=spec.role, url=spec.url, reason=str(exc)))
            continue
        except _Skip as exc:
            skipped.append(
                SkippedMedia(role=spec.role, url=spec.url, reason=exc.reason)
            )
            continue
        except Exception as exc:  # noqa: BLE001 单条媒体的任何异常都不能让其余丢
            # **为什么这一条不能省**：`check_url` 的契约是永不抛，但
            # `downloader.get` / `encoder.normalize` 是注入的实现，任何一个
            # 都可能抛没被包成 `_Skip` 的异常。少了这条兜底，一次意外异常会
            # 跳出整个 for 循环——`stored` 是局部变量，异常退出时前几张已下载
            # 的字节全丢。那正好违背本模块「一条失败不影响其余」的核心口径。
            #
            # 与 `kb/worker/loop.py` 的「单条坏数据不打死亡整个 worker」
            # 同一个理由，只是粒度更细（一条媒体 vs 一条任务）。
            log.exception(
                "媒体处理未预期异常 item=%s role=%s url=%s",
                item_id,
                spec.role,
                spec.url,
            )
            skipped.append(
                SkippedMedia(
                    role=spec.role,
                    url=spec.url,
                    reason=f"未预期异常（{type(exc).__name__}: {exc}）",
                )
            )
            continue
        stored.append(blob)

    return MediaResult(stored=tuple(stored), skipped=tuple(skipped))


def summarize_skipped(skipped: tuple[SkippedMedia, ...]) -> str:
    """把 skipped 收成一句写进 `props.mediaError` 的短串。

    为什么写这句：**静默忽略不可接受**——用户会看到卡片没有封面却不知道为什么。
    为什么**不写 `jobs.error`**：`derive_status` 只要看到任意 job 是 `failed`
    就把条目显示成「处理失败」，而封面没下下来不影响这条内容可读。

    取前 3 条而不是全部：20 条失败塞进 jsonb 是一段没人读的日志，而「第一条
    原因」通常就是根因（CDN 域名封了、磁盘满了）。
    """
    if not skipped:
        return ""
    head = skipped[:_REASON_MAX]
    parts = [f"{item.role}：{item.reason}" for item in head]
    tail = (
        ""
        if len(skipped) <= _REASON_MAX
        else f"（另有 {len(skipped) - _REASON_MAX} 条）"
    )
    return "；".join(parts) + tail


class _Skip(Exception):
    """内部信号：这一条不存，原因在 `reason` 里。

    用它而不是给 `_one` 加一个「成功/失败」的联合返回值：`_one` 的成功分支要
    返回 `StoredBlob`，失败分支要带原因，混成一个返回值就得在每处调用点解包。
    异常让成功路径保持直线，而这条异常从不出本模块（在 `store_media` 里收掉）。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _one(
    spec: MediaSpec,
    *,
    item_id: int,
    media_root: Path,
    downloader: Downloader,
    encoder: ImageEncoder,
    resolver: Resolver,
    clock: Callable[[], float],
) -> StoredBlob:
    """处理一条媒体。失败抛 `_Skip`，成功返回 `StoredBlob`。

    六步，顺序固定：URL 校验 → 下载 → mime 判定 → role 对齐 → 压缩 → 写盘。
    每一步失败都带**具体原因**（`HTTP 403` / `mime 无法判定` / `写盘失败：...`）,
    因为这一句会出现在 `props.mediaError` 里给人看。
    """
    target = check_url(spec.url, resolver=resolver)
    if isinstance(target, str):
        raise _Skip(f"地址不可用（{target}）")

    deadline = clock() + config.REQUEST_TOTAL_SEC
    try:
        status, headers, data = downloader.get(
            target, max_bytes=config.MAX_SOURCE_BYTES, deadline=deadline
        )
    except DownloadError as exc:
        raise _Skip(f"下载失败（{exc}）") from exc

    if 300 <= status < 400:
        # 不跟随重定向：一个 302 → 127.0.0.1 能绕过 SSRF 判定的全部三层
        raise _Skip(f"重定向不跟随（HTTP {status}）")
    if not 200 <= status < 300:
        raise _Skip(f"HTTP {status}")
    if not data:
        raise _Skip("响应体为空")

    sniffed = sniff.sniff_mime(data, content_type_of(headers))
    if sniffed is None:
        raise _Skip("mime 无法判定")
    mime, basis = sniffed
    if not sniff.mime_matches_role(mime, spec.role):
        raise _Skip(f"mime 与角色不符（{mime} 不能当 {spec.role}）")

    ext = layout.ext_for_mime(mime)
    if ext is None:
        raise _Skip(f"不支持的图片格式（{mime}）")

    try:
        payload, width, height = encoder.normalize(
            data, long_side=config.LONG_SIDE, max_bytes=config.TARGET_BYTES
        )
    except EncodeError:
        raise
    except Exception as exc:  # noqa: BLE001 编码器实现抛非预期异常也要收成 skipped
        raise _Skip(f"图片处理失败（{type(exc).__name__}: {exc}）") from exc

    blob = DownloadedBlob(
        role=spec.role,
        url=spec.url,
        sort_order=spec.sort_order,
        data=payload,
        mime=mime,
        ext=ext,
    )
    digest = hashlib.sha256(blob.data).hexdigest()
    key = layout.storage_key(item_id, digest, blob.ext)
    if not layout.is_valid_key(key):
        # 到不了：key 由本层的纯函数算出，格式必然合法。若真发生说明
        # `layout.storage_key` 与 `layout.KEY_RE` 漂移了，这是编程错误，要抛。
        raise RuntimeError(f"派生出的 storage_key 不合法：{key}")
    _write(media_root, key, blob.data)
    log.debug(
        "媒体落盘 item=%s role=%s key=%s mime=%s（%s）%d 字节",
        item_id,
        spec.role,
        key,
        mime,
        basis,
        len(blob.data),
    )

    return StoredBlob(
        role=blob.role,
        storage_key=key,
        mime=blob.mime,
        size_bytes=len(blob.data),
        width=width,
        height=height,
        duration_sec=None,
        sha256=digest,
        sort_order=blob.sort_order,
    )


def _write(media_root: Path, key: str, data: bytes) -> None:
    """写盘。已存在则跳过，否则 `.incoming/` → `os.replace`。

    **已存在就跳过写入**：key 由内容哈希派生，同 key 必然同内容，重写一遍
    只是多一次 IO 与一次改 mtime。这条也让「重跑 fetch」在文件层完全幂等。

    写盘用 `open(path, "wb")` 而不是任何外部库：`kb/vision/grab.py` 记录了
    `cv2.imwrite` 对中文路径返回 `False` 且不抛异常的实测，本项目目录本身就带
    中文（`C:/Users/.../知识库`）。

    失败抛 `_Skip`：磁盘满/只读是**可预期的运行期结果**（一张图存不下不该让
    另外 19 张一起丢），所以不是编程错误，走 skipped 通道。
    """
    final_path = media_root / key
    if final_path.exists():
        return

    incoming = media_root / config.INCOMING_DIR
    temp_path = incoming / f"{uuid.uuid4().hex}.part"
    try:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        with open(temp_path, "wb") as handle:
            handle.write(data)
        os.replace(temp_path, final_path)
    except OSError as exc:
        # 磁盘满、只读、权限不足。清掉临时文件避免 `.incoming/` 堆积。
        _unlink_quietly(temp_path)
        raise _Skip(f"写盘失败（{exc}）") from exc


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # noqa: BLE001 清理失败不影响结论，残留由启动清理回收
        log.debug("临时文件清理失败：%s", path)


def _ensure_root(media_root: Path) -> None:
    """建 `<media_root>` 与它的 `.incoming/`。

    建不出来是**编程错误**（配置写错、挂载点不存在），所以直接抛——
    把它降级成 skipped 会让整批媒体静默丢失。
    """
    try:
        media_root.mkdir(parents=True, exist_ok=True)
        (media_root / config.INCOMING_DIR).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"媒体目录建不出来（{media_root}）：{exc}") from exc


def _free_bytes(media_root: Path) -> int | None:
    """取 `media_root` 所在分区的剩余字节。目录不存在就往上找第一个存在的祖先
    （与 `kb/api/health.disk_free_gb` 同一做法）。

    全都不存在、或无权限时返回 `None`：**判不出不等于没空间**，
    此时按「继续尝试」处理——让下载去撞真实的 OSError，错误信息更具体。
    """
    target = media_root
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        return shutil.disk_usage(target).free
    except OSError:
        return None


def clean_incoming(media_root: Path) -> int:
    """清空 `.incoming/`，返回删掉的条目数。

    由进程启动时调用（与 `kb/api/app.py` 的 lifespan 里清过期会话同一个思路）：
    上一次被 kill 时留下的 `.part` 文件不会被任何逻辑回收，而它们与正式文件
    同目录，`stats()` 也看不到——会悄悄吃掉磁盘。

    失败不抛：启动清理失败不该阻止服务/worker 起来。
    """
    incoming = media_root / config.INCOMING_DIR
    if not incoming.is_dir():
        return 0
    removed = 0
    try:
        for entry in incoming.iterdir():
            try:
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                removed += 1
            except OSError:  # noqa: BLE001 单个残留删不掉就留着，下一次再试
                continue
    except OSError:
        return removed
    return removed


def _monotonic() -> float:
    import time

    return time.monotonic()


#: 让 worker 用同一个默认上限，不必自己 import `config`。
DEFAULT_STAGE_BUDGET_SEC = config.STAGE_BUDGET_SEC

#: 导出给 worker：它要判断「有没有被截断」。
MAX_IMAGES_PER_ITEM = plan.MAX_IMAGES_PER_ITEM

__all__ = [
    "DEFAULT_STAGE_BUDGET_SEC",
    "MAX_IMAGES_PER_ITEM",
    "MediaStage",
    "clean_incoming",
    "production_stage",
    "store_media",
    "summarize_skipped",
]

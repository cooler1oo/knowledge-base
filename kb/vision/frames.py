"""抽帧装配：probe → 规划预算 → 切镜头 → 整理区间 → 分层选帧 → 读帧 → 过滤 → 写盘。

本模块**不 import 任何外部库**（连 `cv2` 都没有）。它只依赖两个 Protocol
（`ShotCutter` / `FrameGrabber`），所以整条链路能用两个假实现毫秒级跑完单测
——真实现只在探针脚本里出现。

## 三层失败口径（不许混）

| 层 | 失败 | 处理 | 理由 |
|---|---|---|---|
| 视频 | 打不开（损坏/缺编解码/不是视频） | **抛 `VideoUnreadable`** | 调用方必须显式决定降级。静默返回空集会让「视频坏了」与「视频没内容」在库里长得一样 |
| 镜头 | 检测过程抛异常 | `log.warning` 后按 `raw=()` 继续，走单镜头兜底 | 一条视频的镜头检测失败不该让整条视频零帧 |
| 单帧 | 读失败 / 空白 / 写盘失败 | 记 `SkippedFrame` 后继续下一帧 | 一块坏帧不该让整条视频白跑，理由同 worker 收异常 |

**零帧是合法结果**，返回 `FrameSet(frames=())` 且**不抛异常**：这是「这条视频
画面无信息」的正常结论（纯黑片、纯音频、全屏 PPT 动画但每帧都一样）。把它
当故障抛出去，调用方会去重试一条永远不会有画面的视频。

## 为什么参数这么多而不是一个 Options 对象

每个参数都是独立的物理量（帧数、秒数、像素、质量、阈值）。合成对象会让调用处
变成 `Options(max_frames=12, ...)` 多一层括号，而单测想改的往往只有其中一个。
8 个关键字参数在这个规模上是清楚的。
"""

from __future__ import annotations

import logging
import os
from typing import Sequence

from .cut import SceneDetectCutter, ShotCutter
from .grab import Cv2Grabber, FrameGrabber, FrameReadFailed
from .models import (
    ExtractedFrame,
    FramePick,
    FrameSet,
    ScanPlan,
    Shot,
    SkippedFrame,
    VideoMeta,
)
from .shot_plan import (
    DEFAULT_MAX_SCAN_SEC,
    DEFAULT_MIN_STD_LUMA,
    frame_filename,
    is_blank,
    merge_shots,
    plan_picks,
    plan_scan,
)

log = logging.getLogger(__name__)

# 帧图长边上限。不取 768 的理由：小屏视频的字幕字号可能只有 20 像素，缩到 768
# 后只剩 16 像素，视觉模型读中文的成功率会掉。实测真实界面画面 1080x1920：
# 原尺寸 55 KB → 长边 1024 时 18 KB → 768 时 11 KB。12 帧合计 200 KB 量级。
DEFAULT_LONG_SIDE = 1024

# JPEG 质量。85 是「字幕仍清晰、体积仍小」的实测拐点；再往上体积涨得快而
# 视觉模型读字没有提升。
DEFAULT_JPEG_QUALITY = 85

# 抽帧数上限。⚠️ 12 的依据是**成本**，而这个成本还没有真实数字：方案 §4.11 那
# 8 帧（均分 5 段 + 3 个转折帧）成功读到 4 个章节标题，12 是往上留一半余量。
# 视觉通道配好后应按真实单价复核这个默认值——不要以为 12 有实测依据。
DEFAULT_MAX_FRAMES = 12

# 最短镜头（秒）。短于它的碎片会被并进前一段（见 shot_plan.merge_shots 第 ④ 步）：
# 闪烁视频（片头频闪）在低阈值下会切出 250 个 0.02 秒镜头，按这个阈值收敛成 1 个。
DEFAULT_MIN_SHOT_SEC = 0.5

# 跳过原因。`blank` 与 `read_failed` 是设计定的两类；`write_failed` 是补的
# ——写盘会因磁盘满或权限失败，那是真事，不该混进 `read_failed` 里。
REASON_BLANK = "blank"
REASON_READ_FAILED = "read_failed"
REASON_WRITE_FAILED = "write_failed"


def extract_frames(
    video_path: str,
    work_dir: str,
    *,
    cutter: ShotCutter | None = None,
    grabber: FrameGrabber | None = None,
    max_frames: int = DEFAULT_MAX_FRAMES,
    min_shot_sec: float = DEFAULT_MIN_SHOT_SEC,
    long_side: int = DEFAULT_LONG_SIDE,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    min_std_luma: float = DEFAULT_MIN_STD_LUMA,
    max_scan_sec: float = DEFAULT_MAX_SCAN_SEC,
) -> FrameSet:
    """从视频拿到一组带时间戳的帧。

    `cutter` / `grabber` 是注入点：传 `None` 用真实现（PySceneDetect + OpenCV），
    单测传假实现。**用参数注入而不是 monkeypatch**：patch 会让"这个函数用了什么"
    变成运行时才知道，而构造参数写在签名里，读代码就能看见。

    返回的 `work_dir` 由调用方决定保留还是删——本层**不清理**。清理时机属于
    调用方的生命周期：探针要把帧给人眼看，worker 可能在写完数据库之后才删。
    """
    if max_frames < 0:
        raise ValueError("max_frames 不能为负")
    if min_shot_sec < 0:
        raise ValueError("min_shot_sec 不能为负")
    if max_scan_sec <= 0:
        raise ValueError("max_scan_sec 必须是正数")

    cutter = cutter if cutter is not None else SceneDetectCutter()
    grabber = grabber if grabber is not None else Cv2Grabber()

    # ① probe。打不开就抛，让调用方显式降级——这是三层口径里唯一抛异常的一层。
    meta = grabber.probe(video_path)

    # ② 预算。元信息不可信（duration<=0 或 fps<=0）时返回空集，不去猜时长硬扫。
    scan = plan_scan(meta.duration_sec, meta.fps, max_scan_sec)
    if scan is None:
        log.warning(
            "视频元信息不可信（时长 %.3f 秒，fps %.3f），不抽帧：%s",
            meta.duration_sec,
            meta.fps,
            video_path,
        )
        return _empty_set(work_dir, meta)

    # ③ 切镜头。异常不放弃整条视频：按「无切点」继续，走单镜头兜底。
    shots = _detect_shots(cutter, video_path, scan, min_shot_sec, meta)

    # ④ 整理区间。merge_shots 保证时间轴连续覆盖 [0, scan_sec]，这是⑤的前提。
    # 注意传 scan.scan_sec 而不是 meta.duration_sec：检测只扫到 scan_sec。
    shots = merge_shots(
        raw=[(s.start_sec, s.end_sec) for s in shots],
        duration_sec=meta.duration_sec,
        scan_sec=scan.scan_sec,
        min_shot_sec=min_shot_sec,
    )
    if not shots:
        return _empty_set(work_dir, meta, scan)

    # ⑤ 分层选帧。
    picks = plan_picks(shots, max_frames=max_frames)
    if not picks:
        return _empty_set(work_dir, meta, scan, shots=shots)

    # ⑥ 逐帧读、过滤、写盘。
    os.makedirs(work_dir, exist_ok=True)
    frames, skipped = _collect(
        grabber=grabber,
        video_path=video_path,
        picks=picks,
        work_dir=work_dir,
        long_side=long_side,
        jpeg_quality=jpeg_quality,
        min_std_luma=min_std_luma,
    )

    # ⑦ 兜底：一帧都没抽到（全被判定 blank 或全读失败）时，在 shots[0] 的中点
    # 再试一次。仍失败就如实返回零帧——不抛异常，因为「画面无信息」是结论不是故障。
    #
    # 若这个位置就是刚才试过的那个（单镜头视频必然如此），重试只是把同一个失败
    # 再演一遍。所以先判重：**不做没有信息增量的重试**。
    if not frames:
        retry_pick = _retry_pick(shots)
        if retry_pick is not None and all(
            abs(retry_pick.at_sec - p.at_sec) > 1e-6 for p in picks
        ):
            log.info(
                "按镜头分层抽帧全部落空，改在首个镜头中点再试一次：%.3f 秒",
                retry_pick.at_sec,
            )
            frames, more_skipped = _collect(
                grabber=grabber,
                video_path=video_path,
                picks=(retry_pick,),
                work_dir=work_dir,
                long_side=long_side,
                jpeg_quality=jpeg_quality,
                min_std_luma=min_std_luma,
            )
            skipped = skipped + more_skipped

    return FrameSet(
        frames=frames,
        shots=shots,
        meta=meta,
        scan=scan,
        work_dir=work_dir,
        skipped=skipped,
    )


def _empty_set(
    work_dir: str,
    meta: VideoMeta,
    scan: ScanPlan | None = None,
    shots: tuple[Shot, ...] = (),
) -> FrameSet:
    """零帧结果。

    `work_dir` **原样带回**（即使目录还没被创建），不返回空串：调用方按契约会
    拿它去 `shutil.rmtree`，给空串会让 `rmtree("")` 抛 `FileNotFoundError`。
    「没有帧」不该让调用方在清理这一步再失败一次。

    `scan` 缺省时给一个零长方案：元信息不可信的情况下没有扫描预算。
    """
    return FrameSet(
        frames=(),
        shots=shots,
        meta=meta,
        scan=scan
        if scan is not None
        else ScanPlan(frame_skip=0, scan_sec=0.0, truncated=False),
        work_dir=work_dir,
        skipped=(),
    )


def _detect_shots(
    cutter: ShotCutter,
    video_path: str,
    scan: ScanPlan,
    min_shot_sec: float,
    meta: VideoMeta,
) -> tuple[Shot, ...]:
    """切镜头，失败降级为「无切点」。

    降级而不是上抛：一条视频的镜头检测失败不该让整条视频零帧。降级后
    `merge_shots` 会补一个覆盖全片的镜头，于是这条视频仍然得到一帧代表画面
    ——这正是「检测没工作」时最合理的兜底。
    """
    try:
        return cutter.detect(video_path, scan, min_shot_sec)
    except Exception as exc:  # noqa: BLE001 镜头检测失败降级为单镜头
        log.warning(
            "切镜头失败，按「整条一个镜头」继续（%s，%.1f 秒）：%s: %s",
            video_path,
            meta.duration_sec,
            type(exc).__name__,
            exc,
        )
        return ()


def _collect(
    grabber: FrameGrabber,
    video_path: str,
    picks: Sequence[FramePick],
    work_dir: str,
    long_side: int,
    jpeg_quality: int,
    min_std_luma: float,
) -> tuple[tuple[ExtractedFrame, ...], tuple[SkippedFrame, ...]]:
    """按 `picks` 逐帧读、判空白、编码、写盘。

    `open` 与 `close` 包在 try/finally 里：`open` 之后任何一帧出错都不能让
    解码句柄泄漏——长跑 worker 会句柄耗尽，表现是「跑几小时之后所有视频都打不开」。
    """
    frames: list[ExtractedFrame] = []
    skipped: list[SkippedFrame] = []
    try:
        grabber.open(video_path)
        for pick in picks:
            at_sec = round(pick.at_sec, 3)
            try:
                image = grabber.read_at(at_sec)
            except FrameReadFailed as exc:
                log.warning("第 %d 帧读失败：%s", pick.shot_index, exc)
                skipped.append(
                    SkippedFrame(
                        shot_index=pick.shot_index,
                        at_sec=at_sec,
                        reason=REASON_READ_FAILED,
                    )
                )
                continue

            if is_blank(image, min_std_luma):
                # 全黑帧送给视觉模型只会得到「这是一张纯黑图片」，白花一次调用。
                # 但要把原因记下来：能从 FrameSet 看出「12 个镜头里有 3 个纯黑」
                # 是内容信息，不是故障。
                log.debug(
                    "第 %d 帧（%.3f 秒）判定为空白：std_luma=%.2f",
                    pick.shot_index,
                    at_sec,
                    image.std_luma,
                )
                skipped.append(
                    SkippedFrame(
                        shot_index=pick.shot_index, at_sec=at_sec, reason=REASON_BLANK
                    )
                )
                continue

            try:
                data = grabber.encode(image, long_side, jpeg_quality)
                path = os.path.join(work_dir, frame_filename(pick.shot_index, at_sec))
                _write_bytes(path, data)
            except (FrameReadFailed, OSError) as exc:
                log.warning("第 %d 帧写盘失败：%s", pick.shot_index, exc)
                skipped.append(
                    SkippedFrame(
                        shot_index=pick.shot_index,
                        at_sec=at_sec,
                        reason=REASON_WRITE_FAILED,
                    )
                )
                continue

            frames.append(
                ExtractedFrame(
                    shot_index=pick.shot_index,
                    at_sec=at_sec,
                    start_sec=pick.start_sec,
                    end_sec=pick.end_sec,
                    path=path,
                    width=image.width,
                    height=image.height,
                    size_bytes=len(data),
                )
            )
    finally:
        grabber.close()

    return tuple(frames), tuple(skipped)


def _write_bytes(path: str, data: bytes) -> None:
    """写 JPEG 字节。

    **不能用 `cv2.imwrite`**：实测它对中文路径返回 `False` 且文件不存在、
    不抛异常。本项目目录本身就带中文（`C:/Users/.../知识库`），临时目录也可能带。
    用 `open(..., 'wb')` 自己写，Python 的路径处理在三个平台上都正确。

    走 `grabber.encode` 拿 bytes 再写，也让「编码」与「落盘」的失败能分开记：
    编码失败是库的问题，写盘失败是磁盘或权限的问题，排查方向完全不同。
    """
    with open(path, "wb") as fh:
        fh.write(data)


def _retry_pick(shots: Sequence[Shot]) -> FramePick | None:
    """兜底重试的位置：首个镜头的**中点**。

    取中点不取起点：起点离切换边界太近，`CAP_PROP_POS_MSEC` 的定位精度到帧，
    向上取整就落到前一个镜头。中点距两侧边界各半个镜头长度，同样的精度误差
    落在中点不会有影响。
    """
    if not shots:
        return None
    first = shots[0]
    return FramePick(
        shot_index=first.index,
        at_sec=round((first.start_sec + first.end_sec) / 2, 3),
        start_sec=first.start_sec,
        end_sec=first.end_sec,
    )

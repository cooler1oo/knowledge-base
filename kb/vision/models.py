"""视觉链路的值对象：八个 frozen dataclass 加一个异常类。

单独一个文件而不是塞进 `shot_plan.py`：`cut.py`、`grab.py`、`frames.py` 都要用这些
类型，任何一个文件定义它们都会造成三处以上互相 import 或单点依赖。单列一个只有
dataclass 的文件，谁 import 都不产生耦合。

**全部 frozen**，理由是这些值会在多处之间传递（切分 → 抽帧规划 → 写盘 → 返回），
可变对象中途被谁改过就说不清了。排查「为什么第 7 帧的时间戳不对」时，能确定
「拿到手之后没人改过」省掉一半工作。

时间单位一律**秒（float）**，字段名以 `_sec` 结尾；区间是**左闭右开**
`[start_sec, end_sec)`，与 `kb/asr` 的采样点区间口径一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VideoMeta:
    """视频的元信息，来自容器头，不解码任何一帧。

    `duration_sec` 与 `fps` 由 `帧数 / fps` 与容器声明值算出，**不可信是常态**：
    抖音的 mp4 常见 fps 声明为 0 或 30 而实际 25。所以 `plan_scan` 拿这两个数
    做预算分配时会先校验，异常值一律当成「元信息不可信」而不是硬算下去。
    """

    path: str
    width: int
    height: int
    duration_sec: float
    fps: float


@dataclass(frozen=True)
class ScanPlan:
    """检测预算的分配结果。纯算术产出，不依赖视频文件。

    `frame_skip` 与 `scan_sec` 管的是两件事：前者是「每秒看多少帧」，
    后者是「最多看多久」。实测跳帧只损失切点精度、不损失检出率，所以
    超长视频优先用跳帧压成本；`scan_sec` 截断会不可逆地丢掉后半段章节，
    因此只在超过 30 分钟时才触发。
    """

    frame_skip: int
    scan_sec: float
    # scan_sec < duration_sec，即尾部没扫。标出来是为了让下游知道
    # 「最后一帧不代表整条视频的结尾」。
    truncated: bool


@dataclass(frozen=True)
class Shot:
    """一个镜头，左闭右开区间 `[start_sec, end_sec)`。

    `partial=True` 表示这个镜头的结尾不是真实结尾——扫描被 `max_scan_sec`
    截断了。不留这个字段，「第 500 个镜头特别长」这种假象无法解释：
    它其实是被截断的尾巴里塞着的几十个镜头。
    """

    index: int
    start_sec: float
    end_sec: float
    partial: bool = False

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec


@dataclass(frozen=True)
class FramePick:
    """要抽哪一帧的意图，**尚未真正读取**。

    与 `ExtractedFrame` 分开是因为两者之间隔着三次可能失败的动作
    （读帧、判空白、编码写盘）。用同一个类型表示「想抽」和「抽到了」，
    调用方就分不清 `frames` 里少掉的那一帧是被判空白还是读不出来。
    """

    shot_index: int
    at_sec: float
    start_sec: float
    end_sec: float


@dataclass(frozen=True)
class FrameImage:
    """刚从视频读出来的一帧（未编码、未压缩）。

    `data` 声明为 `Any` 而不是 `numpy.ndarray`，是为了本文件不 import numpy：
    `opencv-python-headless` 与 `scenedetect` 都会把 numpy 带进来，但纯逻辑层
    的单测不该因为 numpy 缺失而收集失败。真正用 `data` 的只有 `grab.py`，
    那里已经在方法内 import cv2 了。

    `mean_luma` 与 `std_luma` 是灰度统计（0–255 量纲）。**只有 `std_luma`
    参与空白判定**：纯色的标准差恒为 0 与亮度无关，而「暗但有内容」的场景
    标准差是 16.8，用平均亮度判会把它误杀。`mean_luma` 留在结构里只为排查时看得见。
    """

    data: Any
    width: int
    height: int
    mean_luma: float
    std_luma: float


@dataclass(frozen=True)
class ExtractedFrame:
    """已落盘的一帧。

    连 `start_sec` / `end_sec` 一起带上，不只是 `at_sec`：方案 §4.11 的用途是
    「摘要里的词来自第几秒要能查」。只知道帧在第 222.5 秒不够——还要知道它
    代表 `[218.0, 227.0)` 这一整个镜头的画面，否则「这个章节覆盖多长」说不清。
    """

    shot_index: int
    at_sec: float
    start_sec: float
    end_sec: float
    path: str
    width: int
    height: int
    size_bytes: int


@dataclass(frozen=True)
class SkippedFrame:
    """本该抽但没抽成的一帧，以及原因。

    留存是为了回答「为什么只有 8 帧」——逐帧数下去发现 4 帧被跳过，
    与「切镜头只切出 8 个」是完全不同的结论，前者是画面无信息，
    后者是检测没工作。`reason` 取 `"blank"` 或 `"read_failed"`。
    """

    shot_index: int
    at_sec: float
    reason: str


@dataclass(frozen=True)
class FrameSet:
    """一次抽帧的全部产出。

    `work_dir` 由调用方传入、由调用方决定保留还是清理——**本层不删**。
    清理时机属于调用方的生命周期：探针脚本要把帧给人眼看，worker 可能在
    写完数据库之后才删。本层擅自清理会删掉调用方正要读的文件。
    """

    frames: tuple[ExtractedFrame, ...]
    shots: tuple[Shot, ...]
    meta: VideoMeta
    scan: ScanPlan
    work_dir: str
    skipped: tuple[SkippedFrame, ...] = ()

    @property
    def count(self) -> int:
        return len(self.frames)

    def paths(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.frames)


class VideoUnreadable(Exception):
    """视频打不开（文件损坏、缺编解码、不是视频文件）。

    单独一个异常类而不返回空集：调用方必须**显式决定**怎么降级
    （跳过这条内容 / 标处理失败 / 告警）。静默返回 `FrameSet(frames=())`
    会让「视频坏了」与「视频画面无信息」在库里长得一样，而两者的正确处置
    完全不同——前者该重试或告警，后者是正常结论。

    `path` 与 `reason` 都保留：worker 的 `jobs.error` 要能说清是哪条视频、
    因为什么原因打不开。
    """

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"视频打不开：{path}（{reason}）")
        self.path = path
        self.reason = reason

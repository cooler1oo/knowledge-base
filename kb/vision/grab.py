"""抽帧：从视频里按秒读出一帧，再压成 JPEG 字节。

`FrameGrabber` 是五方法的 Protocol（`probe` / `open` / `read_at` / `encode` / `close`），
真实现 `Cv2Grabber` 把 OpenCV 接进来。`cv2` 在方法体内 import：这样
`tests/test_vision_plan.py` 与本机没装 cv2 的环境都能 `import kb.vision.grab`。

## 为什么 `probe` 与 `open` 分开

`probe` 要在切镜头**之前**拿到时长与 fps（`plan_scan` 需要它），`open` 要在帧
**选定之后**才持有解码句柄。合成一个方法会让解码句柄在整个切镜头过程里开着，
而那段过程 PySceneDetect 自己要开一个解码器——平板 8 GB 上两个解码器同时占内存
是上一轮 fp32 模型崩溃的同一个坑。

## 三条实测事实（写错会在真机上静默出错，不会报错）

1. **必须复用 cap，不能每帧重开。** 实测同一视频抽 40 帧：复用 0.219 秒
   （5.5 ms/帧），每帧重开 1.372 秒（34.3 ms/帧），差 6.2 倍。重开在大文件上
   还要重新解析容器头。
2. **必须用 `cv2.imencode` + 自己写文件，不能用 `cv2.imwrite`。** 实测
   `cv2.imwrite('.tmp_cn/知识库测试/a.jpg', img)` 返回 `False` 且文件不存在，
   **不抛异常**。本项目目录本身就带中文（`C:/Users/.../知识库`），这不是理论风险。
   写文件的部分在 `frames.py`（纯 `open(...,'wb')`），本模块只负责出字节。
3. **越界判定只能用 `ok`，不能用 `CAP_PROP_POS_MSEC`。** 实测 3 秒视频问
   3.0 秒与 5.0 秒都返回 `False`，而 `CAP_PROP_POS_MSEC` 读回 **0**（不是 3.0 或 5.0），
   `CAP_PROP_POS_FRAMES` 停在最后一帧——位置信息在越界后不可信。

## 不做帧偏移补偿

实测 `CAP_PROP_POS_MSEC` 定位是准的：3 镜头合成视频（红 0–1s / 绿 1–2s / 蓝 2–3s，
25 fps），问 0.40 秒得红、0.96 秒得红、1.00 秒得绿、2.40 秒得蓝，逐帧对齐。
早先设计稿写过「减半帧抵掉 1 帧偏移」，那是把 `0.96s → 红`（本来正确）误读成偏差。
加一个不存在的补偿会让每帧落在镜头边界前 0.02 秒，反而是新的风险。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from .models import FrameImage, VideoMeta, VideoUnreadable

log = logging.getLogger(__name__)


class FrameReadFailed(Exception):
    """单帧读不出来（越界、解码失败、句柄未打开）。

    与 `VideoUnreadable` 分开：那个是「整条视频打不开」，调用方必须显式降级
    （标处理失败/告警）；这个只是「这一帧没有」，记一条 `SkippedFrame` 继续下一帧
    即可。两者混用会让降级策略写错——一帧读失败不该让整条视频零帧。

    定义在本模块而不是 `models.py`：它是抽帧这一个装配环节的错误，不是
    跨文件共享的值对象。`models.py` 只放会被三个以上文件用到的类型。
    """

    def __init__(self, at_sec: float, reason: str) -> None:
        super().__init__(f"第 {at_sec:.3f} 秒这一帧读不出来（{reason}）")
        self.at_sec = at_sec
        self.reason = reason


class FrameGrabber(Protocol):
    """读一帧、压一张图。五个方法，全部与「拿文字」无关。

    `probe` 与 `open` 分开的理由见模块 docstring（解码句柄的生命周期）。
    `encode` 属于本 Protocol 而不属于纯逻辑层：JPEG 编码是 OpenCV 的事，
    放纯逻辑层就要在那一层 import cv2，单测性就没了。
    """

    def probe(self, video_path: str) -> VideoMeta: ...

    def open(self, video_path: str) -> None: ...

    def read_at(self, at_sec: float) -> FrameImage: ...

    def encode(self, image: FrameImage, long_side: int, quality: int) -> bytes: ...

    def close(self) -> None: ...


class Cv2Grabber:
    """OpenCV 的装配。`cv2` 在方法内 import。

    句柄与元信息都挂在实例上：`open` 时从 cap 读一次 fps 与总帧数存下来，
    `read_at` 用它们做上限钳位。这样 `read_at` 不依赖调用方是否先调过 `probe`
    ——顺序错了不该变成"取不到帧"这种难查的现象。
    """

    def __init__(self) -> None:
        self._cap: Any = None
        self._path: str | None = None
        self._fps: float = 0.0
        self._duration_sec: float = 0.0

    def probe(self, video_path: str) -> VideoMeta:
        """读容器头拿元信息，不解码任何一帧。

        `fps` 声明为 0 是常见情形（抖音的 mp4 有），此时 `duration_sec` 给 0.0
        而不是除零。`plan_scan` 收到 0 会返回 None，调用方据此返回空 `FrameSet`
        ——所以这里不需要抛异常，把「不知道」如实表达出来就是正确的。
        """
        import cv2

        cap = cv2.VideoCapture(video_path)
        try:
            if not cap.isOpened():
                raise VideoUnreadable(video_path, "cv2 打不开（缺编解码或不是视频）")
            fps = float(cap.get(cv2.CAP_PROP_FPS))
            frames = float(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        finally:
            cap.release()

        if width <= 0 or height <= 0:
            raise VideoUnreadable(video_path, "读不到画面尺寸（容器头损坏）")
        return VideoMeta(
            path=video_path,
            width=width,
            height=height,
            duration_sec=(frames / fps) if fps > 0 else 0.0,
            fps=fps,
        )

    def open(self, video_path: str) -> None:
        """持有解码句柄。重复调用先释放旧的，避免句柄泄漏。

        fps 与总帧数在这里再读一次（不依赖 `probe` 有没有被调用过）：
        `read_at` 的上限钳位需要它们，而钳位是**每次读帧都要用**的。
        """
        import cv2

        self.close()
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            cap.release()
            raise VideoUnreadable(video_path, "cv2 打不开（缺编解码或不是视频）")
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        frames = float(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self._cap = cap
        self._path = video_path
        self._fps = fps
        self._duration_sec = (frames / fps) if fps > 0 else 0.0

    def read_at(self, at_sec: float) -> FrameImage:
        """定位到 `at_sec` 附近的一帧并读出来。

        `at_sec` 先钳到 `[0, duration_sec - 1/fps]`：由实测可知
        `at_sec >= duration_sec` **必然**返回 `False`。`plan_picks` 给的是镜头中点，
        正常不会越界，但尾部补的段若 `end_sec` 恰好等于浮点 `duration_sec`，
        取中点仍可能因浮点误差擦到边界。钳一次比在这里抛异常干净。

        `ok=False` 只当「这一帧拿不到」，不推测是越界还是解码坏了 —— 实测越界后
        `CAP_PROP_POS_MSEC` 读回 0，位置信息不可信，猜不出来。
        """
        import cv2

        if self._cap is None:
            raise FrameReadFailed(at_sec, "还没 open()，没有解码句柄")
        limit = self._duration_sec - (1.0 / self._fps if self._fps > 0 else 0.0)
        target = min(max(at_sec, 0.0), max(limit, 0.0))
        self._cap.set(cv2.CAP_PROP_POS_MSEC, target * 1000.0)
        ok, frame = self._cap.read()
        if not ok:
            raise FrameReadFailed(at_sec, "cv2 read 返回 False")
        if frame is None:  # pragma: no cover - cv2 的契约是 ok=True 时 frame 非 None
            raise FrameReadFailed(at_sec, "cv2 返回了空帧但 ok=True")

        height = int(frame.shape[0])
        width = int(frame.shape[1])
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean, std = cv2.meanStdDev(gray)
        return FrameImage(
            data=frame,
            width=width,
            height=height,
            mean_luma=float(mean[0][0]),
            std_luma=float(std[0][0]),
        )

    def encode(self, image: FrameImage, long_side: int, quality: int) -> bytes:
        """缩到长边 `long_side` 后压成 JPEG 字节。

        **只缩不放**：320x240 的视频放大到 1024 不会多出任何信息，只会让文件
        大 10 倍。`long_side` 是上限而不是目标尺寸。

        `INTER_AREA` 是缩小的正确插值（区域平均），`INTER_LINEAR` 缩小会产生
        摩尔纹，对「读字幕」这个用途是实打实的损失。
        """
        import cv2

        if long_side <= 0:
            raise ValueError("long_side 必须是正数")
        if not 0 < quality <= 100:
            raise ValueError("quality 必须在 1–100")
        frame = image.data
        if frame is None:
            raise FrameReadFailed(0.0, "帧数据为空，无法编码")

        longest = max(image.width, image.height)
        if longest > long_side:
            scale = long_side / longest
            frame = cv2.resize(
                frame,
                (
                    max(1, int(round(image.width * scale))),
                    max(1, int(round(image.height * scale))),
                ),
                interpolation=cv2.INTER_AREA,
            )
        ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise FrameReadFailed(0.0, "cv2.imencode 编码失败")
        return bytes(buffer.tobytes())

    def close(self) -> None:
        """释放解码句柄。不释放的话长跑 worker 会句柄耗尽。"""
        if self._cap is not None:
            self._cap.release()
        self._cap = None
        self._path = None
        self._fps = 0.0
        self._duration_sec = 0.0

"""切镜头：把视频按画面硬切处切成若干镜头区间。

`ShotCutter` 是一个只有方法的 Protocol，真实现 `SceneDetectCutter` 把
PySceneDetect 接进来。假实现（测试里的）只要也提供 `detect` 就能替换——
本层不需要知道镜头是"怎么"切出来的，只需要一串左闭右开的时间区间。

**外部库在方法体内 import，不在模块顶层。** 这不是洁癖：`kb/vision/` 的纯逻辑
单测（`tests/test_vision_plan.py`）不该因为"没装 scenedetect"而收集失败，而本机
与 CI 装的东西并不一致。放方法内，任何环境下 `import kb.vision.cut` 都成功。

## 为什么不用 `scenedetect.detect()` 便捷函数

交接文档已记录踩坑：`detect(path, ContentDetector())` 用默认阈值 27.0，对纯色/
低对比画面返回 0 个镜头。统一走 `open_video` + `SceneManager` + 显式
`ContentDetector(threshold=15.0)`，阈值成为可调参数而不是隐藏默认值。

## 0.6.6 的三个接口差异（按 0.7.x 写会在平板上直接崩）

| 项 | 0.6.6 的实际行为 |
|---|---|
| `FrameTimecode` | **只有 `get_seconds()`，没有 `.seconds` 属性** |
| `ContentDetector(min_scene_len=)` | 只收**整数帧数**；传 float 秒会 `TypeError: slice indices must be integers` |
| `get_scene_list()` | `start_in_scene` 默认 **False**，必须显式传 True |
"""

from __future__ import annotations

import logging
from typing import Protocol

from .models import ScanPlan, Shot

log = logging.getLogger(__name__)

# 默认内容阈值。0.6.6 的默认值是 27.0，对纯色/低对比画面会漏检（实测返回 0 个镜头）。
# 15.0 是本机与平板都验过的值：合成三镜头视频切点精确到帧。
DEFAULT_THRESHOLD = 15.0


class ShotCutter(Protocol):
    """把视频切成镜头。

    只要求这一个方法：真实现与测试里的假实现都能满足，`frames.py` 不需要
    知道 PySceneDetect 的任何细节。返回的 `Shot.index` 先按检测顺序给，
    `merge_shots` 会重新编号（因为要合并碎片与补尾部截断）。
    """

    def detect(
        self, video_path: str, plan: ScanPlan, min_shot_sec: float
    ) -> tuple[Shot, ...]: ...


class SceneDetectCutter:
    """PySceneDetect 0.6.6 的装配。

    `threshold` 可调是为了留一条对照路径：闪烁视频（片头频闪）在低阈值下会切出
    几十个碎片，靠 `min_shot_sec` 兜住；若某类视频阈值确实不合适，调用方可以
    按内容调高，不必改代码。
    """

    def __init__(self, threshold: float = DEFAULT_THRESHOLD) -> None:
        self._threshold = threshold

    def detect(
        self, video_path: str, plan: ScanPlan, min_shot_sec: float
    ) -> tuple[Shot, ...]:
        """检测镜头。异常不在这里吞掉，交给 `frames.py` 决定降级方式。

        两处参数的口径必须盯住：

        - `min_scene_len` 传**整数帧数**：`int(round(fps * min_shot_sec))`，且至少 1。
          0.6.6 内部拿它当切片长度，float 会直接抛 TypeError。
        - `get_scene_list(start_in_scene=True)` **必传**。不带它时无切点的视频返回
          `[]`——「口播视频全程一个机位」这条正常路径会静默变成零个镜头，
          下游于是以为"这条视频没画面"。实测：50 帧纯静态画面，
          不带参数 0 个镜头，带参数 1 个镜头 `[(0.0, 2.0)]`。
        """
        from scenedetect import ContentDetector, SceneManager, open_video

        video = open_video(video_path)
        fps = float(video.frame_rate)
        min_len_frames = max(1, int(round(fps * min_shot_sec)))
        manager = SceneManager()
        manager.add_detector(
            ContentDetector(threshold=self._threshold, min_scene_len=min_len_frames)
        )
        # duration 传 float 秒：实测 0.6.6 接受，60 秒视频传 30.0 只返回前 30 秒的镜头。
        manager.detect_scenes(video, duration=plan.scan_sec, frame_skip=plan.frame_skip)
        spans = manager.get_scene_list(start_in_scene=True)
        shots = tuple(
            Shot(
                index=i,
                start_sec=float(start.get_seconds()),
                end_sec=float(end.get_seconds()),
            )
            for i, (start, end) in enumerate(spans)
        )
        log.debug(
            "切镜头 %s：fps=%.3f min_scene_len=%d 帧 跳帧=%d 扫描=%.1f 秒 → %d 个镜头",
            video_path,
            fps,
            min_len_frames,
            plan.frame_skip,
            plan.scan_sec,
            len(shots),
        )
        return shots

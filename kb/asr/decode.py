"""分段识别的装配：切段 → 逐段解码 → 拼回全文。

**模型加载与本模块分开**：`recognizer` 由调用方注入。理由是加载一次 int8 模型
在平板上要 0.8 秒、占约 2.8 GB 峰值内存（技术方案 §4.11 实测）；每条音频重新加载
会把 worker 拖垮。注入让「加载一次、复用多次」成为调用方的选择，而不是本模块的
隐藏行为。

**为什么按顺序单线程解码**：分段已经让 RTF 降到 0.024（448 秒音频 10.8 秒跑完），
并行收益有限，却会同时占用多份模型内存——而 2.8 GB/份在 8 GB 平板上刚好是
上次 fp32 崩溃的同一个坑。单线程是这里的安全选择。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, Sequence

from .segment import (
    DEFAULT_SEARCH_SEC,
    DEFAULT_WINDOW_SEC,
    frame_energy,
    join_texts,
    peak_abs,
    plan_segments,
)

log = logging.getLogger(__name__)

# 算能量时每帧含多少采样。与 segment.FRAME_SEC 保持一致：160 采样 = 16kHz 下 10 毫秒。
_ENERGY_FRAME_SAMPLES = 160


class Decoder(Protocol):
    """解码一段音频，返回文本。

    只要求这一个方法：sherpa-onnx 的 `OfflineStream` 与测试里的假实现
    都能满足，本模块不需要知道模型的任何细节。
    """

    def decode(self, samples: Sequence[float], sample_rate: int) -> str: ...


@dataclass(frozen=True)
class SegmentText:
    """一段的识别结果，带时间区间。

    `start_sec` / `end_sec` 保留下来不是为了界面显示，是为了**排查**：
    摘要出现奇怪的词时，要能定位到原始音频的第几秒去复核。
    """

    start_sec: float
    end_sec: float
    text: str


@dataclass(frozen=True)
class Transcript:
    """整条音频的转写结果。"""

    text: str
    segments: tuple[SegmentText, ...]

    @property
    def chars(self) -> int:
        return len(self.text)


def transcribe(
    samples: Sequence[float],
    sample_rate: int,
    decoder: Decoder,
    window_sec: float = DEFAULT_WINDOW_SEC,
    search_sec: float = DEFAULT_SEARCH_SEC,
) -> Transcript:
    """把整段音频切成小段分别解码，拼成全文。

    `samples` 是 [-1, 1] 的浮点采样（sherpa-onnx 的输入口径）。传入 16 位
    整数会被当成超大浮点数，识别结果全是乱码——所以这里显式校验取值范围，
    把口径错误挡在这一层。

    单段解码失败（模型抛异常）不放弃整条音频：记一笔、跳过该段、继续下一段。
    理由和 worker 收异常一样——一条坏数据不该让整条音频白跑。
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate 必须是正数")
    # 用 len() 而不是 `not samples`：调用方几乎总传 numpy 数组，
    # 而 numpy 对 `not array` 是「元素真值有歧义」直接报错——真机第一次跑就撞上了。
    if len(samples) == 0:
        return Transcript(text="", segments=())

    peak = peak_abs(samples)
    if peak > 1.5:
        raise ValueError(
            f"采样值超出 [-1, 1]（峰值 {peak:.1f}）：这是 16 位整数的口径，"
            "请先除以 32768 转成浮点"
        )

    energy = frame_energy(samples, _ENERGY_FRAME_SAMPLES)
    plan = plan_segments(
        total_samples=len(samples),
        sample_rate=sample_rate,
        window_sec=window_sec,
        search_sec=search_sec,
        energy=energy,
    )

    parts: list[SegmentText] = []
    for index, (start, end) in enumerate(plan.spans):
        chunk = samples[start:end]
        try:
            text = decoder.decode(chunk, sample_rate)
        except Exception as exc:  # noqa: BLE001 单段失败不放弃整条音频
            log.warning(
                "第 %d 段解码失败（%.1f–%.1f 秒）：%s: %s",
                index,
                start / sample_rate,
                end / sample_rate,
                type(exc).__name__,
                exc,
            )
            continue
        parts.append(
            SegmentText(
                start_sec=round(start / sample_rate, 2),
                end_sec=round(end / sample_rate, 2),
                text=text,
            )
        )

    return Transcript(text=join_texts([p.text for p in parts]), segments=tuple(parts))


class SherpaDecoder:
    """用 sherpa-onnx 的 SenseVoice 做解码。

    注意 `num_threads=4`：平板 8 核，留一半给别的服务。调满会和其他进程抢核，
    整体反而更慢（技术方案 §4.11 的实测口径就是 4 线程，保持可比）。
    """

    def __init__(
        self,
        model: str,
        tokens: str,
        num_threads: int = 4,
        use_itn: bool = True,
    ) -> None:
        import sherpa_onnx

        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=model,
            tokens=tokens,
            num_threads=num_threads,
            use_itn=use_itn,
            debug=False,
        )

    def decode(self, samples: Sequence[float], sample_rate: int) -> str:
        stream = self._recognizer.create_stream()
        stream.accept_waveform(sample_rate, samples)
        self._recognizer.decode_stream(stream)
        return str(stream.result.text)

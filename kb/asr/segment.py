"""切点规划：把长音频切成若干段，切点尽量落在安静处。

**为什么不按固定 30 秒硬切**（2026-11-27 实测，428 秒中文、切点故意落在句中）：

| 切法 | 输出字数 | 理论字数 | 说明 |
|---|---|---|---|
| 硬切 30 秒 | 1486 | 1480 | 切口处多吐字，多出来的 6 字是模型在半个音节上的幻觉 |
| 切点吸附安静处 | **1478** | 1480 | 最接近理论值，标点也更完整 |

一句话：模型在「话说到一半被掐断」的地方会补字。把刀落在换气的静音里，
就没有这半个音节可补。

**为什么不做重叠 + 去重**（FunASR 官方给的替代方案是 30 秒窗口 + 2 秒重叠）：
实测这条路在本项目更差。按后缀-前缀最长匹配去重后得 1610 字，比理论值多 130 字
——识别结果在重叠区并不逐字相同（同一个字这次带标点、下次不带），
字符串匹配找不到该去的地方，反而把重复内容留了下来。
**去重靠文字匹配不可靠，靠音频本身找静音才可靠。**

本模块只做算术，不 import 模型、不碰数据库：切点对不对可以用合成信号验，
不必等模型跑完。
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from typing import Any, Sequence

# 一个窗口的默认时长（秒）。取 30 的依据：SenseVoice 训练与官方示例的输入上限
# 就是 30 秒，超过它准确率开始掉（技术方案 §4.11 的 445 秒实测）。
DEFAULT_WINDOW_SEC = 30.0

# 切点允许前后挪动的范围（秒）。太小则找不到静音，太大则单段时长偏离 30 秒过多，
# 又会碰到退化。实测 ±2 秒足以覆盖正常说话的换气间隔。
DEFAULT_SEARCH_SEC = 2.0

# 算能量时每帧的时长（秒）。10 毫秒是语音处理的常用粒度：再细没意义，
# 再粗会把 20 毫秒的短停顿抹平。
FRAME_SEC = 0.01

# 「尾巴短到不值得单独成段」的阈值（秒）。
#
# 取 1 秒，不取接近窗口长度的值。原实现用 `window - search`（28 秒）当阈值，
# 含义变成「不到 28 秒的尾巴都并掉」——25 秒的尾巴被并进前一段，
# 拼出 55 秒的长段（2026-11-27 平板真机复现）。
#
# 1 秒的依据：一段 1 秒的语音通常是一个词或半句话，单独解码能得到有效内容；
# 短于 1 秒基本只有气口和环境噪声，模型容易吐幻觉字，并进前一段更干净。
MIN_TAIL_SEC = 1.0


@dataclass(frozen=True)
class Plan:
    """切点方案。

    `spans` 是左闭右开的采样点区间，直接拿去切片：`samples[start:end]`。
    这些区间**首尾相接、不重叠**——重叠方案已被实测否决，见模块 docstring。
    """

    spans: tuple[tuple[int, int], ...]
    sample_rate: int

    @property
    def count(self) -> int:
        return len(self.spans)

    def duration_sec(self, index: int) -> float:
        start, end = self.spans[index]
        return (end - start) / self.sample_rate


def frame_energy(samples: Sequence[int] | Sequence[float], hop: int) -> list[float]:
    """按 `hop` 个采样一帧，算每帧的平均绝对值。

    用平均绝对值而不是均方根：只为比较「哪一帧更安静」，两者排序一致，
    而绝对值不用开方，快一个量级。

    有 numpy 且输入不是 list 时走向量化路径。这不是过早优化：448 秒音频有
    718 万个采样，纯 Python 逐元素循环要 3 秒以上，而识别本身才 10.8 秒——
    预处理吃掉三成时间是荒谬的。
    """
    if hop <= 0:
        raise ValueError("hop 必须是正数")
    frames = len(samples) // hop
    if frames == 0:
        return []

    np = _numpy_or_none()
    if np is not None and not isinstance(samples, (list, tuple)):
        arr = np.abs(np.asarray(samples, dtype=np.float64))[: frames * hop]
        return [float(v) for v in arr.reshape(frames, hop).mean(axis=1)]

    out: list[float] = []
    for i in range(0, frames * hop, hop):
        chunk = samples[i : i + hop]
        out.append(sum(abs(float(v)) for v in chunk) / hop)
    return out


def peak_abs(samples: Sequence[int] | Sequence[float]) -> float:
    """整段采样的最大绝对值。用来校验输入口径（16 位整数 vs [-1,1] 浮点）。

    不能用「取首尾几个点」代替：静音开头、静音结尾的音频会全部命中 0，
    校验就形同虚设。
    """
    if len(samples) == 0:
        return 0.0
    np = _numpy_or_none()
    if np is not None and not isinstance(samples, (list, tuple)):
        return float(np.abs(np.asarray(samples, dtype=np.float64)).max())
    return max(abs(float(v)) for v in samples)


def _numpy_or_none() -> Any:
    """可选依赖：装了就用。**不写进 pyproject 的 dependencies**——本模块的
    纯 Python 路径是功能完整的，numpy 只是加速；把它变成硬依赖会让文档检查、
    CI 的最小环境也要装 12 MB。"""
    try:
        import numpy
    except ImportError:
        return None
    return numpy


def quietest_near(
    energy: Sequence[float],
    pos: int,
    radius_frames: int,
    fallback: int,
) -> int:
    """在 `pos` 前后 `radius_frames` 帧内找能量最低的一帧，返回其帧号。

    找不到（`energy` 为空或半径无效）时返回 `fallback`，让调用方拿到一个
    确定的、可解释的结果，而不是抛异常打断整条音频的转写。

    同能量时取**离 `pos` 更近**的：能让每段时长更贴近 30 秒，减少退化风险。
    """
    if not energy:
        return fallback
    lo = max(0, pos - radius_frames)
    hi = min(len(energy) - 1, pos + radius_frames)
    if lo > hi:
        return fallback
    best = fallback
    best_energy: float | None = None
    best_dist = None
    for idx in range(lo, hi + 1):
        e = energy[idx]
        dist = abs(idx - pos)
        if (
            best_energy is None
            or e < best_energy - 1e-9
            or (
                abs(e - best_energy) <= 1e-9
                and best_dist is not None
                and dist < best_dist
            )
        ):
            best, best_energy, best_dist = idx, e, dist
    return best


def plan_segments(
    total_samples: int,
    sample_rate: int,
    window_sec: float = DEFAULT_WINDOW_SEC,
    search_sec: float = DEFAULT_SEARCH_SEC,
    energy: Sequence[float] | None = None,
) -> Plan:
    """规划切点。切点落在标称位置附近能量最低处。

        `energy` 由 `frame_energy` 预先算好传入。传 `None` 表示不做吸附、等距硬切
        ——留这条路是为了能用「纯算术」验证窗口划分本身，也是没有音频时的兜底。

        单段时长的硬边界：`[window_sec - search_sec, window_sec + search_sec]`。
    这个边界保证两件事：
        1. 不会因为连续几次向前吸附而把段越切越短（吸附偏移不累积）；
        2. 吸附后的段不会超过 `window_sec + search_sec`（默认 32 秒）。

    **尾巴的处理**：剩余不足 `MIN_TAIL_SEC`（1 秒）时并未进前一段，但**只有当前一段
    尚未吃满 `max_len` 才并**。否则宁可留一个短尾巴单独成段——短段只是多一次解码，
    超过 30 秒却会让模型重新退化，那是分段的初衷所反对的。
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate 必须是正数")
    if window_sec <= 0:
        raise ValueError("window_sec 必须是正数")
    if search_sec < 0:
        raise ValueError("search_sec 不能为负")
    if total_samples <= 0:
        return Plan(spans=(), sample_rate=sample_rate)

    frame = max(1, int(sample_rate * FRAME_SEC))
    radius_frames = int(search_sec / FRAME_SEC) if energy is not None else 0
    window = int(window_sec * sample_rate)
    min_len = max(1, int((window_sec - search_sec) * sample_rate))
    max_len = int((window_sec + search_sec) * sample_rate)
    min_tail = int(MIN_TAIL_SEC * sample_rate)

    spans: list[tuple[int, int]] = []
    pos = 0
    while pos < total_samples:
        nominal_end = pos + window
        if nominal_end >= total_samples:
            spans.append((pos, total_samples))
            break

        end = nominal_end
        if energy is not None and radius_frames > 0:
            frame_pos = nominal_end // frame
            snapped = quietest_near(energy, frame_pos, radius_frames, frame_pos)
            candidate = snapped * frame
            # 吸附结果必须落在允许区间内，否则退回标称位置。
            # 这一步是「不信任算法」的护栏：能量平坦（纯噪声）时，
            # 最低帧可能恰好在半径边缘，会把段拉成 28 秒或 32 秒。
            if pos + min_len <= candidate <= pos + max_len:
                end = candidate

        # 收尾保护：尾巴短到不值得单独成段时，并进当前段。
        # 单独一段 0.3 秒的音频，模型只会吐出噪声。
        #
        # **但只有当前一段还没吃满时才允许并**（`total_samples - pos <= max_len`）。
        # 少了这半个条件，就会切出超过模型上限的长段——2026-11-27 平板真机：
        # 445.1 秒抖音视频最后一段变成 55.1 秒，而 SenseVoice 的输入上限是 30 秒，
        # 分段本来是为了躲开这个上限，结果自己又踩进去了。
        if total_samples - end < min_tail and total_samples - pos <= max_len:
            end = total_samples

        spans.append((pos, end))
        pos = end

    return Plan(spans=tuple(spans), sample_rate=sample_rate)


def join_texts(texts: Sequence[str]) -> str:
    """把各段识别结果按顺序拼成一段文本。

    **不插任何分隔符**。理由：每段末尾模型通常已经给了句号或逗号，
    再补一个逗号会出现「，。」这种痕迹。段与段之间本该断句的地方，
    由标点模型或后续摘要环节处理，不在这里猜。

    只做一件事：去掉每段首尾空白，避免英文内容拼出 `word  word`。
    """
    return "".join(t.strip() for t in texts if t and t.strip())


def pcm_from_bytes(raw: bytes) -> array:
    """把 16 位小端 PCM 字节转成采样数组。

    单独抽出来是因为 `array` 的字节序依赖平台，而音频来源（PyAV、wav 文件）
    都是小端。显式转换比依赖 `sys.byteorder` 更少意外。
    """
    samples = array("h")
    samples.frombytes(raw)
    import sys

    if sys.byteorder == "big":
        samples.byteswap()
    return samples

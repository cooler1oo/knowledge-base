"""分段解码的纯逻辑单测：不加载模型、不连库，毫秒级跑完。

这些用例钉住的是 2026-11-27 实测暴露的三个坑，不是凭空设计的接口：

1. **硬切会在切口处多吐字** —— 用合成信号验证「吸附到静音」确实把刀挪到了安静处。
2. **重叠 + 文字去重不可靠** —— 所以本实现不做重叠，用 `test_切点首尾相接不重叠` 钉死。
3. **16 位整数口径会让结果全乱** —— 用 `test_传入16位整数口径要报错` 挡住。

真模型的行为由 `scripts/asr_test.py` 在平板上验证，不放进 pytest：
它要 228 MB 模型，CI 装不动，跑一次几十秒。
"""

from __future__ import annotations

import math

from kb.asr.decode import Transcript, transcribe
from kb.asr.segment import (
    frame_energy,
    join_texts,
    pcm_from_bytes,
    plan_segments,
    quietest_near,
)

SR = 16000


class FakeDecoder:
    """假解码器：返回这一段里含多少个非零采样，方便断言「切了几段、每段多长」。

    记录每次收到的长度，用来验证切点划分，不关心文本本身。
    """

    def __init__(self) -> None:
        self.calls: list[int] = []

    def decode(self, samples, sample_rate: int) -> str:
        assert sample_rate == SR
        self.calls.append(len(samples))
        return f"[{len(samples)}]"


class ExplodingDecoder:
    """第 N 段抛异常，用来验证「单段失败不影响整条音频」。"""

    def __init__(self, fail_at: int) -> None:
        self.fail_at = fail_at
        self.seen = 0

    def decode(self, samples, sample_rate: int) -> str:
        index = self.seen
        self.seen += 1
        if index == self.fail_at:
            raise RuntimeError("模型在这段崩了")
        return "好"


def tone(n: int, amp: float = 0.3) -> list[float]:
    """正弦波，模拟「有人在说话」。"""
    return [amp * math.sin(2 * math.pi * 220 * i / SR) for i in range(n)]


def silence(n: int) -> list[float]:
    return [0.0] * n


# ── 切点规划：窗口划分 ──────────────────────────────────────


def test_短音频只有一段_不切():
    """10 秒音频小于 30 秒窗口，切了只会更差。"""
    plan = plan_segments(total_samples=10 * SR, sample_rate=SR)

    assert plan.count == 1
    assert plan.spans[0] == (0, 10 * SR)


def test_正好一个窗口_也是一段():
    plan = plan_segments(total_samples=30 * SR, sample_rate=SR)

    assert plan.count == 1
    assert plan.spans[0] == (0, 30 * SR)


def test_长音频切成多段_覆盖全部采样不丢不重():
    """首尾相接是关键：漏一段就丢内容，重叠就重复。"""
    total = 125 * SR

    plan = plan_segments(total_samples=total, sample_rate=SR)

    assert plan.spans[0][0] == 0
    assert plan.spans[-1][1] == total
    for (_, prev_end), (next_start, _) in zip(plan.spans, plan.spans[1:]):
        assert prev_end == next_start, "段与段之间必须首尾相接"


def test_空音频得到零段():
    plan = plan_segments(total_samples=0, sample_rate=SR)

    assert plan.count == 0


def test_参数非法要报错():
    import pytest

    with pytest.raises(ValueError):
        plan_segments(total_samples=100, sample_rate=0)
    with pytest.raises(ValueError):
        plan_segments(total_samples=100, sample_rate=SR, window_sec=0)
    with pytest.raises(ValueError):
        plan_segments(total_samples=100, sample_rate=SR, search_sec=-1)


# ── 切点规划：吸附到静音 ────────────────────────────────────


def test_静音处在标称切点之后_刀要往后挪():
    """模型在半个音节上会补字，所以要把刀放到静音里。

    `energy` 长度 4000 帧，标称切点在第 1000 帧，静音在第 1100 帧。
    半径 200 帧必须能覆盖到它。
    """
    energy = [1000.0] * 4000
    for i in range(1100, 1110):
        energy[i] = 0.0

    picked = quietest_near(energy, pos=1000, radius_frames=200, fallback=1000)

    assert 1100 <= picked <= 1109, "应该选中静音区间里的某一帧"


def test_静音处在标称切点之前_刀要往前挪():
    energy = [1000.0] * 4000
    energy[900] = 0.0

    picked = quietest_near(energy, pos=1000, radius_frames=200, fallback=1000)

    assert picked == 900


def test_半径内没有静音_取最近的最安静帧():
    """全是噪声时不能乱挪太远，否则段长会偏离 30 秒。"""
    energy = [1000.0, 1000.0, 500.0, 1000.0, 1000.0]

    picked = quietest_near(energy, pos=2, radius_frames=2, fallback=2)

    assert picked == 2
    assert abs(picked - 2) <= 2, "不能超出半径"


def test_能量全相同时取离标称切点最近的():
    """纯噪声（能量平坦）下，取最靠中间的，避免段长被拉到边界。"""
    energy = [100.0] * 4000

    picked = quietest_near(energy, pos=1000, radius_frames=200, fallback=1000)

    assert picked == 1000


def test_能量为空时返回兜底值_不抛异常():
    assert quietest_near([], pos=10, radius_frames=5, fallback=10) == 10


def test_吸附后的段长不超出允许区间():
    """这条是护栏：静音恰好落在半径边缘时，段长会变成 28 或 32 秒。

    32 秒已经超过 SenseVoice 的 30 秒上限，会重新引入退化。
    """
    total = 125 * SR
    window = int(30 * SR)
    # 构造：每个标称切点前 1.9 秒处全静音，其余是语音
    energy = [800.0] * (total // 160)
    for cut in range(window, total, window):
        idx = (cut - int(1.9 * SR)) // 160
        energy[idx] = 0.0

    plan = plan_segments(total, SR, energy=energy)

    for i in range(plan.count - 1):  # 最后一段是尾巴，允许短
        length = plan.spans[i][1] - plan.spans[i][0]
        assert int(28 * SR) <= length <= int(32 * SR), f"第 {i} 段长 {length}"


def test_尾巴太短要并进前一段():
    """剩余 1 秒单独成段，模型只会吐噪声。"""
    total = 30 * SR + SR // 2  # 30 秒 + 0.5 秒

    plan = plan_segments(total_samples=total, sample_rate=SR)

    assert plan.count == 1
    assert plan.spans[0][1] == total


# ── 单段时长上限（2026-11-27 平板真机撞出来的）──────────────
#
# 平板跑真实抖音视频（445.1 秒）时最后一段是 **55.1 秒**。
# 根因：原实现「尾巴不到一个窗口就并进前一段」没区分尾巴有多长——
# 前一段已满额 30 秒，再并进 25 秒尾巴就成了 55 秒。
# 这正好踩中分段要避免的退化：SenseVoice 的输入上限就是 30 秒。
#
# 下面四条覆盖三个真实复现长度（都在本机复现过），外加一条扫长度。


def test_445秒音频最后一段不能超过32秒():
    """平板真实视频的长度。原实现最后一段 55.1 秒。"""
    total = int(445.1 * SR)

    plan = plan_segments(total_samples=total, sample_rate=SR)

    for i in range(plan.count):
        length = plan.duration_sec(i)
        assert length <= 32.0, f"第 {i} 段 {length:.1f} 秒超限"


def test_33秒音频不能被并成一段():
    """剩 3 秒尾巴，并进前一段会变成 33 秒。原实现就这么干的。"""
    plan = plan_segments(total_samples=33 * SR, sample_rate=SR)

    for i in range(plan.count):
        length = plan.duration_sec(i)
        assert length <= 32.0, f"第 {i} 段 {length:.1f} 秒超限"


def test_92秒音频不能并出32点3秒():
    """剩 2.3 秒尾巴，原实现并出 32.3 秒。"""
    plan = plan_segments(total_samples=int(92.3 * SR), sample_rate=SR)

    for i in range(plan.count):
        length = plan.duration_sec(i)
        assert length <= 32.0, f"第 {i} 段 {length:.1f} 秒超限"


def test_任何长度都不产生超限段且不漏内容():
    """扫多个长度，防止只修了某一种尾巴。"""
    for total_sec in (31, 33, 45, 60.5, 61, 92.3, 120, 445.1, 600):
        total = int(total_sec * SR)
        plan = plan_segments(total_samples=total, sample_rate=SR)
        for i in range(plan.count):
            length = plan.duration_sec(i)
            assert length <= 32.0, f"总长 {total_sec}s 的第 {i} 段 {length:.1f} 秒超限"
        assert plan.spans[0][0] == 0
        assert plan.spans[-1][1] == total, f"总长 {total_sec}s 漏了内容"


# ── 能量计算 ───────────────────────────────────────────────


def test_能量的帧数按hop切分():
    energy = frame_energy([100, -100, 100, -100, 50], hop=2)

    assert len(energy) == 2, "5 个采样、hop=2，只有 2 个完整帧"
    assert energy[0] == 100.0
    assert energy[1] == 100.0


def test_静音帧能量为零():
    assert frame_energy([0] * 100, hop=10) == [0.0] * 10


def test_hop非法要报错():
    import pytest

    with pytest.raises(ValueError):
        frame_energy([1, 2, 3], hop=0)


# ── 文本拼接 ───────────────────────────────────────────────


def test_拼接不加分隔符():
    """每段末尾模型已经带标点，再补逗号会出现「，。」这种痕迹。"""
    assert join_texts(["今天下雨。", "记得带伞。"]) == "今天下雨。记得带伞。"


def test_拼接要去掉段首尾空白():
    assert join_texts(["  hello ", " world  "]) == "helloworld"


def test_拼接跳过空段():
    assert join_texts(["好", "", "  ", "的"]) == "好的"


# ── 整条流程 ───────────────────────────────────────────────


def test_整条音频被完整解码_每段都送到模型():
    """长短两段音频，验证段数等于规划结果。"""
    samples = tone(125 * SR)
    decoder = FakeDecoder()

    result = transcribe(samples, SR, decoder)

    assert decoder.calls, "必须真的喂给模型"
    assert sum(decoder.calls) == len(samples), "每段长度之和要等于总长，不丢不重"
    assert isinstance(result, Transcript)


def test_转写结果带每段的时间区间():
    samples = tone(65 * SR)
    decoder = FakeDecoder()

    result = transcribe(samples, SR, decoder)

    assert len(result.segments) >= 2
    assert result.segments[0].start_sec == 0.0
    # 区间要递增，不能重叠——排查时靠它定位到原始音频的秒数
    for prev, nxt in zip(result.segments, result.segments[1:]):
        assert prev.end_sec <= nxt.start_sec


def test_空音频返回空转写():
    result = transcribe([], SR, FakeDecoder())

    assert result.text == ""
    assert result.segments == ()


def test_单段解码失败不放弃整条音频():
    """一块坏数据不该让整条音频白跑，理由同 worker 收异常。"""
    samples = tone(65 * SR)
    fail_at = 1

    # 先问「不崩的话应该是几段」，再断言「崩一段就少一段」。
    # 不写死段数：窗口划分策略一调，写死的数字就会假报警——这会真的发生过。
    expected = len(transcribe(samples, SR, FakeDecoder()).segments)
    result = transcribe(samples, SR, ExplodingDecoder(fail_at=fail_at))

    assert result.segments, "第 2 段崩了，其余段仍要有结果"
    assert len(result.segments) == expected - 1
    # 崩掉那段的区间不该出现在结果里，否则时间轴会错位
    failed_start = 30.0
    assert all(
        s.start_sec >= failed_start + 28 or s.start_sec < failed_start
        for s in result.segments
    )


def test_传入16位整数口径要报错():
    """采样值 32767 会被当成超大浮点数，结果全是乱码。这条挡住口径错误。"""
    import pytest

    with pytest.raises(ValueError, match="16 位整数"):
        transcribe([32767.0, -32768.0, 1000.0], SR, FakeDecoder())


def test_pcm字节转采样_小端正确():
    import struct

    raw = struct.pack("<4h", 100, -100, 32767, -32768)

    samples = pcm_from_bytes(raw)

    assert list(samples) == [100, -100, 32767, -32768]


# ── numpy 数组输入（2026-11-27 真机撞出来的）────────────────
#
# 下面两条都是「用 list 测全绿、接真音频立刻崩」的类型。
# 真音频从 PyAV / wave 出来就是 numpy 数组，不是 list。


def test_numpy数组输入要走得通():
    """`not samples` 对 numpy 数组会抛「元素真值有歧义」。

    这是第一次用真模型跑就撞上的错误，单测当时只用 list 所以没拦住。
    """
    np = __import__("pytest").importorskip("numpy")
    samples = np.zeros(65 * SR, dtype=np.float32)
    samples[::2] = 0.3

    result = transcribe(samples, SR, FakeDecoder())

    assert result.segments, "numpy 输入必须能正常分段"


def test_numpy空数组也返回空转写():
    np = __import__("pytest").importorskip("numpy")

    result = transcribe(np.array([], dtype=np.float32), SR, FakeDecoder())

    assert result.text == ""


def test_空list也返回空转写():
    assert transcribe([], SR, FakeDecoder()).text == ""


def test_结尾静音的长音频_峰值校验仍能生效():
    """峰值只看首尾会漏：静音开头静音结尾时首尾都是 0，校验形同虚设。

    这里首尾静音、中间是 16 位整数口径，必须照样报错。
    """
    import pytest

    samples = silence(SR) + [20000.0] * SR + silence(SR)

    with pytest.raises(ValueError, match="16 位整数"):
        transcribe(samples, SR, FakeDecoder())


def test_峰值计算取整段最大而非首尾():
    from kb.asr.segment import peak_abs

    assert peak_abs([0.0, 0.0, 0.9, 0.0]) == 0.9
    assert peak_abs([]) == 0.0

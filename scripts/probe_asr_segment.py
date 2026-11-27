"""分段解码对照探针：同一段音频，整段 vs 分段，看差多少。

为什么需要这个脚本：`kb/asr` 的单测全部用假解码器（不装模型也能跑），
真模型的表现必须另外验。这个脚本就是那条「真模型」的验证路径。

**它和 `scripts/asr_test.py` 的区别**：`asr_test.py` 测的是「视频能不能转写」，
一个数字（RTF）够用。本脚本测的是「分段到底有没有用」，必须做对照——
同一段音频跑两种方式，把字数、标点、耗时并排打出来，才看得出收益在哪。

跑法（平板，模型与视频已在容器内）：

    proot-distro login debian -- /opt/kb-venv/bin/python /opt/kb-app/scripts/probe_asr_segment.py \
        --audio /tmp/audio16k.wav

跑法（本机需先装 `sherpa-onnx` 并有模型）：

    python scripts/probe_asr_segment.py --audio samples/asr/long_448s.wav \
        --model samples/asr/model.int8.onnx --tokens samples/asr/tokens.txt

**验收口径**（v9.14 §4.11 定的）：分段后的字数偏差应 ≤ 2 字，且耗时显著低于整段。
偏差超过 2 字说明切点策略有问题；耗时没降说明分段没生效。
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

# 允许从仓库根直接跑：`python scripts/probe_asr_segment.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.asr.decode import SherpaDecoder, transcribe  # noqa: E402
from kb.asr.segment import DEFAULT_WINDOW_SEC  # noqa: E402

SR = 16000


def read_wav(path: str) -> list[float]:
    """读 16 位单声道 wav，转成 [-1, 1] 浮点。

    非 16 kHz 直接报错而不是悄悄重采样：重采样会让「RTF 对比」失去可比性，
    而本脚本的全部意义就是对比。
    """
    with wave.open(path, "rb") as w:
        if w.getframerate() != SR:
            raise SystemExit(f"采样率是 {w.getframerate()}，本脚本只认 {SR}")
        if w.getnchannels() != 1:
            raise SystemExit(f"声道数是 {w.getnchannels()}，本脚本只认单声道")
        raw = w.readframes(w.getnframes())
    import numpy as np

    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return [float(v) for v in pcm]


def count_punct(text: str) -> tuple[int, int]:
    return text.count("，"), text.count("。")


def main() -> int:
    ap = argparse.ArgumentParser(prog="probe_asr_segment")
    ap.add_argument("--audio", required=True, help="16kHz 单声道 wav")
    ap.add_argument("--model", default="/opt/kb-models/model.int8.onnx")
    ap.add_argument("--tokens", default="/opt/kb-models/tokens.txt")
    ap.add_argument("--window", type=float, default=DEFAULT_WINDOW_SEC)
    ap.add_argument(
        "--expect-chars", type=int, default=0, help="已知真值字数，给了就算偏差"
    )
    args = ap.parse_args()

    for label, path in (("模型", args.model), ("词表", args.tokens)):
        if not Path(path).exists():
            print(f"❌ {label}不存在：{path}")
            return 2

    print(f"=== 读音频 {args.audio} ===")
    samples = read_wav(args.audio)
    duration = len(samples) / SR
    print(f"  时长 {duration:.1f} 秒，{len(samples)} 个采样")

    print("\n=== 加载模型 ===")
    t0 = time.monotonic()
    decoder = SherpaDecoder(args.model, args.tokens)
    print(f"  加载耗时 {time.monotonic() - t0:.2f} 秒")

    print("\n=== A. 整段解码（对照）===")
    t0 = time.monotonic()
    whole = decoder.decode(samples, SR)
    t_whole = time.monotonic() - t0
    wc, wp = count_punct(whole)
    print(
        f"  耗时 {t_whole:.2f} 秒  RTF {t_whole / duration:.3f}  字数 {len(whole)}  逗号 {wc}  句号 {wp}"
    )

    print(f"\n=== B. 分段解码（窗口 {args.window:.0f} 秒，切点吸附静音）===")
    t0 = time.monotonic()
    result = transcribe(samples, SR, decoder, window_sec=args.window)
    t_seg = time.monotonic() - t0
    sc, sp = count_punct(result.text)
    print(
        f"  段数 {len(result.segments)}  耗时 {t_seg:.2f} 秒  RTF {t_seg / duration:.3f}"
    )
    print(f"  字数 {result.chars}  逗号 {sc}  句号 {sp}")
    print(
        "  段长："
        + " ".join(f"{s.end_sec - s.start_sec:.1f}s" for s in result.segments)
    )

    print("\n=== 对照结论 ===")
    if t_whole > 0:
        print(
            f"  速度：分段是整段的 {t_whole / t_seg:.1f} 倍"
            if t_seg > 0
            else "  速度：无法比较"
        )
    print(
        f"  字数：整段 {len(whole)} → 分段 {result.chars}（差 {result.chars - len(whole):+d}）"
    )
    print(f"  标点：整段 {wc + wp} 个 → 分段 {sc + sp} 个")
    if args.expect_chars:
        gap = result.chars - args.expect_chars
        verdict = "✅ 通过" if abs(gap) <= 2 else "❌ 偏差超标（>2 字）"
        print(f"  真值 {args.expect_chars} 字，偏差 {gap:+d} → {verdict}")
    else:
        print("  （没给 --expect-chars，不判断偏差是否合格）")

    print("\n=== 整段前 200 字 ===")
    print(whole[:200])
    print("\n=== 分段前 200 字 ===")
    print(result.text[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""ASR 实测脚本：视频 → 音频 → 中文转写，并记录耗时与内存。

用途：判断 sherpa-onnx + SenseVoice 在平板上的真实表现，
      为「摘要要不要走 ASR」这个决策提供数据，而不是靠猜。

跑法：
proot-distro login debian -- /opt/kb-venv/bin/python /root/asr_test.py
"""

import json
import os
import resource
import sys
import time
import wave

import numpy as np

VIDEO = "/tmp/v.mp4"
WAV = "/tmp/audio16k.wav"
MODEL_DIR = "/opt/kb-models"
SAMPLE_RATE = 16000


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def extract_audio() -> tuple[int, float]:
    """从 mp4 抽 16k 单声道 wav。返回 (采样点数, 耗时秒)。"""
    import av

    t0 = time.monotonic()
    container = av.open(VIDEO)
    stream = next((s for s in container.streams if s.type == "audio"), None)
    if stream is None:
        container.close()
        raise RuntimeError("视频里没有音频轨")

    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    chunks: list[np.ndarray] = []
    for frame in container.decode(stream):
        for out in resampler.resample(frame):
            chunks.append(out.to_ndarray().reshape(-1))
    # 冲刷重采样器
    for out in resampler.resample(None):
        chunks.append(out.to_ndarray().reshape(-1))
    container.close()

    if not chunks:
        raise RuntimeError("解出 0 个音频采样")

    pcm = np.concatenate(chunks).astype(np.int16)
    with wave.open(WAV, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())

    return len(pcm), time.monotonic() - t0


def read_wav() -> np.ndarray:
    with wave.open(WAV, "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(samples: np.ndarray) -> tuple[str, float]:
    """跑识别。返回 (文本, 耗时秒)。"""
    import sherpa_onnx

    t0 = time.monotonic()
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=f"{MODEL_DIR}/model.int8.onnx",
        tokens=f"{MODEL_DIR}/tokens.txt",
        num_threads=4,
        use_itn=True,
        debug=False,
    )
    stream = recognizer.create_stream()
    stream.accept_waveform(SAMPLE_RATE, samples)
    recognizer.decode_stream(stream)
    text = stream.result.text
    return text, time.monotonic() - t0


def main() -> int:
    print(f"=== 模型: {MODEL_DIR} ===")
    for f in ("model.int8.onnx", "tokens.txt"):
        p = os.path.join(MODEL_DIR, f)
        print(f"  {f}: {os.path.getsize(p) / 1024 / 1024:.1f} MB")

    print("\n=== 抽音频 ===")
    mem0 = peak_rss_mb()
    try:
        n_samples, t_audio = extract_audio()
        duration = n_samples / SAMPLE_RATE
        print(f"  采样点: {n_samples}  时长: {duration:.1f} 秒  耗时: {t_audio:.2f} 秒")
        print(f"  峰值内存: {peak_rss_mb():.0f} MB（此前 {mem0:.0f} MB）")
    except Exception as exc:  # noqa: BLE001 实测脚本要看到任何异常
        print(f"  失败: {type(exc).__name__}: {exc}")
        return 2

    print("\n=== 转写 ===")
    samples = read_wav()
    try:
        text, t_asr = transcribe(samples)
    except Exception as exc:  # noqa: BLE001
        print(f"  失败: {type(exc).__name__}: {exc}")
        return 3

    print(f"  耗时: {t_asr:.2f} 秒  峰值内存: {peak_rss_mb():.0f} MB")
    print(f"  RTF（实时率，越小越快）: {t_asr / duration:.3f}")
    print(f"  字数: {len(text)}")
    print("\n=== 转写结果（前 800 字）===")
    print(text[:800])

    out = {
        "duration_sec": round(duration, 2),
        "audio_extract_sec": round(t_audio, 2),
        "asr_sec": round(t_asr, 2),
        "rtf": round(t_asr / duration, 3),
        "peak_mb": round(peak_rss_mb(), 1),
        "text_len": len(text),
        "text_head": text[:300],
    }
    with open("/tmp/asr_result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n结果已存 /tmp/asr_result.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

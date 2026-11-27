"""抽帧探针：看抖音视频画面里到底有没有可用信息。

用户提出「按长度分时段抽帧 + 关键帧 + 转折帧，让 AI 看画面写摘要」。
要判断这个方案可行与否，必须先看画面里有什么。

不用 ffmpeg（容器内没有），用已装的 cv2。
按设想的三类帧各抽一张，落到 /tmp/frames/ 供人眼查看。
"""

import os
import sys

import cv2

VIDEO = "/tmp/v.mp4"
OUT = "/tmp/frames"


def main() -> int:
    if not os.path.exists(VIDEO):
        print(f"视频不存在：{VIDEO}")
        return 2

    os.makedirs(OUT, exist_ok=True)
    cap = cv2.VideoCapture(VIDEO)
    if not cap.isOpened():
        print("cv2 打不开视频（编解码器缺失？）")
        return 3

    fps = cap.get(cv2.CAP_PROP_FPS)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration_s = total / fps if fps else 0
    print(f"元信息: {w}x{h} fps={fps:.2f} 总帧={total} 时长={duration_s:.1f}s")

    # 1) 按长度分时段抽：均分 5 段，每段取中间帧
    picks: list[tuple[str, int]] = []
    for i in range(5):
        idx = int(total * (i + 0.5) / 5)
        picks.append((f"uniform_{i}_t{idx / fps:.1f}s", idx))

    # 2) 转折帧：用帧间差异找变化最大的位置（粗略版关键帧）
    step = max(1, total // 120)  # 最多采样 120 帧来比
    prev = None
    diffs: list[tuple[float, int]] = []
    for idx in range(0, total, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            continue
        small = cv2.resize(frame, (64, 64))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if prev is not None:
            diff = float(cv2.absdiff(gray, prev).mean())
            diffs.append((diff, idx))
        prev = gray

    # 变化最大的 3 个位置当作「转折帧」
    diffs.sort(reverse=True)
    for rank, (d, idx) in enumerate(diffs[:3]):
        picks.append((f"change_{rank}_diff{d:.1f}_t{idx / fps:.1f}s", idx))

    saved = 0
    for name, idx in picks:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            print(f"  [跳过] {name} 读不到")
            continue
        path = os.path.join(OUT, f"{name}.jpg")
        cv2.imwrite(path, frame)
        saved += 1
        print(f"  存帧: {os.path.basename(path)}")

    cap.release()
    print(f"\n共存 {saved} 张到 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

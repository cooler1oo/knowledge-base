"""抽帧探针：接**真实现**跑一条视频，把镜头表与帧数据打给人看。

## 为什么不放进 pytest

它要一条真实视频文件，而 CI 与容器里都没有可用的样本。放 pytest 里的结果是
`skip`，而 skip 多了之后没人再看——真出问题时也分不清「没跑」和「跑了通过」。
所以单列成脚本，需要验证时人工跑。

## 与 `scripts/probe_frames.py`（旧）的关系

旧脚本用自写的帧差法（均分 5 段 + 变化最大的 3 帧），新脚本用 PySceneDetect。
两者**并存不是冗余**：旧脚本是不依赖 `scenedetect` 的降级路径——如果
`scenedetect` 在某台机器上装不起来，旧脚本仍能抽帧。设计文档 §9.3 记了这一条。

对照点（同一条视频）：

| 项 | probe_frames.py（旧） | probe_frames_vision.py（新） |
|---|---|---|
| 选帧依据 | 均分时间段 + 帧间差异最大的 3 帧 | 切镜头后按时间分层，每层一帧 |
| 空白过滤 | 无 | 灰度标准差 < 2.0 判空白并跳过 |
| 输出 | 6–8 张，无时间区间元信息 | 至多 12 张，每张带所属镜头区间 |

## 用法

```bash
python scripts/probe_frames_vision.py --video /path/to/v.mp4 --out /tmp/frames
python scripts/probe_frames_vision.py --video v.mp4 --max-frames 6 --keep
```

`--keep` 表示保留帧文件。默认**不保留**：探针跑完就把临时目录删掉，避免
反复跑堆出几百 MB。要看图时必须显式 `--keep` 并给 `--out`。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

# 允许从仓库根直接跑：`python scripts/probe_frames_vision.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.vision.frames import DEFAULT_MAX_FRAMES, extract_frames  # noqa: E402
from kb.vision.models import FrameSet, VideoUnreadable  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="probe_frames_vision.py",
        description="用 PySceneDetect + OpenCV 抽帧，打印镜头表与每帧数据",
    )
    parser.add_argument("--video", required=True, help="视频文件路径")
    parser.add_argument(
        "--out",
        default=None,
        help="帧输出目录；不给则用临时目录（配合 --keep 才有意义）",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=DEFAULT_MAX_FRAMES,
        help=f"最多抽多少帧（默认 {DEFAULT_MAX_FRAMES}）",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="保留帧文件。默认跑完删除临时目录（给了 --out 时总是保留）",
    )
    return parser


def format_meta(frame_set: FrameSet) -> list[str]:
    """元信息与扫描预算。第一眼看的就是这几行。"""
    meta = frame_set.meta
    scan = frame_set.scan
    lines = [
        f"视频      {meta.path}",
        f"尺寸      {meta.width}x{meta.height}",
        f"时长      {meta.duration_sec:.2f} 秒  fps {meta.fps:.3f}",
        (
            f"扫描预算  跳帧={scan.frame_skip}  扫描={scan.scan_sec:.2f} 秒"
            f"  截断={'是' if scan.truncated else '否'}"
        ),
        f"镜头数    {len(frame_set.shots)}",
    ]
    return lines


def format_shots(frame_set: FrameSet) -> list[str]:
    """镜头表。`partial` 单独标出来——它表示这一段的结尾不是真实结尾。"""
    lines = ["", "镜头表（左闭右开）："]
    for shot in frame_set.shots:
        mark = "  ⚠ 截断所得" if shot.partial else ""
        lines.append(
            f"  #{shot.index:03d}  {shot.start_sec:9.2f} – {shot.end_sec:9.2f} 秒"
            f"  时长 {shot.duration_sec:7.2f} 秒{mark}"
        )
    return lines


def format_frames(frame_set: FrameSet) -> list[str]:
    """每帧的时间位置与体积。体积是判断「长边 1024、q85」是否合适的直接依据。"""
    lines = ["", f"抽到的帧（{frame_set.count} 张）："]
    if not frame_set.frames:
        lines.append("  （无）")
        return lines
    total_bytes = 0
    for frame in frame_set.frames:
        total_bytes += frame.size_bytes
        lines.append(
            f"  #{frame.shot_index:03d}  {frame.at_sec:9.2f} 秒"
            f"  {frame.width}x{frame.height}"
            f"  {frame.size_bytes / 1024:7.1f} KB"
            f"  {Path(frame.path).name}"
        )
    lines.append(
        f"  合计 {total_bytes / 1024:.1f} KB，平均 {total_bytes / len(frame_set.frames) / 1024:.1f} KB/帧"
    )
    return lines


def format_skipped(frame_set: FrameSet) -> list[str]:
    """跳过的帧与原因。「12 个镜头里 3 个纯黑」是内容信息，不是故障。"""
    lines = ["", f"跳过的帧（{len(frame_set.skipped)} 张）："]
    if not frame_set.skipped:
        lines.append("  （无）")
        return lines
    for item in frame_set.skipped:
        reason = {
            "blank": "纯色/全黑（画面无信息）",
            "read_failed": "读不出来（越界或解码失败）",
            "write_failed": "编码或写盘失败",
        }.get(item.reason, item.reason)
        lines.append(f"  #{item.shot_index:03d}  {item.at_sec:9.2f} 秒  {reason}")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    video = Path(args.video)
    if not video.is_file():
        print(f"视频不存在：{video}")
        return 2

    keep = args.keep or args.out is not None
    if args.out is not None:
        work_dir = Path(args.out)
        work_dir.mkdir(parents=True, exist_ok=True)
        work_dir_str = str(work_dir)
    else:
        work_dir_str = tempfile.mkdtemp(prefix="kb_frames_")

    print(f"抽帧开始：{video}")
    started = time.perf_counter()
    try:
        frame_set = extract_frames(
            str(video),
            work_dir_str,
            max_frames=args.max_frames,
        )
    except VideoUnreadable as exc:
        print(f"视频打不开：{exc.reason}")
        print(f"  路径 {exc.path}")
        if not keep and args.out is None:
            shutil.rmtree(work_dir_str, ignore_errors=True)
        return 3
    elapsed = time.perf_counter() - started

    for line in format_meta(frame_set):
        print(line)
    for line in format_shots(frame_set):
        print(line)
    for line in format_frames(frame_set):
        print(line)
    for line in format_skipped(frame_set):
        print(line)

    meta = frame_set.meta
    rtf = elapsed / meta.duration_sec if meta.duration_sec > 0 else 0.0
    print()
    print(f"耗时      {elapsed:.2f} 秒  RTF {rtf:.3f}")
    print(f"帧目录    {frame_set.work_dir}")

    if not keep:
        shutil.rmtree(work_dir_str, ignore_errors=True)
        print("帧文件已清理（要保留加 --keep 或指定 --out）")
    else:
        print("帧文件已保留，可直接查看")

    # 零帧不是错误退出码：它是「这条视频画面无信息」的正常结论。
    # 但要让调用方看得出「跑通了但没帧」，所以退出码 0 + 明确的打印。
    if frame_set.count == 0:
        print("⚠ 零帧：这条视频的画面被判为无信息（或全部读不出来）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

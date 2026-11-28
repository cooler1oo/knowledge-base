"""抽帧规划：四个纯函数加一个文件名生成。

本模块只做算术，不 import `cv2`、`scenedetect`、`numpy`：切点对不对、帧位选得
对不对，都能用一组数字直接断言，不必等视频解码跑完。这与 `kb/asr/segment.py`
的分工一致——那一层也是纯算术，模型由外面注入。

## 为什么帧数按「时间分层」裁，而不是取最长的 K 个镜头

方案 §4.11 给视觉的定位是「章节骨架」。骨架是**时间上铺满的框架**，不是
「最重要的几帧」。用「10 分钟视频、前 2 分钟一个标题页、后面 200 个 2.4 秒镜头」
这组真实分布验过（2026-11-28 手算 + 实测）：

| 做法 | 抽到的位置（前 6 个） | 毛病 |
|---|---|---|
| 全局取最长 12 个镜头 | 60、514.8、522、526.8… | 前 2 分钟只 1 帧，后 1 分半挤了 10 帧 |
| 按时间等分 12 层，每层一帧 | 25、75、110、152.4…553.2 | 12 个时间窗各一帧 |

## 为什么帧位取「层与镜头的交集中点」，不取镜头中点

一个镜头可能横跨好几层（上面那个标题页横跨前 3 层）。取镜头中点会让这 3 层
抽到同一张图（都 60.0 秒），于是 12 层只覆盖 11 个时间窗。取交集中点则同一个
镜头在不同层给出不同位置，12 层铺满 12 个窗。

## 为什么空白帧用灰度标准差判，不用平均亮度

实测（长边缩到 1024 后取灰度）：纯黑 `mean=0.0 std=0.0`、纯白 `255.0/0.0`、
18 灰 `18.0/0.0`、50% 灰 `128.0/0.0`——**纯色的标准差恒为 0，与亮度无关**。
而「暗但有边缘」的场景是 `27.5/16.8`，PPT 文字是 `181.6/99.2`。
用平均亮度判会把前者的 `27.5` 判成空白帧，把真实内容整段丢掉。
阈值 2.0 距最近的 16.8 有 8 倍余量。
"""

from __future__ import annotations

from typing import Sequence

from .models import FrameImage, FramePick, ScanPlan, Shot

# 跳帧档位：(视频时长上限, frame_skip)。按耗时选档，因为实测跳帧只损失
# 切点精度、不损失检出率——120 秒 12 镜头视频 frame_skip 0→2 耗时 11.21→5.12 秒，
# 切点数量与位置都不变；另在 8 个 2 秒镜头上验 frame_skip=2 仍全检出，最大偏 0.08 秒。
#
# 上界停在 2 的原因不是检出率而是偏移量：偏移与 frame_skip/fps 同阶，
# 25 fps 下 frame_skip=2 的采样间隔 0.12 秒，实测最大偏移 0.08 秒；再往上
# （比如 5）偏移进入 0.2 秒量级，而省下的只有 1 秒——不值得为 1 秒让切点挪 0.2 秒。
SKIP_TIERS: tuple[tuple[float, int], ...] = (
    (300.0, 0),  # <= 5 分钟：每帧都算，切点零偏移
    (1200.0, 1),  # <= 20 分钟：隔一帧算
    (float("inf"), 2),  # 更长：隔两帧算（25 fps 下每 0.12 秒算一次）
)

# 扫描上限（秒）。30 分钟是最后防线，不是主力手段：截断会不可逆地丢掉后半段
# 全部章节，而 FrameSet 里只能从 `truncated` 标志知道「丢了」。
DEFAULT_MAX_SCAN_SEC = 1800.0

# 空白帧的灰度标准差阈值。依据见模块 docstring 的实测表。
DEFAULT_MIN_STD_LUMA = 2.0

# `at_sec` 对外保留的小数位。3 位即毫秒级，远超镜头边界精度的需要，
# 但能让「同一段视频两次跑出同一个值」成立——浮点尾数不截掉时，
# 两份缓存比对会永远不等。
_AT_SEC_DIGITS = 3


def plan_scan(
    duration_sec: float,
    fps: float,
    max_scan_sec: float = DEFAULT_MAX_SCAN_SEC,
) -> ScanPlan | None:
    """按视频时长决定跳帧步长与扫描上限。

    返回 `None` 表示**元信息不可信**（`duration_sec <= 0` 或 `fps <= 0`）。
    此时调用方直接返回空 `FrameSet`，不去猜一个时长硬扫下去——抖音的 mp4
    常见 fps 声明为 0 或 30 而实际 25，拿这种数算预算只会切出错误的镜头表。
    """
    if max_scan_sec <= 0:
        raise ValueError("max_scan_sec 必须是正数")
    if duration_sec <= 0 or fps <= 0:
        return None

    frame_skip = SKIP_TIERS[-1][1]
    for limit, skip in SKIP_TIERS:
        if duration_sec <= limit:
            frame_skip = skip
            break

    scan_sec = min(duration_sec, max_scan_sec)
    return ScanPlan(
        frame_skip=frame_skip,
        scan_sec=scan_sec,
        truncated=scan_sec < duration_sec,
    )


def merge_shots(
    raw: Sequence[tuple[float, float]],
    duration_sec: float,
    scan_sec: float,
    min_shot_sec: float,
) -> tuple[Shot, ...]:
    """把检测器给的原始区间整理成**时间轴连续覆盖 `[0, scan_sec]`** 的镜头表。

    六步，每一步都对应一种实测见过的坏输入：

    | 步 | 做什么 | 对应的坏输入 |
    |---|---|---|
    | ① | 裁掉 `start < 0` / `end <= start`，并把 `end` 钳到 `scan_sec` | 解码器给的越界时间码 |
    | ② | 按 `start_sec` 排序 | 库返回顺序未定义 |
    | ③ | 同一 `start_sec` 的合并，保留 `end` 更大的 | 相邻两窗口在同一帧都报切点 |
    | ④ | 短于 `min_shot_sec` 的并进**前**一段；若是第一段则并进**后**一段 | 闪烁视频切出 250 个 0.02 秒镜头（实测） |
    | ⑤ | `raw` 为空 → 返回单个 `Shot(0, 0.0, scan_sec)` | 纯色/静态视频返回 0 个切点（实测 2 秒纯色、60 秒静态画面） |
    | ⑥ | `scan_sec < duration_sec` → 尾部补 `Shot(partial=True, scan_sec, duration_sec)` | 30 分钟以上视频被截断 |

    **第 ④ 步为什么是「并进前一段」而不是「删掉」**：删掉会在时间轴上留空隙，
    后面的分层抽样就会在某层里找不到与之重叠的镜头，退化成「该层无帧」。
    并进前一段则时间轴始终连续，这是分层抽样的前提。

    **`scan_sec <= 0` 返回空表**：零长视频没有可抽的帧，造一个零长镜头只会让
    后面的分层抽样除零。这一条不在上面六步里，是给 `duration_sec=0` 的护栏。
    """
    if min_shot_sec < 0:
        raise ValueError("min_shot_sec 不能为负")
    if scan_sec <= 0:
        return ()

    # ① 裁掉非法区间。`end > scan_sec` 的钳到 scan_sec——不然第 ⑥ 步补的尾巴
    # 会与已有区间重叠，时间轴就不连续了。
    spans: list[tuple[float, float]] = []
    for start, end in raw:
        if start < 0 or end <= start:
            continue
        clipped_end = min(end, scan_sec)
        clipped_start = min(start, scan_sec)
        if clipped_end <= clipped_start:
            continue
        spans.append((clipped_start, clipped_end))

    # ② 排序。检测器的返回顺序在文档里没有承诺。
    spans.sort(key=lambda span: (span[0], span[1]))

    # ③ 同一 start 只留 end 最大的。相邻两个检测窗口在同一帧都报切点时会出现。
    deduped: list[list[float]] = []
    for start, end in spans:
        if deduped and abs(deduped[-1][0] - start) < 1e-9:
            deduped[-1][1] = max(deduped[-1][1], end)
            continue
        deduped.append([start, end])

    # ④ 短镜头合并。第一段太短时不能往前并（前面没东西），往后并——
    # 起点保留第一段的起点，这样时间轴不从一段之后开始。
    merged: list[list[float]] = []
    for start, end in deduped:
        if not merged:
            merged.append([start, end])
            continue
        if end - start < min_shot_sec:
            merged[-1][1] = max(merged[-1][1], end)
            continue
        if len(merged) == 1 and merged[0][1] - merged[0][0] < min_shot_sec:
            merged[0] = [merged[0][0], end]
            continue
        merged.append([start, end])

    # ⑤ 零切点是正常路径，不是异常：口播视频全程一个机位就是这种情形。
    # 返回空会让下游以为「这条视频没画面」，所以补一个覆盖全片的镜头。
    if not merged:
        merged = [[0.0, scan_sec]]

    # ⑤b 补足覆盖边界。检测器不保证首帧恰好是 0.0、末帧恰好等于 scan_sec：
    # 帧对齐会让末段结尾少半帧（实测 3 秒 25fps 视频末段结束在 3.0，但带
    # `duration` 截断时会落在最后一个已解码帧上）。差这一点的后果不是「少一帧
    # 画面」，而是**最后一层找不到与之重叠的镜头**，直接无帧。
    # 覆盖连续是分层抽样的前提，所以这里显式拉齐，不做「大概齐」。
    merged[0][0] = 0.0
    merged[-1][1] = max(merged[-1][1], scan_sec)

    shots: list[Shot] = [
        Shot(index=i, start_sec=start, end_sec=end, partial=False)
        for i, (start, end) in enumerate(merged)
    ]

    # ⑥ 被截断时补尾部。`partial=True` 让下游知道「这一帧不代表整个后半段」——
    # 那段真实内容没检测过，它可能包含 50 个镜头。
    if scan_sec < duration_sec:
        shots.append(
            Shot(
                index=len(shots),
                start_sec=scan_sec,
                end_sec=duration_sec,
                partial=True,
            )
        )

    return tuple(shots)


def plan_picks(
    shots: Sequence[Shot],
    max_frames: int = 12,
) -> tuple[FramePick, ...]:
    """按时间分层，每层取与该层重叠最长的镜头，帧位取**交集中点**。

    `K = min(max_frames, len(shots))`：`max_frames=12` 而视频只有 3 个镜头时，
    硬分 12 层会让 9 层指向重复位置。取镜头数为上限，3 个镜头就抽 3 帧
    （实测三镜头 15 秒视频 → 2.5 / 7.5 / 12.5 秒）。

    三个已知形状（都已手算验证）：

    | 输入 | 输出 |
    |---|---|
    | 单镜头 480 秒 | 1 帧 @ 240 秒——口播视频一帧代表全片，不制造假的多帧 |
    | 两镜头 600 秒 | 2 帧 @ 150 / 450 秒 |
    | 12 镜头 60 秒 | 12 帧 @ 2.5 / 7.5 / … / 57.5 秒 |

    `max_frames <= 0` 返回空表：调用方明确说了不要帧，不该被强塞一张。
    """
    if max_frames <= 0 or not shots:
        return ()

    total = shots[-1].end_sec
    if total <= 0:
        return ()

    layers = min(max_frames, len(shots))
    picks: list[FramePick] = []
    for i in range(layers):
        lo = total * i / layers
        hi = total * (i + 1) / layers
        best_shot: Shot | None = None
        best_lo = 0.0
        best_hi = 0.0
        best_overlap = 0.0
        for shot in shots:
            overlap_lo = max(shot.start_sec, lo)
            overlap_hi = min(shot.end_sec, hi)
            overlap = overlap_hi - overlap_lo
            if overlap <= 0 or overlap <= best_overlap:
                continue
            best_shot, best_lo, best_hi, best_overlap = (
                shot,
                overlap_lo,
                overlap_hi,
                overlap,
            )

        if best_shot is None:
            # 兜底：按 merge_shots 的契约这条不会发生（它保证时间轴连续覆盖
            # [0, scan_sec]）。保留它是为了「根因在上游时不在这里抛 IndexError」——
            # 在帧规划这一层抛异常，排查时看不出问题出在镜头表。
            best_shot = _nearest_shot(shots, (lo + hi) / 2)
            best_lo, best_hi = lo, hi

        picks.append(
            FramePick(
                shot_index=best_shot.index,
                at_sec=round((best_lo + best_hi) / 2, _AT_SEC_DIGITS),
                start_sec=best_shot.start_sec,
                end_sec=best_shot.end_sec,
            )
        )

    # ⑤ 契约要求按时间升序返回。分层本身从早到晚遍历，但同层的兜底分支
    # 可能把位置拉到邻层，显式排一次比依赖推导可靠。
    picks.sort(key=lambda pick: pick.at_sec)
    return tuple(picks)


def _nearest_shot(shots: Sequence[Shot], at_sec: float) -> Shot:
    """取时间上离 `at_sec` 最近的镜头。只被 `plan_picks` 的兜底分支用到。"""

    def distance(shot: Shot) -> float:
        if shot.start_sec <= at_sec <= shot.end_sec:
            return 0.0
        return min(abs(shot.start_sec - at_sec), abs(shot.end_sec - at_sec))

    return min(shots, key=distance)


def is_blank(image: FrameImage, min_std: float = DEFAULT_MIN_STD_LUMA) -> bool:
    """这一帧是不是纯色/全黑。

    只看灰度标准差。`mean_luma` 记录进 `FrameImage` 但**不参与判定**：
    暗场景的标准差可以是 16.8，用亮度判会把它当空白帧丢掉。
    """
    return image.std_luma < min_std


def frame_filename(shot_index: int, at_sec: float) -> str:
    """帧文件名：`shot007_000123.45s.jpg`。

    三处格式都是为了让文件**能排序、能读**：`{index:03d}` 让字典序等于镜头序号序；
    `{at_sec:09.2f}` 固定 9 字符宽度，字典序等于时间序；时间进文件名是为了
    不打开图就知道这一帧来自第几秒。

    只用 ASCII，不用中文：`cv2.imwrite` 在中文路径下静默失败（实测返回 False
    且不写文件），本项目目录本身就带中文，文件名再带中文会让这个坑放大。
    """
    return f"shot{shot_index:03d}_{at_sec:09.2f}s.jpg"

"""抽帧规划的纯逻辑单测：不装 cv2、不装 scenedetect、不解码任何视频，毫秒级跑完。

这些用例钉住的是实测暴露过的坑，不是凭空设计的接口：

1. **`get_scene_list()` 不带 `start_in_scene` 对无切点视频返回 `[]`** ——
   于是「口播视频一个镜头」这条正常路径静默变成零镜头。用
   `test_零切点返回单个覆盖全片的镜头` 与 `test_覆盖必须从0到scan_sec` 钉死。
2. **闪烁视频切出 250 个 0.02 秒镜头** —— 用 `test_短镜头并进前一段` 钉死：
   并进前一段而不是删掉，因为删掉会让时间轴留空隙、分层抽样在空隙里抽不到帧。
3. **全局取最长 K 个镜头会把帧挤在少数长窗口** —— 用
   `test_前两分钟一个标题页_后面两百个短镜头` 钉死分层与全局 top-K 的差别。
4. **帧位取镜头中点会让横跨多层的镜头抽到同一张图** —— 用
   `test_横跨多层的镜头在不同层给出不同位置` 钉死「交集中点」。

真视频的行为由 `scripts/probe_frames_vision.py` 验证，不放进 pytest：
它要真视频文件，而容器里没有 ffmpeg 现成可用的样本。
"""

from __future__ import annotations

import pytest

from kb.vision.models import FrameImage, Shot
from kb.vision.shot_plan import (
    DEFAULT_MAX_SCAN_SEC,
    SKIP_TIERS,
    frame_filename,
    is_blank,
    merge_shots,
    plan_picks,
    plan_scan,
)


def image(std_luma: float, mean_luma: float = 128.0) -> FrameImage:
    """构造一帧。`data=None` 是关键：本测试层不许碰 numpy。"""
    return FrameImage(
        data=None,
        width=1024,
        height=576,
        mean_luma=mean_luma,
        std_luma=std_luma,
    )


def shot(index: int, start: float, end: float, partial: bool = False) -> Shot:
    return Shot(index=index, start_sec=start, end_sec=end, partial=partial)


# ── plan_scan：元信息校验 ──────────────────────────────────


@pytest.mark.parametrize("duration_sec", [0.0, -1.0, -0.001])
def test_时长为零或负返回None(duration_sec: float) -> None:
    """元信息不可信时返回 None，让调用方返回空 FrameSet。

    硬算下去的后果是切出错误的镜头表——抖音 mp4 常见 duration 声明为 0。
    """
    assert plan_scan(duration_sec=duration_sec, fps=25.0) is None


@pytest.mark.parametrize("fps", [0.0, -25.0])
def test_fps为零或负返回None(fps: float) -> None:
    assert plan_scan(duration_sec=60.0, fps=fps) is None


def test_时长非正时不去看fps也能返回None() -> None:
    """两个校验各自独立：任一不可信就该返回 None。"""
    assert plan_scan(duration_sec=0.0, fps=0.0) is None


# ── plan_scan：跳帧档位 ────────────────────────────────────


@pytest.mark.parametrize(
    ("duration_sec", "expected_skip"),
    [
        (1.0, 0),
        (300.0, 0),  # 档位边界闭区间
        (300.1, 1),
        (1200.0, 1),
        (1200.1, 2),
        (7200.0, 2),
    ],
)
def test_跳帧按三档选(duration_sec: float, expected_skip: int) -> None:
    plan = plan_scan(duration_sec=duration_sec, fps=25.0)

    assert plan is not None
    assert plan.frame_skip == expected_skip


def test_档位表上界是inf以便兜住任意长视频() -> None:
    """最后一档必须是 inf：否则超长视频会落到 for 循环外，frame_skip 取到
    SKIP_TIERS[-1] 之外的值，行为取决于实现细节。"""
    assert SKIP_TIERS[-1][0] == float("inf")


def test_短于上限时不截断() -> None:
    plan = plan_scan(duration_sec=100.0, fps=25.0)

    assert plan is not None
    assert plan.scan_sec == 100.0
    assert plan.truncated is False


def test_超过上限时截断并标记() -> None:
    """30 分钟以上的视频才截断。`truncated` 是下游唯一能知道「丢了后半段」的线索。"""
    plan = plan_scan(duration_sec=3600.0, fps=25.0)

    assert plan is not None
    assert plan.scan_sec == DEFAULT_MAX_SCAN_SEC
    assert plan.truncated is True


def test_恰好等于上限不算截断() -> None:
    plan = plan_scan(duration_sec=DEFAULT_MAX_SCAN_SEC, fps=25.0)

    assert plan is not None
    assert plan.truncated is False


def test_自定义上限生效() -> None:
    plan = plan_scan(duration_sec=100.0, fps=25.0, max_scan_sec=60.0)

    assert plan is not None
    assert plan.scan_sec == 60.0
    assert plan.truncated is True


def test_上限非正数要报错() -> None:
    """上限写成 0 或负数等于「一帧都不扫」，那是配置错误不是意图。"""
    with pytest.raises(ValueError):
        plan_scan(duration_sec=100.0, fps=25.0, max_scan_sec=0.0)
    with pytest.raises(ValueError):
        plan_scan(duration_sec=100.0, fps=25.0, max_scan_sec=-1.0)


# ── merge_shots：六步各自的输入 ─────────────────────────────


def test_零切点返回单个覆盖全片的镜头() -> None:
    """纯色/静态视频返回 0 个切点（实测 2 秒纯色、60 秒静态画面）。

    这是口播视频的正常路径。返回空会让下游以为「这条视频没画面」。
    """
    shots = merge_shots(raw=[], duration_sec=60.0, scan_sec=60.0, min_shot_sec=0.5)

    assert len(shots) == 1
    assert shots[0].start_sec == 0.0
    assert shots[0].end_sec == 60.0
    assert shots[0].index == 0
    assert shots[0].partial is False


def test_越界与倒置的区间被裁掉() -> None:
    """① 解码器给的越界时间码不能进镜头表。"""
    shots = merge_shots(
        raw=[(-1.0, 2.0), (3.0, 3.0), (5.0, 4.0), (6.0, 8.0)],
        duration_sec=8.0,
        scan_sec=8.0,
        min_shot_sec=0.5,
    )

    for s in shots:
        assert s.start_sec >= 0.0, "负起点必须被裁掉，否则时间轴从负数开始"
        assert s.end_sec > s.start_sec, "倒置区间必须被裁掉"
    # 三个非法区间全被裁掉，只剩 (6, 8)，再被 ⑤b 拉齐成覆盖全片
    assert [(s.start_sec, s.end_sec) for s in shots] == [(0.0, 8.0)]


def test_超出扫描边界的区间被钳到scan_sec() -> None:
    """① 的钳位。不钳的话第 ⑥ 步补的尾巴会与已有区间重叠，时间轴不连续。"""
    shots = merge_shots(
        raw=[(0.0, 5.0), (5.0, 22.0)],
        duration_sec=30.0,
        scan_sec=10.0,
        min_shot_sec=0.5,
    )

    assert shots[-1].end_sec == 30.0, "尾部补到 duration_sec"
    for s in shots:
        assert s.start_sec < s.end_sec


def test_乱序输入按start排序() -> None:
    """② 检测器的返回顺序在文档里没有承诺。"""
    shots = merge_shots(
        raw=[(6.0, 9.0), (0.0, 3.0), (3.0, 6.0)],
        duration_sec=9.0,
        scan_sec=9.0,
        min_shot_sec=0.5,
    )

    assert [s.start_sec for s in shots] == [0.0, 3.0, 6.0]
    assert [s.index for s in shots] == [0, 1, 2], "index 按时间顺序重编"


def test_同一start的区间只留end更大的() -> None:
    """③ 相邻两个检测窗口在同一帧都报切点时会给出两个起点相同的区间。"""
    shots = merge_shots(
        raw=[(0.0, 2.0), (2.0, 4.0), (2.0, 6.0), (6.0, 9.0)],
        duration_sec=9.0,
        scan_sec=9.0,
        min_shot_sec=0.5,
    )

    assert len(shots) == 3
    spans = [(s.start_sec, s.end_sec) for s in shots]
    assert spans == [(0.0, 2.0), (2.0, 6.0), (6.0, 9.0)]


def test_短镜头并进前一段而不是删掉() -> None:
    """④ 闪烁视频切出 250 个 0.02 秒镜头（实测）。

    并进前一段而不是删掉：删掉会在 time 轴留空隙，分层抽样在空隙里抽不到帧。
    """
    shots = merge_shots(
        raw=[(0.0, 3.0), (3.0, 3.02), (3.02, 3.04), (3.04, 6.0)],
        duration_sec=6.0,
        scan_sec=6.0,
        min_shot_sec=0.5,
    )

    assert len(shots) == 2, "两个 0.02 秒的碎片并进前一段"
    assert (shots[0].start_sec, shots[0].end_sec) == (0.0, 3.04)


def test_第一段太短时并进后一段() -> None:
    """④ 的前边界：第一段前面没有段落可并，只能往后并，且起点保留。

    起点保留 0.0 很重要：时间轴必须从 0 开始，否则第 0 秒的画面无人代表。
    """
    shots = merge_shots(
        raw=[(0.0, 0.02), (0.02, 4.0), (4.0, 8.0)],
        duration_sec=8.0,
        scan_sec=8.0,
        min_shot_sec=0.5,
    )

    assert len(shots) == 2
    assert (shots[0].start_sec, shots[0].end_sec) == (0.0, 4.0)


def test_连续多个短镜头链式并入同一段() -> None:
    """闪烁视频实际形态：一长串碎片应全部并进最近的长段。"""
    raw = [(0.0, 2.0)]
    raw += [(2.0 + i * 0.02, 2.0 + (i + 1) * 0.02) for i in range(5)]
    raw += [(2.1, 6.0)]

    shots = merge_shots(raw=raw, duration_sec=6.0, scan_sec=6.0, min_shot_sec=0.5)

    assert len(shots) == 2
    assert (shots[0].start_sec, shots[0].end_sec) == (0.0, 2.1)


def test_截断时尾部补一段并标partial() -> None:
    """⑥ 那段真实内容没被检测过，可能包含几十个镜头，必须让下游知道。"""
    shots = merge_shots(
        raw=[(0.0, 10.0)],
        duration_sec=60.0,
        scan_sec=10.0,
        min_shot_sec=0.5,
    )

    assert len(shots) == 2
    assert shots[-1].partial is True
    assert (shots[-1].start_sec, shots[-1].end_sec) == (10.0, 60.0)
    assert shots[-1].index == 1


def test_未截断时不补尾巴() -> None:
    shots = merge_shots(
        raw=[(0.0, 10.0)],
        duration_sec=10.0,
        scan_sec=10.0,
        min_shot_sec=0.5,
    )

    assert len(shots) == 1
    assert shots[0].partial is False


def test_覆盖必须从0到scan_sec() -> None:
    """时间轴连续覆盖是分层抽样的前提。这条用一个刁钻输入钉住首尾。

    检测器不保证首帧恰好 0.0、末帧恰好 scan_sec——差一点的后果不是「少一帧
    画面」，而是最后一层找不到重叠镜头、直接无帧。
    """
    shots = merge_shots(
        raw=[(0.04, 3.0), (3.0, 7.96)],
        duration_sec=8.0,
        scan_sec=8.0,
        min_shot_sec=0.5,
    )

    assert shots[0].start_sec == 0.0
    assert shots[-1].end_sec == 8.0
    for prev, nxt in zip(shots, shots[1:]):
        assert prev.end_sec == nxt.start_sec, "段与段之间必须首尾相接"


def test_相邻镜头首尾相接不留缝() -> None:
    shots = merge_shots(
        raw=[(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)],
        duration_sec=3.0,
        scan_sec=3.0,
        min_shot_sec=0.5,
    )

    assert [(s.start_sec, s.end_sec) for s in shots] == [
        (0.0, 1.0),
        (1.0, 2.0),
        (2.0, 3.0),
    ]


def test_scan_sec为零返回空表() -> None:
    """零长视频没有可抽的帧。造一个零长镜头只会让分层抽样除零。"""
    assert merge_shots([], duration_sec=0.0, scan_sec=0.0, min_shot_sec=0.5) == ()


def test_负的min_shot_sec要报错() -> None:
    with pytest.raises(ValueError):
        merge_shots([], duration_sec=10.0, scan_sec=10.0, min_shot_sec=-0.1)


def test_单镜头视频得到一个镜头() -> None:
    """口播视频的形态：整条一个机位。"""
    shots = merge_shots(
        raw=[(0.0, 480.0)],
        duration_sec=480.0,
        scan_sec=480.0,
        min_shot_sec=0.5,
    )

    assert len(shots) == 1
    assert shots[0].duration_sec == 480.0


# ── plan_picks：分层抽样 ───────────────────────────────────


def test_单镜头480秒抽一帧在中点() -> None:
    """口播视频：一帧代表全片，不制造假的多帧。"""
    picks = plan_picks([shot(0, 0.0, 480.0)], max_frames=12)

    assert len(picks) == 1
    assert picks[0].at_sec == 240.0
    assert picks[0].shot_index == 0


def test_两镜头600秒抽两帧在各层中点() -> None:
    """镜头数少于 max_frames 时按镜头数出帧，不硬凑 12 层。"""
    shots = [shot(0, 0.0, 300.0), shot(1, 300.0, 600.0)]

    picks = plan_picks(shots, max_frames=12)

    assert len(picks) == 2
    assert [p.at_sec for p in picks] == [150.0, 450.0]


def test_12镜头60秒每镜头一帧取中点() -> None:
    """12 个 5 秒镜头、要 12 帧：每层正好落在一个镜头里，取各自中点。"""
    shots = [shot(i, i * 5.0, (i + 1) * 5.0) for i in range(12)]

    picks = plan_picks(shots, max_frames=12)

    assert len(picks) == 12
    assert [p.at_sec for p in picks] == [2.5 + i * 5.0 for i in range(12)]


def test_三镜头15秒视频得到2点5_7点5_12点5() -> None:
    """设计文档 §4.3.3 里写下的实测形状，原样钉住。"""
    shots = [shot(0, 0.0, 5.0), shot(1, 5.0, 10.0), shot(2, 10.0, 15.0)]

    picks = plan_picks(shots, max_frames=12)

    assert [p.at_sec for p in picks] == [2.5, 7.5, 12.5]


def test_层内取与该层重叠最长的镜头() -> None:
    """分两层，第二层里 4–10 秒的镜头占 4 秒、10–20 的占 6 秒，该选后者。

    层边界：total=20，两层是 [0,10) 与 [10,20]。
    """
    shots = [shot(0, 0.0, 4.0), shot(1, 4.0, 10.0), shot(2, 10.0, 20.0)]

    picks = plan_picks(shots, max_frames=2)

    assert picks[0].shot_index == 1, "第一层里 4–10 重叠 6 秒最多"
    assert picks[1].shot_index == 2


def test_横跨多层的镜头在不同层给出不同位置() -> None:
    """取镜头中点会让 3 层抽到同一张图（都 60.0 秒），12 层只覆盖 11 个窗。

    用「前 2 分钟一个标题页、后面一堆短镜头」这组真实分布验。
    """
    shots = [shot(0, 0.0, 120.0)]
    for i in range(200):
        shots.append(shot(i + 1, 120.0 + i * 2.4, 120.0 + (i + 1) * 2.4))

    picks = plan_picks(shots, max_frames=12)

    at_secs = [p.at_sec for p in picks]
    assert len(set(at_secs)) == 12, f"12 层必须给出 12 个不同位置，实际 {at_secs}"
    assert at_secs == sorted(at_secs), "按时间升序"
    # 总长 600 秒、12 层 → 层宽 50。前 2 分钟横跨前 3 层，
    # 交集中点分别是 [0,50)、[50,100)、[100,120) 的中点 → 25 / 75 / 110。
    # 设计文档 §4.3.3 记的就是这组数字。
    assert at_secs[:3] == [25.0, 75.0, 110.0]


def test_帧不挤在少数长镜头里() -> None:
    """全局取最长 K 个镜头的毛病：前 2 分钟只 1 帧。分层则每窗一帧。"""
    shots = [shot(0, 0.0, 120.0)]
    for i in range(200):
        shots.append(shot(i + 1, 120.0 + i * 2.4, 120.0 + (i + 1) * 2.4))
    total = shots[-1].end_sec

    picks = plan_picks(shots, max_frames=12)

    # 每层一个帧位，层宽 total/12；首层的帧位必须落在首层内
    layer_width = total / 12
    for i, pick in enumerate(picks):
        assert i * layer_width <= pick.at_sec <= (i + 1) * layer_width


def test_max_frames为零返回空表() -> None:
    """调用方明确说了不要帧，不该被强塞一张。"""
    assert plan_picks([shot(0, 0.0, 10.0)], max_frames=0) == ()
    assert plan_picks([shot(0, 0.0, 10.0)], max_frames=-3) == ()


def test_镜头表为空返回空表() -> None:
    assert plan_picks([], max_frames=12) == ()


def test_零长镜头表返回空表() -> None:
    """total=0 时分层要除零，直接返回空。"""
    assert plan_picks([shot(0, 0.0, 0.0)], max_frames=12) == ()


def test_帧位按时间升序返回() -> None:
    shots = [shot(i, i * 2.0, (i + 1) * 2.0) for i in range(20)]

    picks = plan_picks(shots, max_frames=7)

    at_secs = [p.at_sec for p in picks]
    assert at_secs == sorted(at_secs)


def test_帧位带所属镜头的区间() -> None:
    """区间带给下游排查：摘要里的词要能定位到「第几秒、属于哪个镜头」。"""
    shots = [shot(0, 0.0, 10.0), shot(1, 10.0, 20.0)]

    picks = plan_picks(shots, max_frames=2)

    assert picks[0].start_sec == 0.0
    assert picks[0].end_sec == 10.0
    assert picks[1].start_sec == 10.0
    assert picks[1].end_sec == 20.0


def test_尾部补的partial镜头也能被抽到() -> None:
    """截断视频的尾巴是一个完整镜头，它该占一层。"""
    shots = merge_shots(
        raw=[(0.0, 10.0)],
        duration_sec=60.0,
        scan_sec=10.0,
        min_shot_sec=0.5,
    )

    picks = plan_picks(shots, max_frames=2)

    assert len(picks) == 2
    assert picks[-1].shot_index == 1
    assert 10.0 <= picks[-1].at_sec <= 60.0


# ── is_blank：只看标准差 ───────────────────────────────────


@pytest.mark.parametrize(
    ("std_luma", "expected"),
    [
        (0.0, True),  # 纯黑 / 纯白 / 任何纯色
        (1.9, True),
        (2.0, False),  # 阈值是严格小于，2.0 不算空白
        (16.8, False),  # 暗场景但有边缘（实测）
        (99.2, False),  # PPT 文字（实测）
    ],
)
def test_空白判定只看标准差(std_luma: float, expected: bool) -> None:
    assert is_blank(image(std_luma=std_luma)) is expected


def test_暗但有内容的场景不能判成空白() -> None:
    """实测「暗场景（有边缘）」是 mean=27.5 / std=16.8。

    用平均亮度判会把它当空白帧丢掉——这把真实内容整段删了。
    """
    assert is_blank(image(std_luma=16.8, mean_luma=27.5)) is False


def test_纯白帧也算空白() -> None:
    """纯色的标准差恒为 0，与亮度无关：纯白 mean=255 也是空白。"""
    assert is_blank(image(std_luma=0.0, mean_luma=255.0)) is True


def test_阈值可调() -> None:
    assert is_blank(image(std_luma=5.0), min_std=10.0) is True
    assert is_blank(image(std_luma=5.0), min_std=1.0) is False


# ── frame_filename：能排序能读 ─────────────────────────────


def test_文件名格式() -> None:
    assert frame_filename(7, 123.45) == "shot007_000123.45s.jpg"


def test_文件名只含ASCII() -> None:
    """本项目目录本身带中文，文件名再带中文会放大 cv2 中文路径的坑。"""
    name = frame_filename(3, 42.5)

    assert name.isascii()
    assert name.endswith(".jpg")


def test_文件名字典序等于镜头序() -> None:
    names = [frame_filename(i, float(i * 10)) for i in range(120)]

    assert names == sorted(names), "index 补零到 3 位，字典序等于数值序"


def test_文件名字典序等于时间序() -> None:
    at_secs = [0.0, 1.5, 10.0, 99.99, 123.45, 9999.0]
    names = [frame_filename(0, sec) for sec in at_secs]

    assert names == sorted(names), "秒数补到 9 字符宽，字典序等于时间序"


def test_零秒也合法() -> None:
    assert frame_filename(0, 0.0) == "shot000_000000.00s.jpg"

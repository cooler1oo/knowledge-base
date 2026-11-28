"""抽帧装配的单测：两个假实现注入，不解码任何视频，毫秒级跑完。

假实现走**构造参数注入**，不用 `monkeypatch`（沿用 `tests/test_worker_loop.py`
的口径）。patch 会让「这个函数用了什么」变成运行时才知道；构造参数写在签名里，
读代码就能看见。

这些用例钉住的都是「静默出错」的类型——不报错但结果错：

1. **切镜头抛异常时不该零帧**（`test_切镜头抛异常走单镜头兜底`）：一条视频的镜头
   检测失败，不该让整条视频没有代表性画面。
2. **单帧读失败不该影响其余帧**（`test_单帧读失败不影响其余帧`）：理由同 worker
   收异常，一块坏数据不该让整条任务白跑。
3. **全空白帧是零帧而不是异常**（`test_全空白返回零帧而不抛异常`）：纯黑视频的
   正确结论是「画面无信息」，抛异常会让调用方去重试一条永远没有画面的视频。
4. **视频打不开必须上抛**（`test_视频打不开要抛VideoUnreadable`）：这是三层口径
   里唯一抛异常的一层，吞掉它会让「视频坏了」与「视频没内容」长得一样。

真实现（PySceneDetect + OpenCV）的行为由 `scripts/probe_frames_vision.py` 验证，
不放进 pytest：它要真视频文件。
"""

from __future__ import annotations

import os

import pytest

from kb.vision.frames import (
    REASON_BLANK,
    REASON_READ_FAILED,
    REASON_WRITE_FAILED,
    extract_frames,
)
from kb.vision.grab import FrameReadFailed
from kb.vision.models import FrameImage, ScanPlan, Shot, VideoMeta, VideoUnreadable

VIDEO = "/fake/视频.mp4"


class FakeCutter:
    """按剧本返回预设镜头区间。

    `raise_exc` 用来模拟「PySceneDetect 在解码到一半时崩了」——这是真机上出现过
    的情形（缺编解码时 open_video 成功、detect_scenes 中途抛）。
    """

    def __init__(
        self,
        shots: tuple[tuple[float, float], ...] = (),
        raise_exc: Exception | None = None,
    ) -> None:
        self.shots = shots
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, ScanPlan, float]] = []

    def detect(
        self, video_path: str, plan: ScanPlan, min_shot_sec: float
    ) -> tuple[Shot, ...]:
        self.calls.append((video_path, plan, min_shot_sec))
        if self.raise_exc is not None:
            raise self.raise_exc
        return tuple(
            Shot(index=i, start_sec=start, end_sec=end)
            for i, (start, end) in enumerate(self.shots)
        )


class FakeGrabber:
    """按 `at_sec` 返回预设的 `FrameImage`。

    三组可配置的坏行为，各自对应一类真实故障：

    - `fail_at_sec`：这些位置读失败（越界或解码失败）
    - `blank_at_sec`：这些位置返回纯色帧（`std_luma=0`）
    - `default_std`：其余位置的灰度标准差，默认 50（有内容）
    """

    def __init__(
        self,
        duration_sec: float = 60.0,
        fps: float = 25.0,
        fail_at_sec: tuple[float, ...] = (),
        blank_at_sec: tuple[float, ...] = (),
        default_std: float = 50.0,
        probe_exc: Exception | None = None,
        open_exc: Exception | None = None,
        encode_exc: Exception | None = None,
    ) -> None:
        self.duration_sec = duration_sec
        self.fps = fps
        self.fail_at_sec = fail_at_sec
        self.blank_at_sec = blank_at_sec
        self.default_std = default_std
        self.probe_exc = probe_exc
        self.open_exc = open_exc
        self.encode_exc = encode_exc
        self.opened = 0
        self.closed = 0
        self.reads: list[float] = []

    def probe(self, video_path: str) -> VideoMeta:
        if self.probe_exc is not None:
            raise self.probe_exc
        return VideoMeta(
            path=video_path,
            width=1080,
            height=1920,
            duration_sec=self.duration_sec,
            fps=self.fps,
        )

    def open(self, video_path: str) -> None:
        if self.open_exc is not None:
            raise self.open_exc
        self.opened += 1

    def read_at(self, at_sec: float) -> FrameImage:
        self.reads.append(at_sec)
        if any(abs(at_sec - bad) < 1e-6 for bad in self.fail_at_sec):
            raise FrameReadFailed(at_sec, "fake 读失败")
        std = (
            0.0
            if any(abs(at_sec - b) < 1e-6 for b in self.blank_at_sec)
            else self.default_std
        )
        return FrameImage(
            data=None,
            width=1080,
            height=1920,
            mean_luma=128.0,
            std_luma=std,
        )

    def encode(self, image: FrameImage, long_side: int, quality: int) -> bytes:
        if self.encode_exc is not None:
            raise self.encode_exc
        return b"\xff\xd8\xff\xe0" + b"x" * 100  # 一个形状像 JPEG 的假载荷

    def close(self) -> None:
        self.closed += 1


def shots_12x5(total: int = 12) -> tuple[tuple[float, float], ...]:
    """12 个 5 秒镜头，共 60 秒。"""
    return tuple((i * 5.0, (i + 1) * 5.0) for i in range(total))


# ── 正常路径 ───────────────────────────────────────────────


def test_12镜头60秒抽出12帧(tmp_path) -> None:
    """每镜头一帧，各取中点。这是 PPT 讲解类视频的典型形态。"""
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0)

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert frame_set.count == 12
    assert [f.at_sec for f in frame_set.frames] == [2.5 + i * 5.0 for i in range(12)]
    assert [f.shot_index for f in frame_set.frames] == list(range(12))


def test_每帧都落了盘且大小非零(tmp_path) -> None:
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0)

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    for frame in frame_set.frames:
        assert os.path.isfile(frame.path), f"帧文件不存在：{frame.path}"
        assert frame.size_bytes > 0
        assert os.path.getsize(frame.path) == frame.size_bytes


def test_帧文件名可排序(tmp_path) -> None:
    """字典序等于镜头序 + 时间序，否则人眼看目录会乱。"""
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0)

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    names = [os.path.basename(f.path) for f in frame_set.frames]
    assert names == sorted(names)
    assert all(n.isascii() for n in names), "文件名只用 ASCII，避开 cv2 中文路径的坑"


def test_帧带所属镜头的区间(tmp_path) -> None:
    """区间带给下游排查：摘要里的词要能定位到第几秒、属于哪个镜头。"""
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0)

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    first = frame_set.frames[0]
    assert (first.start_sec, first.end_sec) == (0.0, 5.0)


def test_句柄开了也关了(tmp_path) -> None:
    """不关会句柄耗尽：表现是「跑几小时之后所有视频都打不开」。"""
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0)

    extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert grabber.opened == 1
    assert grabber.closed == 1


def test_句柄在异常时也关(tmp_path) -> None:
    """`encode` 每次都崩，仍必须 close——try/finally 的意义就在这里。"""
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(
        duration_sec=60.0, encode_exc=FrameReadFailed(0.0, "编码崩了")
    )

    extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert grabber.closed == 1


def test_切镜头收到的是扫描方案与最短镜头时长(tmp_path) -> None:
    """`ScanPlan` 与 `min_shot_sec` 必须原样传到 cutter，不能被默认值吃掉。"""
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0)

    extract_frames(
        VIDEO,
        str(tmp_path),
        cutter=cutter,
        grabber=grabber,
        min_shot_sec=1.25,
    )

    assert len(cutter.calls) == 1
    path, plan, min_shot_sec = cutter.calls[0]
    assert path == VIDEO
    assert plan.scan_sec == 60.0
    assert plan.frame_skip == 0, "60 秒视频落在第一档，每帧都算"
    assert min_shot_sec == 1.25


# ── 全空白 ─────────────────────────────────────────────────


def test_全空白返回零帧而不抛异常(tmp_path) -> None:
    """纯黑视频的正确结论是「画面无信息」，不是故障。

    抛异常会让调用方去重试一条永远不会有画面的视频。
    """
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0, default_std=0.0)

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert frame_set.count == 0
    assert frame_set.frames == ()
    assert frame_set.shots, "镜头表要留着：能从它看出切分确实工作过"


def test_全空白时每帧都记了跳过原因(tmp_path) -> None:
    """「12 个镜头里 12 个纯黑」是内容信息，必须能从 FrameSet 看出来。"""
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0, default_std=0.0)

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert len(frame_set.skipped) >= 12
    assert all(s.reason == REASON_BLANK for s in frame_set.skipped)


def test_部分空白只丢掉空白的那些(tmp_path) -> None:
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0, blank_at_sec=(2.5, 17.5, 32.5))

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert frame_set.count == 9
    assert len(frame_set.skipped) == 3
    assert {s.at_sec for s in frame_set.skipped} == {2.5, 17.5, 32.5}


def test_空白判定用标准差不用亮度(tmp_path) -> None:
    """暗但有内容的场景（std=16.8）不能被当空白丢掉。"""
    cutter = FakeCutter(shots=((0.0, 10.0),))
    grabber = FakeGrabber(duration_sec=10.0, default_std=16.8)

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert frame_set.count == 1, "std=16.8 距阈值 2.0 有 8 倍余量，必须留下"


def test_阈值可调(tmp_path) -> None:
    cutter = FakeCutter(shots=((0.0, 10.0),))
    grabber = FakeGrabber(duration_sec=10.0, default_std=16.8)

    frame_set = extract_frames(
        VIDEO, str(tmp_path), cutter=cutter, grabber=grabber, min_std_luma=20.0
    )

    assert frame_set.count == 0


# ── 单帧失败 ───────────────────────────────────────────────


def test_单帧读失败不影响其余帧(tmp_path) -> None:
    """一块坏数据不该让整条视频白跑，理由同 worker 收异常。"""
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0, fail_at_sec=(7.5,))

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert frame_set.count == 11, "12 帧里崩 1 帧，其余 11 帧仍要有"
    assert len(frame_set.skipped) == 1
    assert frame_set.skipped[0].reason == REASON_READ_FAILED
    assert frame_set.skipped[0].at_sec == 7.5


def test_多帧读失败也照常继续(tmp_path) -> None:
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0, fail_at_sec=(2.5, 7.5, 27.5))

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert frame_set.count == 9
    assert len(frame_set.skipped) == 3


def test_写盘失败记write_failed而不是read_failed(tmp_path) -> None:
    """编码失败是库的问题，写盘失败是磁盘或权限的问题，排查方向完全不同。"""
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(
        duration_sec=60.0, encode_exc=FrameReadFailed(0.0, "编码崩了")
    )

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert frame_set.count == 0
    assert all(s.reason == REASON_WRITE_FAILED for s in frame_set.skipped)


def test_单帧失败时其余帧仍在磁盘上(tmp_path) -> None:
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0, fail_at_sec=(2.5,))

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert frame_set.count == 11
    assert os.listdir(tmp_path), "其余帧必须真的落了盘"
    assert len(os.listdir(tmp_path)) == 11


# ── 视频打不开 ─────────────────────────────────────────────


def test_视频打不开要抛VideoUnreadable(tmp_path) -> None:
    """这是三层口径里唯一抛异常的一层。吞掉它会让「视频坏了」与「没内容」
    在库里长得一样，而降级策略完全不同——前者该重试或告警。"""
    grabber = FakeGrabber(probe_exc=VideoUnreadable(VIDEO, "缺编解码"))

    with pytest.raises(VideoUnreadable, match="缺编解码"):
        extract_frames(VIDEO, str(tmp_path), cutter=FakeCutter(), grabber=grabber)


def test_open时打不开也抛VideoUnreadable(tmp_path) -> None:
    """`probe` 成功但 `open` 失败（文件在两步之间被删）也要上抛。"""
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(
        duration_sec=60.0, open_exc=VideoUnreadable(VIDEO, "open 时打不开")
    )

    with pytest.raises(VideoUnreadable):
        extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)


def test_VideoUnreadable带路径与原因(tmp_path) -> None:
    """worker 的 jobs.error 要能说清是哪条视频、因为什么打不开。"""
    exc = VideoUnreadable(VIDEO, "不是视频文件")

    assert exc.path == VIDEO
    assert exc.reason == "不是视频文件"
    assert VIDEO in str(exc)


# ── 切镜头失败降级 ─────────────────────────────────────────


def test_切镜头抛异常走单镜头兜底(tmp_path) -> None:
    """一条视频的镜头检测失败不该让整条视频零帧。

    降级后 merge_shots 补一个覆盖全片的镜头，于是这条视频仍得到一帧代表画面。
    """
    cutter = FakeCutter(raise_exc=RuntimeError("PySceneDetect 在解码中途崩了"))
    grabber = FakeGrabber(duration_sec=60.0)

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert frame_set.count == 1, "兜底成一个覆盖全片的镜头 → 一帧"
    assert len(frame_set.shots) == 1
    assert (frame_set.shots[0].start_sec, frame_set.shots[0].end_sec) == (0.0, 60.0)
    assert frame_set.frames[0].at_sec == 30.0, "单镜头取中点"


def test_切镜头零结果也走单镜头兜底(tmp_path) -> None:
    """口播视频的正常路径：0 个切点。这不该是零帧，而是「整条一个镜头」。"""
    cutter = FakeCutter(shots=())
    grabber = FakeGrabber(duration_sec=60.0)

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert len(frame_set.shots) == 1
    assert frame_set.count == 1


def test_切镜头失败但仍按max_frames取帧(tmp_path) -> None:
    """兜底成单镜头后，max_frames 不该被忽略成"一帧"。"""
    cutter = FakeCutter(raise_exc=RuntimeError("崩"))
    grabber = FakeGrabber(duration_sec=60.0)

    frame_set = extract_frames(
        VIDEO, str(tmp_path), cutter=cutter, grabber=grabber, max_frames=1
    )

    assert frame_set.count == 1


# ── 元信息不可信 ───────────────────────────────────────────


def test_时长为零返回空集不抛异常(tmp_path) -> None:
    """抖音的 mp4 常见 duration 声明为 0。这不是故障，是元信息不可信。"""
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=0.0)

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert frame_set.count == 0
    assert cutter.calls == [], "元信息不可信时不该去切镜头"


def test_零帧结果的work_dir也要原样带回(tmp_path) -> None:
    """调用方按契约会拿 work_dir 去 rmtree，给空串会让 rmtree("") 抛
    FileNotFoundError——「没有帧」不该让调用方在清理这一步再失败一次。"""
    grabber = FakeGrabber(duration_sec=0.0)

    frame_set = extract_frames(
        VIDEO, str(tmp_path), cutter=FakeCutter(), grabber=grabber
    )

    assert frame_set.count == 0
    assert frame_set.work_dir == str(tmp_path)


def test_全空白时work_dir也原样带回(tmp_path) -> None:
    """同上，另一条返回零帧的路径。"""
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0, default_std=0.0)

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert frame_set.count == 0
    assert frame_set.work_dir == str(tmp_path)


def test_fps为零返回空集不抛异常(tmp_path) -> None:
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0, fps=0.0)

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert frame_set.count == 0


# ── 截断与参数校验 ─────────────────────────────────────────


def test_超长视频截断后尾部补一段(tmp_path) -> None:
    """30 分钟以上的视频才截断。补的那段标 partial，让下游知道不代表整段。"""
    cutter = FakeCutter(shots=((0.0, 120.0),))
    grabber = FakeGrabber(duration_sec=3600.0)

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert frame_set.scan.truncated is True
    assert frame_set.scan.scan_sec == 1800.0
    assert frame_set.shots[-1].partial is True
    assert frame_set.shots[-1].end_sec == 3600.0


def test_超长视频用跳帧压预算(tmp_path) -> None:
    """跳帧只损失切点精度、不损失检出率，所以优先用跳帧而不是截断。"""
    cutter = FakeCutter(shots=((0.0, 120.0),))
    grabber = FakeGrabber(duration_sec=3600.0)

    extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    plan = cutter.calls[0][1]
    assert plan.frame_skip == 2, "超过 20 分钟落在最慢档"


def test_max_frames生效(tmp_path) -> None:
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0)

    frame_set = extract_frames(
        VIDEO, str(tmp_path), cutter=cutter, grabber=grabber, max_frames=3
    )

    assert frame_set.count == 3


def test_镜头数少于max_frames时按镜头数出帧(tmp_path) -> None:
    """3 个镜头要 12 帧，不该硬凑 12 张重复画面。"""
    cutter = FakeCutter(shots=((0.0, 5.0), (5.0, 10.0), (10.0, 15.0)))
    grabber = FakeGrabber(duration_sec=15.0)

    frame_set = extract_frames(
        VIDEO, str(tmp_path), cutter=cutter, grabber=grabber, max_frames=12
    )

    assert frame_set.count == 3


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"max_frames": -1}, "max_frames"),
        ({"min_shot_sec": -0.1}, "min_shot_sec"),
        ({"max_scan_sec": 0.0}, "max_scan_sec"),
    ],
)
def test_非法参数要报错(tmp_path, kwargs, match) -> None:
    with pytest.raises(ValueError, match=match):
        extract_frames(
            VIDEO,
            str(tmp_path),
            cutter=FakeCutter(shots=shots_12x5()),
            grabber=FakeGrabber(duration_sec=60.0),
            **kwargs,
        )


def test_work_dir不存在时自动创建(tmp_path) -> None:
    """调用方不该为了抽帧先去 mkdir。"""
    nested = tmp_path / "深度" / "目录"
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0)

    frame_set = extract_frames(VIDEO, str(nested), cutter=cutter, grabber=grabber)

    assert nested.is_dir()
    assert frame_set.count == 12, "中文路径也要能写盘（不用 cv2.imwrite 的理由）"


def test_work_dir原样带回(tmp_path) -> None:
    """本层不清理，work_dir 交回调用方决定。"""
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0)

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert frame_set.work_dir == str(tmp_path)


def test_paths返回全部帧路径(tmp_path) -> None:
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0)

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert frame_set.paths() == tuple(f.path for f in frame_set.frames)
    assert len(frame_set.paths()) == 12


def test_meta与scan原样带在结果里(tmp_path) -> None:
    cutter = FakeCutter(shots=shots_12x5())
    grabber = FakeGrabber(duration_sec=60.0)

    frame_set = extract_frames(VIDEO, str(tmp_path), cutter=cutter, grabber=grabber)

    assert frame_set.meta.duration_sec == 60.0
    assert frame_set.meta.fps == 25.0
    assert frame_set.scan.truncated is False


# ── Cv2Grabber.encode 的纯算术部分 ────────────────────────────
#
# `encode` 里只有「缩放」与「参数校验」是纯算术：给它一个 numpy 数组就能测，
# 不需要真视频文件，也不需要解码器。真解码（probe / read_at）仍只能靠探针验。
#
# 这一段补上的理由：`long_side` 是**上限**不是目标尺寸，写成「一律缩到
# long_side」对小图会放大，对字幕是实打实的清晰度损失；而放大后不报错，
# 只是图变糊。这种错只能靠断言尺寸发现。
#
# `np` 与 `cv2` 在函数内 import：本文件的其余用例不许依赖它们（那部分是
# 假实现跑的快路径），所以只有这一段需要真库时再引入，模块顶层保持干净。


def _cv2():
    """拿 cv2 与 numpy。没装就跳过——本文件的其余用例不受影响。"""
    import numpy

    cv2 = pytest.importorskip("cv2")
    return cv2, numpy


def _solid_image(height: int, width: int):
    """造一张纯色 FrameImage，只要尺寸正确就能测缩放。"""
    _cv2mod, np = _cv2()
    from kb.vision.models import FrameImage

    return FrameImage(
        data=np.full((height, width, 3), 128, dtype=np.uint8),
        width=width,
        height=height,
        mean_luma=128.0,
        std_luma=0.0,
    )


def _decoded_size(data: bytes) -> tuple[int, int]:
    """把 JPEG 字节解回 (高, 宽)，用来断言真的缩到了目标尺寸。"""
    cv2, np = _cv2()
    arr = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert arr is not None, "编码出来的字节不是可解的 JPEG"
    return int(arr.shape[0]), int(arr.shape[1])


def test_encode竖屏超限缩到长边1024() -> None:
    """1080x1920 竖屏（抖音常见）：长边 1920 → 缩到 1024，比例保持。"""
    from kb.vision.grab import Cv2Grabber

    data = Cv2Grabber().encode(_solid_image(1920, 1080), long_side=1024, quality=85)

    assert _decoded_size(data) == (1024, 576), "长边要正好 1024，短边按比例"


def test_encode横屏超限缩到长边1024() -> None:
    """1920x1080 横屏（B 站常见）：长边是宽，缩完是 1024x576。"""
    from kb.vision.grab import Cv2Grabber

    data = Cv2Grabber().encode(_solid_image(1080, 1920), long_side=1024, quality=85)

    assert _decoded_size(data) == (576, 1024)


def test_encode小图不放大() -> None:
    """`long_side` 是上限不是目标：80x100 的图不该被放大到 1024。

    放大不报错，只是把字幕糊掉——这种错只能靠断言尺寸发现。
    """
    from kb.vision.grab import Cv2Grabber

    data = Cv2Grabber().encode(_solid_image(80, 100), long_side=1024, quality=85)

    assert _decoded_size(data) == (80, 100)


def test_encode质量参数确实影响体积() -> None:
    """用噪声图验，不能用纯色图。

    纯色图几乎没有高频细节，JPEG 在 quality=30 与 95 下体积相同——
    2026-11-28 先用纯色图测过一次，误判成「质量参数没生效」。
    噪声图对量化步长敏感，体积随质量单调增，才能证明参数真的传下去了。
    """
    from kb.vision.grab import Cv2Grabber, FrameImage

    cv2, np = _cv2()
    del cv2
    rng = np.random.default_rng(0)
    noisy = FrameImage(
        data=rng.integers(0, 256, (600, 800, 3), dtype=np.uint8),
        width=800,
        height=600,
        mean_luma=128.0,
        std_luma=70.0,
    )

    grabber = Cv2Grabber()
    low = len(grabber.encode(noisy, long_side=1024, quality=30))
    high = len(grabber.encode(noisy, long_side=1024, quality=95))

    assert high > low, "quality 从 30 提到 95，体积必须变大"


@pytest.mark.parametrize(
    ("long_side", "quality", "match"),
    [
        (0, 85, "long_side"),
        (-1, 85, "long_side"),
        (1024, 0, "quality"),
        (1024, 101, "quality"),
    ],
)
def test_encode非法参数要报错(long_side, quality, match) -> None:
    """越界参数在编码前拦掉，不要等到 cv2 给出含糊的失败。"""
    from kb.vision.grab import Cv2Grabber

    with pytest.raises(ValueError, match=match):
        Cv2Grabber().encode(_solid_image(10, 10), long_side=long_side, quality=quality)


def test_encode空帧数据要报错() -> None:
    """`data=None` 是「帧没读到」的口径，不该静默编码出空字节。"""
    from kb.vision.grab import Cv2Grabber, FrameReadFailed
    from kb.vision.models import FrameImage

    empty = FrameImage(data=None, width=10, height=10, mean_luma=0.0, std_luma=0.0)

    with pytest.raises(FrameReadFailed):
        Cv2Grabber().encode(empty, long_side=1024, quality=85)

"""媒体落盘编排：幂等、压缩触发、失败降级。

**全部用构造参数注入的假实现——不 monkeypatch。** 理由（设计 §3.11）：
构造参数写在签名里，读代码就能看见用了什么；patch 让「这个函数依赖什么」
变成运行时才知道。这与 `kb/vision/frames.py`、`kb/ai/transport.py` 的既定做法一致。

## 四类失败口径，每类一条用例

| 层 | 失败 | 处理 |
|---|---|---|
| 单条媒体 | 404 / 超时 / 超体积 / mime 不可判 | 记 `SkippedMedia`，**继续下一条** |
| 整条无 spec | 无封面无配图 | 不碰磁盘、返回空结果 |
| 阶段超时 | 超过 90 秒 | 剩余记 skipped，**正常返回** |
| 磁盘不足 | 低于 1 GiB | 整段跳过，全 skipped |

单条失败绝不能让另外 19 张一起丢——这是本模块最核心的一条。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kb.media import config, layout
from kb.media.encode import EncodeError
from kb.media.fetch import DownloadError, DownloadTooLarge
from kb.media.models import MediaSpec, PinnedTarget
from kb.media.persist import clean_incoming, store_media, summarize_skipped

# 1x1 PNG 的真字节（含正确魔数，`sniff` 能认）
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 200


# ── 假实现（构造参数注入）────────────────────────────────────


class FakeDownloader:
    """按 URL 脚本返回。记录每次调用，便于断言「有没有重下」。

    `responses` 的键是 URL，值是 `(status, headers, data)` 或 `Exception`。
    """

    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = responses
        self.calls: list[PinnedTarget] = []

    def get(
        self, target: PinnedTarget, *, max_bytes: int, deadline: float
    ) -> tuple[int, dict[str, str], bytes]:
        self.calls.append(target)
        # 用 host_header 找脚本：`connect_url` 是 IP，脚本按域名写更好读
        key = self._match(target)
        value = self._responses.get(key)
        if value is None:
            raise DownloadError(f"假下载器没有 {key} 的脚本")
        if isinstance(value, Exception):
            raise value
        status, headers, data = value  # type: ignore[misc]
        return int(status), dict(headers), bytes(data)

    def _match(self, target: PinnedTarget) -> str:
        """按 host 或完整 connect_url 找脚本，两种写法都支持。"""
        if target.host_header in self._responses:
            return target.host_header
        return target.connect_url


class NullEncoder:
    """原样返回。模拟「已经是合格 JPEG 且无需压缩」那条早返回路径。"""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def normalize(
        self, data: bytes, *, long_side: int, max_bytes: int
    ) -> tuple[bytes, int | None, int | None]:
        self.calls.append((long_side, max_bytes))
        return data, 640, 480


class ShrinkingEncoder(NullEncoder):
    """每次都返回不同的字节，用来验证「压缩后 sha256 变了 → key 跟着变」。"""

    def normalize(
        self, data: bytes, *, long_side: int, max_bytes: int
    ) -> tuple[bytes, int | None, int | None]:
        self.calls.append((long_side, max_bytes))
        return data + b"\x00", 320, 240


class FailingEncoder:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error or EncodeError("解码失败")

    def normalize(
        self, data: bytes, *, long_side: int, max_bytes: int
    ) -> tuple[bytes, int | None, int | None]:
        raise self._error


class FixedResolver:
    def __init__(self, table: dict[str, list[str]] | None = None) -> None:
        self._table = table or {"cdn.example": ["8.8.8.8"]}

    def __call__(self, host: str, port: int) -> list[str]:
        if host in self._table:
            return self._table[host]
        # 未登记的域名按公网处理，让用例只需关心要测的那个域
        return ["8.8.8.8"]


class Clock:
    """受控时钟：**每次 `__call__` 前进固定步长**。毫秒级跑完「阶段超时」用例。

    ⚠️ 每次读都前进，不只是「每条媒体前进一次」——`store_media` 对每条媒体
    读两次时钟（阶段闸门一次、算 deadline 一次）。写用例时必须知道这一点，
    否则断言会算错（本文件第一版就是这么错的：以为 5 条各读一次，
    实际 5 条读了 11 次）。
    """

    def __init__(self, step: float = 0.0, start: float = 0.0) -> None:
        self._now = start
        self._step = step
        self.reads = 0

    def __call__(self) -> float:
        current = self._now
        self._now += self._step
        self.reads += 1
        return current


class SequenceClock:
    """按预设序列返回。序列用完后一直返回最后一个值。

    比「每次前进固定步长」更精确：用例能直接写出「第几次读时钟时是几秒」，
    不依赖实现里有几次读。
    """

    def __init__(self, values: list[float]) -> None:
        self._values = list(values)
        self._index = 0

    def __call__(self) -> float:
        if not self._values:
            return 0.0
        value = self._values[min(self._index, len(self._values) - 1)]
        self._index += 1
        return value


def _spec(
    url: str = "https://cdn.example/a.jpg", role: str = "cover", order: int = 0
) -> MediaSpec:
    return MediaSpec(role=role, url=url, sort_order=order)


def _ok(
    data: bytes = PNG, ctype: str = "image/png"
) -> tuple[int, dict[str, str], bytes]:
    return 200, {"content-type": ctype}, data


def _run(
    specs: list[MediaSpec],
    *,
    downloader: object,
    tmp_path: Path,
    encoder: object | None = None,
    resolver: object | None = None,
    item_id: int = 7,
    clock: object | None = None,
    stage_budget_sec: float = config.STAGE_BUDGET_SEC,
):
    return store_media(
        specs,
        item_id=item_id,
        media_root=tmp_path,
        downloader=downloader,  # type: ignore[arg-type]
        encoder=encoder or NullEncoder(),  # type: ignore[arg-type]
        resolver=resolver or FixedResolver(),  # type: ignore[arg-type]
        clock=clock,  # type: ignore[arg-type]
        stage_budget_sec=stage_budget_sec,
    )


# ── 正常路径 ─────────────────────────────────────────────────


def test_一条封面成功落盘(tmp_path):
    result = _run(
        [_spec()],
        downloader=FakeDownloader({"cdn.example": _ok()}),
        tmp_path=tmp_path,
    )

    assert len(result.stored) == 1
    assert result.skipped == ()
    blob = result.stored[0]
    assert blob.role == "cover"
    assert blob.mime == "image/png"
    assert blob.size_bytes == len(PNG)
    assert (tmp_path / blob.storage_key).is_file(), "文件没真的落盘"


def test_落盘路径符合2位hex_32位hex的形状(tmp_path):
    """`<2位hex>/<32位hex>.<ext>`。这条让「扫全盘」与「按前缀分目录」都成立。"""
    result = _run(
        [_spec()], downloader=FakeDownloader({"cdn.example": _ok()}), tmp_path=tmp_path
    )
    key = result.stored[0].storage_key
    directory, filename = key.split("/")
    assert len(directory) == 2
    assert len(filename) == 32 + len(".png")
    assert layout.is_valid_key(key)


def test_sha256是落盘字节的哈希(tmp_path):
    """`sha256` 必须是对**最终字节**算的（压缩后），否则它证明不了文件内容。"""
    result = _run(
        [_spec()],
        downloader=FakeDownloader({"cdn.example": _ok()}),
        tmp_path=tmp_path,
        encoder=NullEncoder(),
    )
    blob = result.stored[0]
    on_disk = (tmp_path / blob.storage_key).read_bytes()
    assert blob.sha256 == hashlib.sha256(on_disk).hexdigest()


def test_压缩器收到的参数与config一致(tmp_path):
    """`long_side=1600` 与 `max_bytes=200KiB` 是 PRD §5.5 的要求，
    不能靠调用方每次传对。"""
    encoder = NullEncoder()
    _run(
        [_spec()],
        downloader=FakeDownloader({"cdn.example": _ok()}),
        tmp_path=tmp_path,
        encoder=encoder,
    )
    assert encoder.calls == [(config.LONG_SIDE, config.TARGET_BYTES)]
    assert config.LONG_SIDE == 1600
    assert config.TARGET_BYTES == 200 * 1024


def test_宽高来自压缩器(tmp_path):
    result = _run(
        [_spec()], downloader=FakeDownloader({"cdn.example": _ok()}), tmp_path=tmp_path
    )
    assert (result.stored[0].width, result.stored[0].height) == (640, 480)


def test_duration_sec本轮恒为None(tmp_path):
    """不下视频，所以没有时长。保留字段是为了将来视频落盘时签名不用改。"""
    result = _run(
        [_spec()], downloader=FakeDownloader({"cdn.example": _ok()}), tmp_path=tmp_path
    )
    assert result.stored[0].duration_sec is None


def test_多张图各自落盘(tmp_path):
    specs = [
        _spec(f"https://cdn.example/{i}.jpg", role="image", order=i + 1)
        for i in range(3)
    ]
    downloader = FakeDownloader(
        {f"https://8.8.8.8/{i}.jpg": _ok(PNG + bytes([i])) for i in range(3)}
    )
    result = _run(specs, downloader=downloader, tmp_path=tmp_path)

    assert len(result.stored) == 3
    assert sorted(b.sort_order for b in result.stored) == [0, 1, 2] or [
        b.sort_order for b in result.stored
    ] == [1, 2, 3]
    assert len({b.storage_key for b in result.stored}) == 3, "三份不同内容该有三个 key"


# ── 幂等：同 item 同内容 → 同 key，不重写文件 ────────────────


def test_重跑得到同一个key且不重写文件(tmp_path):
    """key 由 `sha256(item_id + 内容)` 派生，所以重跑必然同一个 key。

    文件层再判 `exists()` 跳过写入：内容相同，重写只会多一次 IO 与改 mtime。
    这条是「重试幂等」在文件层的落地。
    """
    specs = [_spec()]
    downloader = FakeDownloader({"cdn.example": _ok()})

    first = _run(specs, downloader=downloader, tmp_path=tmp_path)
    path = tmp_path / first.stored[0].storage_key
    mtime_before = path.stat().st_mtime_ns

    second = _run(specs, downloader=downloader, tmp_path=tmp_path)

    assert second.stored[0].storage_key == first.stored[0].storage_key
    assert path.stat().st_mtime_ns == mtime_before, "文件被重写了，不是幂等"


def test_不同item同一内容得到不同key(tmp_path):
    """`attachments_key_uniq` 是全局唯一索引。两个 item 的同一张图必须两个 key，
    否则第二条插入撞索引、第二个 item 永远没有封面。

    磁盘上存两份同样的字节是**有意接受的**代价（设计 §0.3）。
    """
    downloader = FakeDownloader({"cdn.example": _ok()})
    first = _run([_spec()], downloader=downloader, tmp_path=tmp_path, item_id=1)
    second = _run([_spec()], downloader=downloader, tmp_path=tmp_path, item_id=2)

    assert first.stored[0].storage_key != second.stored[0].storage_key
    assert len(list(tmp_path.rglob("*.png"))) == 2, "两份字节两个文件"


def test_内容变化导致key变化(tmp_path):
    """上游换了封面 → sha256 变 → key 变 → 新的行替换旧的行。

    旧文件成孤儿，由将来的清理逻辑回收（本轮不实现清理）。
    """
    first = _run(
        [_spec()],
        downloader=FakeDownloader({"cdn.example": _ok(PNG)}),
        tmp_path=tmp_path,
    )
    second = _run(
        [_spec()],
        downloader=FakeDownloader({"cdn.example": _ok(PNG + b"x")}),
        tmp_path=tmp_path,
    )
    assert first.stored[0].storage_key != second.stored[0].storage_key


def test_同一批里不同内容的spec永不派生重复key(tmp_path):
    """**真机验证抓到的第二个 bug 的判据。**

    `attachments_key_uniq` 是 `storage_key` 上的**全局**唯一索引（索引里没有
    `role`）。所以同一批 `StoredBlob` 里出现两个相同 key 时，真库会让整条
    `replace_for_item` 失败——**一条都存不下来**，而不是「第二条被跳过」。

    真机复现的形态：图文内容原本会产 cover + image 两条同 URL 的 spec，
    派生同一个 key。修法是 `plan.py` 不为图文额外产 cover 行。

    这条用例在 `store_media` 的出口处断言「不重复」，是拦在真库之前的
    最后一道逻辑防线。
    """
    specs = [
        _spec("https://cdn.example/a.jpg", role="cover", order=0),
        _spec("https://cdn.example/b.jpg", role="image", order=1),
        _spec("https://cdn.example/c.jpg", role="image", order=2),
    ]
    downloader = FakeDownloader(
        {
            "https://8.8.8.8/a.jpg": _ok(PNG),
            "https://8.8.8.8/b.jpg": _ok(PNG + b"\x01"),
            "https://8.8.8.8/c.jpg": _ok(PNG + b"\x02"),
        }
    )
    result = _run(specs, downloader=downloader, tmp_path=tmp_path)

    keys = [b.storage_key for b in result.stored]
    assert len(set(keys)) == len(keys), (
        f"同一批里出现了重复 storage_key，真库会撞 attachments_key_uniq：{keys}"
    )


def test_同URL重复下载时只产生一个key(tmp_path):
    """同一 URL 被规划两次（plan 的 URL 去重没兜住的极端情形）。

    此时两个 `StoredBlob` 会派生同一个 key。这条用例记录**当前行为**：
    `store_media` 不去重，重复 key 会原样出现在 `stored` 里。
    真库会撞唯一索引——所以防线必须在 `plan.py` 的 URL 去重上
    （`test_media_plan.py::test_图文的spec里URL互不重复`）。
    """
    specs = [
        _spec("https://cdn.example/same.jpg", role="cover", order=0),
        _spec("https://cdn.example/same.jpg", role="image", order=1),
    ]
    downloader = FakeDownloader({"https://8.8.8.8/same.jpg": _ok(PNG)})
    result = _run(specs, downloader=downloader, tmp_path=tmp_path)

    keys = [b.storage_key for b in result.stored]
    assert len(keys) == 2, "store_media 不做去重，两条都会返回"
    assert len(set(keys)) == 1, (
        "同 URL 同内容必然派生同一个 key——这正是 plan 层必须做 URL 去重的原因"
    )
    assert len(list(tmp_path.rglob("*.png"))) == 1, "文件层靠 exists() 天然只写一份"


# ── 失败降级：一条失败不影响其余 ────────────────────────────


def test_单条HTTP403记skipped但其余照常(tmp_path):
    """**这是本模块最核心的一条。** 一张图 404 不该让另外 2 张一起丢。"""
    specs = [
        _spec("https://cdn.example/bad.jpg", role="cover"),
        _spec("https://cdn.example/a.jpg", role="image", order=1),
        _spec("https://cdn.example/b.jpg", role="image", order=2),
    ]
    downloader = FakeDownloader(
        {
            "https://8.8.8.8/bad.jpg": (403, {}, b""),
            "https://8.8.8.8/a.jpg": _ok(PNG),
            "https://8.8.8.8/b.jpg": _ok(PNG + b"\x01"),
        }
    )
    result = _run(specs, downloader=downloader, tmp_path=tmp_path)

    assert len(result.stored) == 2, "另外两张必须存下来"
    assert len(result.skipped) == 1
    assert "403" in result.skipped[0].reason


def test_重定向不跟随而是记skipped(tmp_path):
    """一个 `302 → http://127.0.0.1:8000/` 能绕过 SSRF 判定的全部三层
    （那三层只校验了第一个 URL）。CDN 的图片地址是直链、不需要跳转。
    """
    result = _run(
        [_spec()],
        downloader=FakeDownloader(
            {"cdn.example": (302, {"location": "http://127.0.0.1:8000/"}, b"")}
        ),
        tmp_path=tmp_path,
    )
    assert result.stored == ()
    assert "重定向" in result.skipped[0].reason


def test_非2xx记skipped(tmp_path):
    for status in (404, 500, 503):
        result = _run(
            [_spec()],
            downloader=FakeDownloader({"cdn.example": (status, {}, b"x")}),
            tmp_path=tmp_path,
        )
        assert result.stored == ()
        assert str(status) in result.skipped[0].reason


def test_空响应体记skipped(tmp_path):
    """空体不可能是图。若放过去，`sniff` 会返回 None，但错误信息会变成
    「mime 无法判定」，看不出真实原因是「CDN 给了空响应」。"""
    result = _run(
        [_spec()],
        downloader=FakeDownloader({"cdn.example": (200, {}, b"")}),
        tmp_path=tmp_path,
    )
    assert "空" in result.skipped[0].reason


def test_超体积抛的DownloadTooLarge被收成skipped(tmp_path):
    """`fetch` 超体积时抛异常，这里必须收成 skipped 而不是让它冒到 worker。"""
    result = _run(
        [_spec()],
        downloader=FakeDownloader({"cdn.example": DownloadTooLarge("超 8 MiB")}),
        tmp_path=tmp_path,
    )
    assert result.stored == ()
    assert "下载失败" in result.skipped[0].reason


def test_下载异常被收成skipped(tmp_path):
    result = _run(
        [_spec()],
        downloader=FakeDownloader({"cdn.example": DownloadError("连接超时")}),
        tmp_path=tmp_path,
    )
    assert result.stored == ()
    assert "连接超时" in result.skipped[0].reason


def test_mime判不出记skipped(tmp_path):
    """签名 URL 失效后 CDN 返回 HTML 错误页——这正是判不出 mime 的典型场景。

    若把它当图存下来，`/api/media` 会以我们的源站身份返回第三方 HTML。
    """
    result = _run(
        [_spec()],
        downloader=FakeDownloader(
            {"cdn.example": _ok(b"<html>403</html>", "text/html")}
        ),
        tmp_path=tmp_path,
    )
    assert result.stored == ()
    assert "mime" in result.skipped[0].reason


def test_mime与role不符记skipped(tmp_path):
    """封面拿到 video/mp4 —— 上游把视频地址塞进了封面字段。"""
    result = _run(
        [_spec(role="cover")],
        downloader=FakeDownloader({"cdn.example": _ok(JPEG, "video/mp4")}),
        tmp_path=tmp_path,
    )
    assert result.stored == ()
    assert "不符" in result.skipped[0].reason


def test_不支持的图片格式记skipped(tmp_path):
    """`image/tiff` 能过 mime 白名单但没登记扩展名——不能猜一个 `.bin`。"""
    result = _run(
        [_spec()],
        downloader=FakeDownloader({"cdn.example": _ok(JPEG, "image/tiff")}),
        tmp_path=tmp_path,
    )
    assert result.stored == ()
    assert "不支持" in result.skipped[0].reason


def test_编码失败记skipped(tmp_path):
    result = _run(
        [_spec()],
        downloader=FakeDownloader({"cdn.example": _ok()}),
        tmp_path=tmp_path,
        encoder=FailingEncoder(),
    )
    assert result.stored == ()
    assert "解码失败" in result.skipped[0].reason


def test_编码器抛非预期异常也收成skipped(tmp_path):
    """假实现（或将来换的编码器）可能抛任何东西。它不该让整批媒体丢。"""
    result = _run(
        [_spec()],
        downloader=FakeDownloader({"cdn.example": _ok()}),
        tmp_path=tmp_path,
        encoder=FailingEncoder(RuntimeError("Pillow 内部炸了")),
    )
    assert result.stored == ()
    assert "图片处理失败" in result.skipped[0].reason


class _ExplodingResolver:
    """第 `break_at` 次调用抛非 OSError 的异常。

    用来验「一条媒体崩了，已下载好的前几条不能丢」——这是本模块核心口径
    的最直接判据。
    """

    def __init__(self, break_at: int) -> None:
        self._break_at = break_at
        self.calls = 0

    def __call__(self, host: str, port: int) -> list[str]:
        self.calls += 1
        if self.calls == self._break_at:
            raise RuntimeError("resolver 内部炸了")
        return ["8.8.8.8"]


def test_单条未预期异常不丢已下载的其余条目(tmp_path):
    """**这条是「一条失败不影响其余」的最终判据。**

    `stored` 是 `store_media` 的局部变量。若第 3 条的 resolver 抛了一个
    没被收掉的异常，异常会跳出整个 `for spec in specs` 循环，函数异常退出
    ——**前 2 条已经下载、已经压好的字节全丢**。

    真实触发面：`system_resolver` 走 `getaddrinfo`，hostname 含非法 IDNA
    字符时抛 `UnicodeError`（不是 OSError）。20 张图里第 3 张这样炸掉，
    整条内容的封面与配图一张都没有。

    这条用例在设计原文下必红（`except OSError` + 主循环只认 `_Skip`）。
    """
    specs = [
        _spec("https://cdn.example/a.jpg", role="cover", order=0),
        _spec("https://cdn.example/b.jpg", role="image", order=1),
        _spec("https://cdn.example/c.jpg", role="image", order=2),
        _spec("https://cdn.example/d.jpg", role="image", order=3),
    ]
    downloader = FakeDownloader(
        {
            "https://8.8.8.8/a.jpg": _ok(PNG),
            "https://8.8.8.8/b.jpg": _ok(PNG + b"\x01"),
            "https://8.8.8.8/c.jpg": _ok(PNG + b"\x02"),
            "https://8.8.8.8/d.jpg": _ok(PNG + b"\x03"),
        }
    )
    # 第 3 次解析（第 3 条 spec）炸掉
    result = _run(
        specs,
        downloader=downloader,
        tmp_path=tmp_path,
        resolver=_ExplodingResolver(break_at=3),
    )

    assert len(result.stored) == 3, (
        f"前 2 条 + 第 4 条都该保住，实际只存了 {len(result.stored)} 条"
    )
    assert len(result.skipped) == 1
    assert "解析失败" in result.skipped[0].reason, (
        f"错误原因该指向解析失败而不是别的：{result.skipped[0].reason}"
    )
    assert len(list(tmp_path.rglob("*.png"))) == 3, "磁盘上的文件数也不该少于成功数"


def test_编码器未预期异常不影响其余条目(tmp_path):
    """与上一条同构，但触发点在 encoder 而不是 resolver。

    两条都要有：`check_url` 的兜底与主循环的兜底是**两道独立防线**，
    只验其中一道时另一道坏了不会有人发现。
    """
    specs = [
        _spec("https://cdn.example/a.jpg", role="cover", order=0),
        _spec("https://cdn.example/b.jpg", role="image", order=1),
    ]

    class _FlakyEncoder(NullEncoder):
        """第二次调用抛非 EncodeError 的异常（模拟 Pillow 内部炸）。"""

        def __init__(self) -> None:
            super().__init__()
            self._n = 0

        def normalize(
            self, data: bytes, *, long_side: int, max_bytes: int
        ) -> tuple[bytes, int | None, int | None]:
            self._n += 1
            if self._n == 2:
                raise RuntimeError("Pillow 内部炸了")
            return super().normalize(data, long_side=long_side, max_bytes=max_bytes)

    result = _run(
        specs,
        downloader=FakeDownloader(
            {
                "https://8.8.8.8/a.jpg": _ok(PNG),
                "https://8.8.8.8/b.jpg": _ok(PNG + b"\x01"),
            }
        ),
        tmp_path=tmp_path,
        encoder=_FlakyEncoder(),
    )

    assert len(result.stored) == 1, "第一条该保住"
    assert len(result.skipped) == 1
    assert (
        "未预期异常" in result.skipped[0].reason
        or "图片处理失败" in result.skipped[0].reason
    )


def test_SSRF判定拒绝记skipped(tmp_path):
    """内网地址在 `check_url` 就被拒，**不会**发出任何请求。"""
    downloader = FakeDownloader({"cdn.example": _ok()})
    result = _run(
        [_spec("http://127.0.0.1:8000/api/items")],
        downloader=downloader,
        tmp_path=tmp_path,
    )
    assert result.stored == ()
    assert downloader.calls == [], "被拒的地址绝不该发出请求"
    assert "不可用" in result.skipped[0].reason


def test_端口不允许时也跳过且不发请求(tmp_path):
    downloader = FakeDownloader({"cdn.example": _ok()})
    result = _run(
        [_spec("https://cdn.example:5432/a.jpg")],
        downloader=downloader,
        tmp_path=tmp_path,
    )
    assert result.stored == ()
    assert downloader.calls == []


def test_写盘失败记skipped(tmp_path):
    """磁盘满/只读。把它收成 skipped 而不是抛——一张图存不下不该让整条 fetch 失败。"""
    # 让 media_root 变成一个**文件**，于是 mkdir 与 open 都会失败
    broken = tmp_path / "not-a-dir"
    broken.write_text("x", encoding="utf-8")

    with pytest.raises(RuntimeError):
        # `_ensure_root` 里建目录失败是**编程错误**（配置写错），必须抛
        _run(
            [_spec()],
            downloader=FakeDownloader({"cdn.example": _ok()}),
            tmp_path=broken,
        )


def test_目录建不出来时抛RuntimeError而不是静默丢全部(tmp_path):
    """`media_root` 配错（指向一个文件、或挂载点不存在）属编程错误。

    降级成 skipped 会让整批媒体**静默丢失**，而错误信息只有「写盘失败」。
    """
    broken = tmp_path / "a-file"
    broken.write_text("x", encoding="utf-8")

    with pytest.raises(RuntimeError) as info:
        _run(
            [_spec()],
            downloader=FakeDownloader({"cdn.example": _ok()}),
            tmp_path=broken,
        )
    assert "媒体目录" in str(info.value)


# ── 阶段预算与磁盘闸门 ──────────────────────────────────────


def test_阶段超时的剩余条目记skipped(tmp_path):
    """单条上限 20 秒 × 20 张 = 400 秒，而一次 fetch 占住队列 400 秒不可接受。

    90 秒闸门到点后剩余条目记 skipped 并**正常返回**——部分成功比
    「全有或全无」有用得多。

    ## 时钟序列怎么算

    `store_media` 对**每条**媒体读两次时钟：一次判阶段闸门、一次算 deadline。
    再算上函数入口那一次 `started = now()`，5 条媒体共读 11 次：

    ```
    读次  1        2        3        4        5        6        7  ...
    值    0        0        20       40       40       60       80
         started  闸门①   deadline① 闸门②   deadline② 闸门③   deadline③
    ```

    `SequenceClock(step=20)` 给出的正是这个形状，于是第 4 次读（闸门③）
    是 60 秒、第 6 次（闸门④）是 100 秒 > 90 秒 → 第 4、5 条被跳过
    ——但 `SequenceClock` 每次读递增，所以这里直接用 `Clock(step=20)` 的
    递增语义配合断言「至少存了 1 条、后面的被跳过」，而不是钉死具体条数。
    钉死条数会让这条用例变成「实现读了几次时钟」的测试，而不是
    「闸门会不会生效」的测试。
    """
    specs = [
        _spec(f"https://cdn.example/{i}.jpg", role="image", order=i) for i in range(5)
    ]
    downloader = FakeDownloader(
        {f"https://8.8.8.8/{i}.jpg": _ok(PNG + bytes([i])) for i in range(5)}
    )
    clock = Clock(step=25.0)
    result = _run(
        specs,
        downloader=downloader,
        tmp_path=tmp_path,
        clock=clock,
        stage_budget_sec=90.0,
    )

    assert len(result.stored) >= 1, "闸门不该把全部条目都砍掉"
    assert len(result.skipped) >= 1, "90 秒闸门必须真的生效"
    assert all("超时" in s.reason for s in result.skipped)


def test_阶段闸门只砍后面的不砍前面的(tmp_path):
    """闸门是「到点就停」，不是「抽样」。已存下来的条目必须都在前面。"""
    specs = [
        _spec(f"https://cdn.example/{i}.jpg", role="image", order=i) for i in range(6)
    ]
    downloader = FakeDownloader(
        {f"https://8.8.8.8/{i}.jpg": _ok(PNG + bytes([i])) for i in range(6)}
    )
    result = _run(
        specs,
        downloader=downloader,
        tmp_path=tmp_path,
        clock=Clock(step=25.0),
        stage_budget_sec=90.0,
    )

    stored_orders = [b.sort_order for b in result.stored]
    skipped_orders = [
        i for i, spec in enumerate(specs) if spec.url in {s.url for s in result.skipped}
    ]
    assert stored_orders == sorted(stored_orders), "存下来的顺序被打乱了"
    if stored_orders and skipped_orders:
        assert max(stored_orders) < min(skipped_orders), (
            "跳过的条目出现在已存条目之前——闸门判反了"
        )


def test_阶段预算宽松时全部处理(tmp_path):
    """预算足够时闸门不该误砍：这条挡住「闸门算错导致部分条目永远存不下来」。"""
    specs = [
        _spec(f"https://cdn.example/{i}.jpg", role="image", order=i) for i in range(5)
    ]
    downloader = FakeDownloader(
        {f"https://8.8.8.8/{i}.jpg": _ok(PNG + bytes([i])) for i in range(5)}
    )
    result = _run(
        specs,
        downloader=downloader,
        tmp_path=tmp_path,
        clock=Clock(step=1.0),
        stage_budget_sec=1000.0,
    )
    assert len(result.stored) == 5
    assert result.skipped == ()


def test_阶段预算为0时全部跳过(tmp_path):
    specs = [_spec()]
    result = _run(
        specs,
        downloader=FakeDownloader({"cdn.example": _ok()}),
        tmp_path=tmp_path,
        clock=Clock(step=1.0),
        stage_budget_sec=0.5,
    )
    assert result.stored == ()
    assert len(result.skipped) == 1


def test_负的阶段预算是编程错误(tmp_path):
    with pytest.raises(ValueError):
        _run(
            [_spec()],
            downloader=FakeDownloader({"cdn.example": _ok()}),
            tmp_path=tmp_path,
            stage_budget_sec=-1.0,
        )


def test_磁盘不足时整段跳过(tmp_path, monkeypatch):
    """低于 1 GiB 时整个媒体阶段跳过，写 `props.mediaError`，fetch 仍算成功。

    这里用 monkeypatch 改 `shutil.disk_usage`：它是**标准库的物理测量**，
    没有可注入的接缝（构造参数注入能做的都做了；磁盘剩余量不是一个
    「依赖」，而是一个环境事实）。判据的逻辑在 `_free_bytes` 与
    `MIN_FREE_BYTES` 的比较上，那条由这条用例覆盖。
    """
    import kb.media.persist as persist_module

    class _Usage:
        free = 100 * 1024 * 1024  # 100 MB < 1 GiB

    monkeypatch.setattr(persist_module.shutil, "disk_usage", lambda _p: _Usage())
    downloader = FakeDownloader({"cdn.example": _ok()})
    result = _run([_spec()], downloader=downloader, tmp_path=tmp_path)

    assert result.stored == ()
    assert "磁盘" in result.skipped[0].reason
    assert downloader.calls == [], "磁盘不足时不该发出任何请求"


# ── 空清单 ───────────────────────────────────────────────────


def test_空spec清单直接返回空结果不碰磁盘(tmp_path):
    result = store_media(
        [],
        item_id=1,
        media_root=tmp_path,
        downloader=FakeDownloader({}),  # type: ignore[arg-type]
        encoder=NullEncoder(),
        resolver=FixedResolver(),
    )
    assert result.stored == ()
    assert result.skipped == ()
    assert not (tmp_path / config.INCOMING_DIR).exists(), "空清单不该建目录"


# ── summarize_skipped ────────────────────────────────────────


def test_没有skipped时返回空串():
    """空串是「这次全部成功」的信号，`set_media_props` 据此删掉 `mediaError` 键。"""
    assert summarize_skipped(()) == ""


def test_汇总串含role与原因():
    from kb.media.models import SkippedMedia

    text = summarize_skipped(
        (SkippedMedia(role="cover", url="https://x.invalid/a", reason="HTTP 403"),)
    )
    assert "cover" in text
    assert "HTTP 403" in text


def test_超过3条时只写前3条并注明剩余():
    """20 条失败塞进 jsonb 是一段没人读的日志，而第一条原因通常就是根因
    （CDN 域名封了、磁盘满了）。"""
    from kb.media.models import SkippedMedia

    skipped = tuple(
        SkippedMedia(
            role="image", url=f"https://x.invalid/{i}", reason=f"HTTP {400 + i}"
        )
        for i in range(5)
    )
    text = summarize_skipped(skipped)
    assert "HTTP 400" in text and "HTTP 402" in text
    assert "HTTP 403" not in text, "第 4 条不该出现"
    assert "另有 2 条" in text


# ── .incoming 清理 ───────────────────────────────────────────


def test_clean_incoming清掉残留的part文件(tmp_path):
    """上次被 kill 留下的 `.part` 与正式文件同目录，`stats()` 看不到，
    会悄悄吃掉磁盘。由进程启动时清空（与 lifespan 里清过期会话同一思路）。
    """
    incoming = tmp_path / config.INCOMING_DIR
    incoming.mkdir()
    (incoming / "abc.part").write_bytes(b"half image")
    (incoming / "def.part").write_bytes(b"another")

    removed = clean_incoming(tmp_path)

    assert removed == 2
    assert list(incoming.iterdir()) == []


def test_clean_incoming不动正式文件(tmp_path):
    """清理范围只能是 `.incoming/`。误删正式文件的后果是「记录在、文件没了」。"""
    incoming = tmp_path / config.INCOMING_DIR
    incoming.mkdir()
    (tmp_path / "ab").mkdir()
    keeper = tmp_path / "ab" / ("c" * 32 + ".jpg")
    keeper.write_bytes(b"real file")

    clean_incoming(tmp_path)

    assert keeper.is_file(), "正式文件被误删了"


def test_clean_incoming对不存在的目录返0(tmp_path):
    """没有 `.incoming/` 说明从没下载过，不是错误。"""
    assert clean_incoming(tmp_path / "never-created") == 0


# ── 顶层不 import 外部库 ─────────────────────────────────────


def test_persist不import外部库也不import_store(tmp_path):
    """两条架构铁律都要在用例里钉一次（`eng_checks` 是可选的，pytest 必跑）。"""
    import ast

    path = Path(__file__).resolve().parents[1] / "kb" / "media" / "persist.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    banned: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            banned += [a.name for a in node.names if a.name in {"httpx", "PIL"}]
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.level == 0
            and node.module.split(".")[0] in {"httpx", "PIL", "kb"}
        ):
            banned.append(node.module)
    # `kb.media.*` 是相对 import（level>0），所以上面只抓 `kb.store` 这类绝对 import
    assert not banned, f"persist 层不该 import 这些：{banned}"

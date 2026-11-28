"""`storage_key` 派生、格式校验、扩展名映射（纯逻辑，不碰磁盘不碰库）。

**这一组守三条性质，每条都有一个具体的坏结果对应：**

1. **key 混入 `item_id`**：纯内容哈希会让两个 item 的同一张图撞
   `attachments_key_uniq`（全局唯一索引），补救只有 `on conflict do nothing`
   ——于是第二个 item 永远没有封面，而字节明明在磁盘上。
2. **格式正则只允许小写十六进制**：`..` 里的点不属于 `[0-9a-f]`，`/` 只能出现在
   第 3 个字符位。这个正则本身就是 `/api/media/{key}` 路径穿越的充分防线。
3. **扩展名只由 mime 决定**：上游 `ext` 是第三方响应里的字符串，
   实测出现过空串与不可信值；取 URL 后缀则会让 `evil.html` 变成真漏洞。
"""

from __future__ import annotations

import hashlib

import pytest

from kb.media import layout

SHA = hashlib.sha256(b"some image bytes").hexdigest()


# ── storage_key 的形状 ────────────────────────────────────────


def test_key_shape_is_2_hex_dir_and_32_hex_name():
    """`ab/cdef…0123.jpg`：2 位一级目录 + 32 位文件名。

    2 位目录的意义是避免单目录堆十几万文件（ext4/xfs 的 `readdir` 会明显变慢）。
    """
    key = layout.storage_key(7, SHA, "jpg")
    directory, filename = key.split("/")
    assert len(directory) == 2
    assert len(filename) == len("0" * 32) + len(".jpg")
    assert filename.endswith(".jpg")
    assert layout.KEY_RE.fullmatch(key), f"派生出的 key 不匹配自己的正则：{key}"


def test_key_is_deterministic():
    """同输入同输出。这条是「重试幂等」的地基——key 变了就是新文件、新附件行。"""
    assert layout.storage_key(7, SHA, "jpg") == layout.storage_key(7, SHA, "jpg")


def test_same_content_different_items_get_different_keys():
    """**这是本模块最重要的用例。**

    `attachments_key_uniq` 是 `storage_key` 上的全局唯一索引。两个不同 item
    引用同一份字节（同一张转载图、同一张占位图）时，纯内容哈希会让第二条插入
    撞索引。混入 `item_id` 之后两者永不相等。

    磁盘上会存两份同样的字节——为 200 KB 的封面付这点代价，换掉一整个
    「跨 item 删除竞态」的问题类别，是划算的。
    """
    first = layout.storage_key(1, SHA, "jpg")
    second = layout.storage_key(2, SHA, "jpg")
    assert first != second, "两个 item 的同一份内容算出了同一个 key，会撞唯一索引"


def test_different_content_same_item_gets_different_keys():
    """内容变了 key 就要变——否则改过封面之后旧文件会被新内容「占住」而没人发现。"""
    other = hashlib.sha256(b"other bytes").hexdigest()
    assert layout.storage_key(1, SHA, "jpg") != layout.storage_key(1, other, "jpg")


def test_extension_participates_in_key():
    """同一份字节当 jpg 与当 png 派生不同 key。

    理由：扩展名是路径的一部分，两条 key 指向两个不同路径。若扩展名不参与哈希，
    `ab/x.jpg` 与 `ab/x.png` 会算成不同的完整 key 但相同的前 34 位——
    120 位哈希仍然够，但如果哪天有人把 key 截短，这个性质会救一次。
    """
    assert layout.storage_key(1, SHA, "jpg") != layout.storage_key(1, SHA, "png")


def test_empty_ext_is_rejected():
    """空扩展名会让 key 长成 `ab/cdef….`——正则不匹配，`/api/media` 永远 404。

    在派生处就抛，比「存进库之后才发现读不出来」早得多。
    """
    with pytest.raises(ValueError):
        layout.storage_key(1, SHA, "")


# ── is_valid_key：路径穿越的充分防线 ─────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        layout.storage_key(1, SHA, "jpg"),
        layout.storage_key(99, SHA, "webp"),
        "ab/" + "0" * 32 + ".jpg",
        "ff/" + "f" * 32 + ".png",
    ],
)
def test_valid_keys_accepted(key: str) -> None:
    assert layout.is_valid_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "",
        "/",
        "abc",
        "ab/" + "a" * 32,  # 没有扩展名
        "ab/" + "a" * 32 + ".",  # 扩展名为空
        "ab/" + "a" * 31 + ".jpg",  # 文件名 31 位
        "ab/" + "a" * 33 + ".jpg",  # 文件名 33 位
        "a/" + "a" * 32 + ".jpg",  # 目录 1 位
        "abc/" + "a" * 32 + ".jpg",  # 目录 3 位
        "AB/" + "a" * 32 + ".jpg",  # 大写目录
        "ab/" + "A" * 32 + ".jpg",  # 大写文件名
        "../" + "a" * 32 + ".jpg",  # 路径穿越
        "ab/../../../etc/passwd",
        "ab/" + "a" * 32 + ".jpg/../../x",
        "ab/" + "a" * 32 + ".tar.gz",  # 两段扩展名
        "ab/" + "a" * 32 + "." + "j" * 6,  # 扩展名 6 位
        "ab\\" + "a" * 32 + ".jpg",  # 反斜杠（Windows 分隔符）
        "ab/" + "a" * 32 + ".jpg\n",  # 尾随换行
    ],
)
def test_invalid_keys_rejected(key: str) -> None:
    """这些全都必须是 `False`——每一条都是一次真实的拒绝理由。"""
    assert not layout.is_valid_key(key), f"这个 key 不该通过：{key!r}"


def test_key_with_absolute_path_rejected():
    """绝对路径（`/etc/passwd`）不能通过：正则要求第 1 个字符是十六进制。"""
    assert not layout.is_valid_key("/etc/passwd")


# ── ext_for_mime ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mime", "expected"),
    [
        ("image/jpeg", "jpg"),
        ("image/png", "png"),
        ("image/gif", "gif"),
        ("image/webp", "webp"),
        ("image/avif", "avif"),
        ("image/jpeg; charset=binary", "jpg"),
        ("IMAGE/JPEG", "jpg"),
        ("  image/png  ", "png"),
    ],
)
def test_ext_for_mime_known(mime: str, expected: str) -> None:
    assert layout.ext_for_mime(mime) == expected


@pytest.mark.parametrize(
    "mime",
    [
        "image/svg+xml",  # 禁用集，不该有扩展名
        "image/tiff",  # 未登记
        "video/mp4",  # 本轮不下
        "",
        "not-a-mime",
    ],
)
def test_ext_for_mime_unknown_returns_none(mime: str) -> None:
    """返回 `None` 让调用方拒绝该 blob——**不给**「猜一个 .bin」的退路。"""
    assert layout.ext_for_mime(mime) is None


def test_ext_mapping_covers_every_sniffable_image():
    """反向一致性：`sniff` 能认出的每种图片格式都必须有扩展名。

    少了这一条，一个新格式（比如 avif）在 `sniff` 里加了、在 `layout` 里忘了加，
    表现是「这种图永远存不下来」而日志里只说「不支持的图片格式」。
    """
    from kb.media import sniff

    for mime in (
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/avif",
    ):
        assert mime in layout.MIME_EXT, f"{mime} 能判定但没有扩展名映射"
        assert sniff.mime_matches_role(mime, "cover"), f"{mime} 过不了 cover 的角色判定"


# ── 模块 import 不得依赖环境 ─────────────────────────────────


def test_import_kb_media_不依赖HOME环境变量():
    """**真机抓到的 bug 的回归用例。**

    原来 `config.py` 顶层写着 `DEFAULT_MEDIA_ROOT = Path.home() / "media"`。
    于是 `import kb.media` 会去读 HOME；而在 HOME/USERPROFILE 都不可用的环境里
    `Path.home()` 抛 `RuntimeError("Could not determine home directory.")`。

    这不是理论风险：`tests/test_ai_e2e_pg.py` 的 `B27` 用例为了让子进程落到
    干净环境只传了几个变量，于是 `python -m kb.worker` 在 **import 阶段**就崩了，
    而那条用例要验的是「缺 httpx 时给友好提示」——被一个不相干的崩溃抢先。

    修法是路径解析放进 `media_root_from_env()`，只在真正要用的那一刻做。
    这条用例用子进程把 HOME 相关变量全清掉，验 import 仍然成功。
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    env = {
        "PYTHONPATH": str(root),
        "PYTHONIOENCODING": "utf-8",
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        # 刻意不传 HOME / USERPROFILE / HOMEDRIVE / HOMEPATH
    }
    result = subprocess.run(
        [sys.executable, "-c", "import kb.media; print('ok')"],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, (
        f"import kb.media 在缺 HOME 的环境里崩了：{result.stderr or result.stdout}"
    )


def test_media_root_from_env优先用环境变量(monkeypatch):
    from kb.media.config import media_root_from_env

    monkeypatch.setenv("KB_MEDIA_ROOT", "/tmp/kb-media-test")
    assert media_root_from_env().as_posix() == "/tmp/kb-media-test"


def test_media_root_from_env缺HOME时有兜底(monkeypatch):
    """`Path.home()` 抛 `RuntimeError` 时退回工作目录下的 `media`。

    一个能跑但位置可能不对的目录，好过让进程起不来——而位置不对这件事
    由启动日志里那行「媒体目录：…」暴露出来。
    """
    from pathlib import Path as _Path

    from kb.media import config

    monkeypatch.delenv("KB_MEDIA_ROOT", raising=False)

    def _boom() -> _Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(_Path, "home", classmethod(lambda cls: _boom()))
    root = config.media_root_from_env()
    assert root.name == "media"

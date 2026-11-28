"""字节头判 mime、伪造 Content-Type、SVG 拒绝（纯逻辑）。

**这一组守三条边界：**

1. **不盲信 `Content-Type`**：它由第三方 CDN 给。一个 `image/jpeg` 头 + HTML
   字节的响应，若采信 header 就会把 HTML 当图存进库、以我们的源站身份返回。
2. **`image/svg+xml` 必须拒**：它满足 `attachments_mime_allowed` 的 `image/`
   前缀、能过 `CHECK`，而 SVG 可以内联 `<script>`——同源下的 XSS 入口。
   而这个知识库里没有任何封面需要是矢量图。
3. **判不出来就拒绝**：不存来历不明的 blob。

**为什么第 2 步（采信 header）不校验字节头**：WebP/AVIF 的魔数在偏移 8，
强制校验会把一张正常的 webp 误拒；而「说它是 jpeg 与它真是 jpeg 不一致」
在业务上无后果（扩展名与 `Content-Type` 都来自这个判定，两者始终自洽）。
字节头判定的真正职责是接住「header 缺失或不可信」那一半。
"""

from __future__ import annotations

import pytest

from kb.media import sniff

# 最小可识别前缀。判定只看头部，不要求完整文件。
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
GIF87 = b"GIF87a" + b"\x00" * 32
GIF89 = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 32
MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 32
HTML = b"<!DOCTYPE html><html><body>error</body></html>"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


# ── 响应头可采信的那一半 ──────────────────────────────────────


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("image/jpeg", "image/jpeg"),
        ("image/png", "image/png"),
        ("image/webp", "image/webp"),
        ("image/avif", "image/avif"),
        ("image/jpeg; charset=binary", "image/jpeg"),
        ("IMAGE/JPEG", "image/jpeg"),
        ("  image/png  ", "image/png"),
        ("video/mp4", "video/mp4"),
    ],
)
def test_白名单内响应头被采信(header: str, expected: str) -> None:
    """依据必须是 `header`：这条让我们知道判定走了哪条路。"""
    result = sniff.sniff_mime(JPEG, header)
    assert result is not None
    assert result == (expected, "header")


def test_charset参数被剥掉():
    """`image/jpeg; charset=binary` 若不剥参数，`ext_for_mime` 查不到 →
    一张正常的 jpeg 会被判成「不支持的格式」。"""
    result = sniff.sniff_mime(JPEG, "image/jpeg;charset=UTF-8")
    assert result == ("image/jpeg", "header")


# ── 响应头不可信的那一半：查字节头 ───────────────────────────


@pytest.mark.parametrize(
    ("data", "header", "expected"),
    [
        # octet-stream 是最常见的「我不知道这是什么」声明
        (JPEG, "application/octet-stream", "image/jpeg"),
        (PNG, "application/octet-stream", "image/png"),
        (GIF87, "application/octet-stream", "image/gif"),
        (GIF89, "application/octet-stream", "image/gif"),
        (WEBP, "application/octet-stream", "image/webp"),
        (MP4, "application/octet-stream", "video/mp4"),
        # 缺头
        (JPEG, "", "image/jpeg"),
        (PNG, "", "image/png"),
        # 非白名单的 header（text/html 是 CDN 返回错误页时的典型值）
        (JPEG, "text/html", "image/jpeg"),
        (PNG, "text/html", "image/png"),
        # 完全乱写的 header
        (JPEG, "application/x-unknown", "image/jpeg"),
    ],
)
def test_不可信的响应头改用字节头判定(data: bytes, header: str, expected: str) -> None:
    result = sniff.sniff_mime(data, header)
    assert result is not None
    assert result == (expected, "magic"), f"没走字节头判定：{result}"


def test_webp必须按偏移8判():
    """`RIFF....WEBP` —— 只看开头 4 字节的 `RIFF` 会把所有 RIFF 容器
    （wav、avi）都当成 webp。"""
    assert sniff.sniff_mime(WEBP, "") == ("image/webp", "magic")
    # RIFF 但不是 WEBP：不该被判成图
    assert sniff.sniff_mime(b"RIFF\x00\x00\x00\x00WAVEfmt ", "") is None


def test_mp4必须按偏移4判():
    """`....ftyp` —— ftyp 在偏移 4，不在开头。"""
    assert sniff.sniff_mime(MP4, "") == ("video/mp4", "magic")


# ── 判不出来就拒绝 ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("data", "header"),
    [
        (HTML, "text/html"),
        (HTML, ""),
        (b"", ""),
        (b"\x00\x01\x02\x03", "application/octet-stream"),
        (b"not an image at all, just text", ""),
        (SVG, "application/octet-stream"),
    ],
)
def test_判不出来返回None(data: bytes, header: str) -> None:
    """`None` 让调用方记 `skipped`。**不给**「猜一个 application/octet-stream」
    的退路——那等于把来历不明的字节存进库。
    """
    assert sniff.sniff_mime(data, header) is None


def test_签名URL失效后CDN返回的HTML错误页被拒():
    """这是真实发生过的形态：签名的 URL 过期后 CDN 返回一个 HTML 错误页。

    若采信 `Content-Type: text/html` 后仍存进库，`/api/media` 会以我们的
    源站身份返回一段第三方 HTML——那是 XSS 入口，而不只是「一张坏图」。
    """
    error_page = (
        b"<html><head><title>403 Forbidden</title></head>"
        b"<body><h1>403 Forbidden</h1></body></html>"
    )
    assert sniff.sniff_mime(error_page, "text/html") is None


# ── SVG 必须拒（唯一的漏洞面）─────────────────────────────────


def test_svg的header被拒():
    """`image/svg+xml` 满足 `image/` 前缀，能过 `attachments_mime_allowed`
    的 `CHECK`。若采信它，同源下就开了一个 XSS 入口。"""
    assert sniff.sniff_mime(SVG, "image/svg+xml") is None


def test_svg即使字节头像xml也被拒():
    """header 不可信 + 字节是 svg 文本 → 两条路都判不出图片，返回 `None`。"""
    assert sniff.sniff_mime(SVG, "application/octet-stream") is None


def test_forbidden_set包含svg():
    """这一条是**规格说明**：禁用集里必须有 svg。

    只断言集合内容，不重复断言判定行为——`sniff_mime` 是否真的查了这张表，
    由上面的 `test_svg的header被拒` 覆盖。两件事分开，改实现时才知道
    「改的是数据还是逻辑」。
    """
    assert "image/svg+xml" in sniff.FORBIDDEN_MIMES
    for mime in sniff.FORBIDDEN_MIMES:
        # 禁用集里的每一项都必须本来能过前缀白名单——否则它就不需要被禁
        assert any(mime.startswith(prefix) for prefix in sniff.ALLOWED_PREFIXES), (
            f"{mime} 过不了前缀白名单，放进禁用集是多余项"
        )


# ── mime 与 role 的匹配 ──────────────────────────────────────


@pytest.mark.parametrize(
    ("mime", "role", "expected"),
    [
        ("image/jpeg", "cover", True),
        ("image/png", "cover", True),
        ("image/webp", "image", True),
        ("image/gif", "image", True),
        # 封面拿到视频：上游把视频地址塞进了封面字段
        ("video/mp4", "cover", False),
        ("video/mp4", "image", False),
        ("video/mp4", "video", True),
        ("image/jpeg", "video", False),
        ("text/html", "cover", False),
        ("application/pdf", "cover", False),
    ],
)
def test_mime与role匹配(mime: str, role: str, expected: bool) -> None:
    assert sniff.mime_matches_role(mime, role) is expected


def test_未登记的role一律不匹配():
    """让「有人加了一个新 role 但忘了配前缀」表现为一条 skipped 记录，
    而不是悄悄存进去一条角色错乱的附件。"""
    for role in ("", "thumbnail", "COVER", "unknown"):
        assert not sniff.mime_matches_role("image/jpeg", role)


# ── 顶层不 import 外部库 ─────────────────────────────────────


def test_sniff顶层不import外部库():
    """纯逻辑单测不该被迫装任何外部库。用 AST 判，不信 `import` 那个词。"""
    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "kb" / "media" / "sniff.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    top: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            top.append(node.module)
    assert top == ["__future__", "typing"], f"顶层多了外部依赖：{top}"

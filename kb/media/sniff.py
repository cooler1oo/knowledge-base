"""从字节头判 mime，以及「mime 与 role 是否匹配」。**纯函数。找不到任何外部库。**

## 为什么不能只信 `Content-Type`

`Content-Type` 由第三方 CDN 给，不可信；`attachments_mime_allowed` 的 `CHECK`
只做**前缀**白名单，挡不住「`image/svg+xml` 里塞脚本」。

| 情况 | 处理 |
|---|---|
| 响应头是白名单内的 `image/*` 且不在禁用集 | 采信，依据 `header` |
| 响应头是 `application/octet-stream` / 空 / 非白名单 | 用字节头判 |
| 判不出来 | **拒绝**。不存来历不明的 blob |
| 与 role 不符（`cover` 拿到 `video/*`） | 拒绝 |

## 为什么 `image/svg+xml` 必须拒

它满足 `attachments_mime_allowed` 的 `image/` 前缀，能入库。而 SVG 可以内联
`<script>`，以我们的源站身份 `inline` 返回它，等于在同源下开一个 XSS 入口
——而这个知识库里没有任何封面需要是矢量图。拒绝发生在**下载阶段**，
早于入库，所以 `CHECK` 永远不会见到它。

## 为什么字节头判定要自己写

`python-magic` 要系统装 libmagic（proot 容器多一个系统依赖），`filetype`
只按魔数判、不处理「响应头可采信但不完整」这个优先级。本轮只需要 5 种图片
格式 + mp4，且必须与 `attachments.mime_allowed` 的前缀逐一对齐，60 行更可控。
"""

from __future__ import annotations

from typing import Final

#: 与 `attachments_mime_allowed` 的 `CHECK` 对齐。图片外的几种本轮不产出，
#: 但判定表要能说出「这是什么」——否则一张误下到手的 mp4 会被当成「mime 无法判定」，
#: 而真实原因（上游把视频地址给了封面字段）就查不出来。
ALLOWED_PREFIXES: Final[tuple[str, ...]] = (
    "image/",
    "video/",
    "audio/",
    "text/",
    "application/pdf",
    "application/zip",
    "application/msword",
    "application/vnd.",
    "application/octet-stream",
)

#: 前缀合法但内容可执行。判定时直接拒，不走白名单。
FORBIDDEN_MIMES: Final[frozenset[str]] = frozenset(
    {
        "image/svg+xml",
        # SVG 的 XML 载体，同样能内联脚本
        "image/svg",
    }
)

#: **响应头可采信的前缀**。与 `ALLOWED_PREFIXES` 是两件事，不能合并：
#:
#: `ALLOWED_PREFIXES` 是「库里允许存什么」（与 `attachments_mime_allowed`
#: 的 `CHECK` 对齐）；这里回答的是「响应头这句话能不能当真」。
#:
#: 只有**声明了具体格式**的头能当真：`image/jpeg` 说了是什么。
#: 而下面这两类必须转去查字节头：
#:
#: - `application/octet-stream`：字面意思是「我不知道这是什么」
#: - `text/html` / `application/pdf` / `application/zip`：说了是什么，
#:   但**不是我们的图片**。若采信它们，一张「403 错误页」会被当成可存的 blob，
#:   `/api/media` 就以我们的源站身份返回了第三方 HTML——那是 XSS 入口，
#:   不只是「一张坏图」
#:
#: 合并两者时实测踩到：`application/octet-stream` 在白名单里，
#: 于是「把字节头判定这一整条路绕过」，一个 HTML 错误页被当正常响应存下来。
_TRUSTED_PREFIXES: Final[tuple[str, ...]] = ("image/", "video/", "audio/")

#: role → 允许的 mime 前缀。`cover` / `image` 只接受图片（设计 §7.3）。
_ROLE_PREFIX: Final[dict[str, tuple[str, ...]]] = {
    "cover": ("image/",),
    "image": ("image/",),
    "video": ("video/",),
    "snapshot": ("image/",),
}

# 各格式的字节头。判据只认前缀，不看长度字段——我们要的是「能不能当图存」，
# 不是「这张图有多大」。
_MAGIC: Final[tuple[tuple[bytes, str], ...]] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _strip_params(header_value: str) -> str:
    """剥掉 `;charset=utf-8` 之类的参数并归一大小写。"""
    return header_value.split(";", 1)[0].strip().lower()


def _is_allowed(mime: str) -> bool:
    """这个 mime 能不能入库。前缀白名单 + 禁用集，两件事都在这里。"""
    if mime in FORBIDDEN_MIMES:
        return False
    return any(mime.startswith(prefix) for prefix in ALLOWED_PREFIXES)


def _is_trusted(mime: str) -> bool:
    """响应头这句话能不能当真。见 `_TRUSTED_PREFIXES` 的注释。"""
    if mime in FORBIDDEN_MIMES:
        return False
    return any(mime.startswith(prefix) for prefix in _TRUSTED_PREFIXES)


def sniff_mime(data: bytes, header_value: str) -> tuple[str, str] | None:
    """判定 mime。返回 `(mime, 依据)`，依据是 `'header'` 或 `'magic'`。

    顺序固定（写反会让「伪造 Content-Type 的 HTML 页面」被当成图入库）：

    1. 剥响应头的 `;charset=...`
    2. 是**声明了具体格式**的白名单 mime（`image/*` / `video/*` / `audio/*`）
       且不被禁 → 采信 `header`
    3. 其余（`octet-stream` / 空 / `text/html` / 不在白名单）→ 用字节头判
    4. 字节头也判不出 → `None`，调用方拒绝

    ## 第 2 步为什么不校验字节头

    WebP/AVIF 的魔数在偏移 8，强制校验会把一张正常的 webp 误拒。而
    「header 说 jpeg 与字节真是 jpeg 不一致」在业务上无后果：扩展名与
    `Content-Type` 都来自这个判定，两者始终自洽。

    字节头判定的真正职责是接住「header 缺失、模糊或不可信」那一半
    ——包括**签名 URL 失效后 CDN 返回的 HTML 错误页**（`text/html`）。

    ## 第 3 步为什么包含 `text/html`

    `text/html` **在** `attachments_mime_allowed` 的白名单里（`text/` 前缀），
    所以「白名单内的 image/*」这个条件不够——`text/html` 不是图，但它是合法
    可存类型。判定看的是「能不能当这张图用」，不是「库里能不能存这种 mime」。
    这也是 `_TRUSTED_PREFIXES` 与 `ALLOWED_PREFIXES` 必须分开的第二个理由。
    """
    header = _strip_params(header_value)
    if header and _is_trusted(header):
        return header, "header"

    detected = _sniff_magic(data)
    if detected is not None and _is_allowed(detected):
        return detected, "magic"
    return None


def _sniff_magic(data: bytes) -> str | None:
    """按字节头认格式。认不出返回 `None`。

    WebP 是 `RIFF....WEBP`（类型在偏移 8），mp4 是偏移 4 处 `ftyp`
    ——这两种必须按偏移判，只看开头 4 字节会同时漏掉它们。
    """
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        # 品牌码（isom / mp42 / avc1 …）不影响「这是视频容器」这个结论
        return "video/mp4"
    return None


def mime_matches_role(mime: str, role: str) -> bool:
    """`cover`/`image` 只接受 `image/*`；`video` 只接受 `video/*`。

    未登记的 role 一律返回 `False`：让「有人加了一个新 role 但忘了配前缀」
    表现为一条 skipped 记录，而不是悄悄存进去一条角色错乱的附件。
    """
    allowed = _ROLE_PREFIX.get(role)
    if allowed is None:
        return False
    return any(mime.startswith(prefix) for prefix in allowed)

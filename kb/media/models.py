"""媒体层的值对象。全部 `frozen=True`，无行为。

**为什么全 frozen**：这些值会穿过 `plan → persist → store → api` 四处，
中途被谁改过就说不清了。冻结之后「`StoredBlob.storage_key` 就是落库时的那个」
在类型层面成立，不需要靠读代码确认。

**为什么 `PinnedTarget` 也在本文件**：它由 `guard.check_url` 产出、被
`fetch.HttpxDownloader` 消费。放在 `guard.py` 会让 `fetch.py` 反向 import
`guard`（只为拿一个 dataclass 的类型），而两者是并列的装配层，没有主从关系。
放进值对象文件后依赖方向变成 `guard → models ← fetch`，没有环。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MediaSpec:
    """一条待下载的媒体。由 `plan.plan_attachments` 产出。

    `role` 只可能是 `'cover'` 或 `'image'`——`'video'` 本轮不产出（PRD M4-07
    把视频原文件定为 P2「按需」）。
    """

    role: str
    url: str
    sort_order: int


@dataclass(frozen=True)
class PinnedTarget:
    """「校验过的那个 IP」与「连接的那个 IP」是同一个的构造结果。

    三个字段缺一不可：
    - `connect_url`：把主机名换成 IP 字面量后的 URL（`httpx` 实际连这个）
    - `host_header`：原始主机名，补在 `Host` 头上（否则 CDN 会 404）
    - `sni_hostname`：TLS 握手用原始主机名（否则证书校验失败）

    只把 URL 换成 IP 而不补这两个头，现象是「所有封面都 403/证书错误」。
    """

    connect_url: str
    host_header: str
    sni_hostname: str


@dataclass(frozen=True)
class DownloadedBlob:
    """下载完成、尚未写盘的字节。

    `mime` 是**判定后**的值，不是响应头原值；`ext` 由 mime 决定，不取上游
    `ext` 也不取 URL 后缀（`evil.html` 这类名字在静态托管后面是真漏洞）。
    """

    role: str
    url: str
    sort_order: int
    data: bytes
    mime: str
    ext: str


@dataclass(frozen=True)
class StoredBlob:
    """写盘完成、等待落库的最终形态。字段与 `attachments` 列一一对应。

    `duration_sec` 本轮恒为 `None`（不下视频）。保留字段是为了将来视频落盘时
    `replace_for_item` 的签名不用改。
    """

    role: str
    storage_key: str
    mime: str
    size_bytes: int
    width: int | None
    height: int | None
    duration_sec: int | None
    sha256: str
    sort_order: int


@dataclass(frozen=True)
class SkippedMedia:
    """一条媒体没存成。

    `reason` 是给人看的短句，会被汇总进 `items.props.mediaError`。
    **不写进 `jobs.error`**：那会让条目显示成「处理失败」，而封面没下下来
    不影响这条内容可读（设计 §5.2）。
    """

    role: str
    url: str
    reason: str


@dataclass(frozen=True)
class MediaResult:
    """媒体阶段的完整结果。`stored` 交给 `attachments.replace_for_item`，
    `skipped` 只用来生成一句 `props.mediaError`。"""

    stored: tuple[StoredBlob, ...]
    skipped: tuple[SkippedMedia, ...]

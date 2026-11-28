"""唯一真正发网络请求的地方。`httpx` 在方法体内 import。

## 为什么单独成文件

这是整个媒体层唯一的出网点。把它拆出来，`persist.py` 就能用假下载器毫秒级跑完
单测，而「`trust_env=False`」「不跟随重定向」「按块读守两个上限」这三处装配
只在这里出现一次——三处都在真机上踩过，分散写必然漏一处。

## 两条装配口径

1. **`trust_env=False`**：容器里残留 `HTTP_PROXY`/`HTTPS_PROXY` 会让请求走到
   不存在的地址然后卡死。这一行是「仍然直连」的唯一保证（`kb/ai/transport.py`
   的 `B21` 同一个教训）。
2. **`follow_redirects=False`**：一个 `302 → http://127.0.0.1:8000/` 能绕过
   SSRF 判定的全部三层（那三层只校验了第一个 URL）。CDN 的图片地址是直链，
   不需要跳转，所以「不跟随」不牺牲功能。

## 两个上限必须边读边判

`max_bytes` 与 `deadline` 都不看 `Content-Length`：服务端可以撒一个 100 字节的
Content-Length 然后吐 1 GB；也可以每 9 秒吐一个字节，让 `read` 超时永不触发。
所以两个守卫共用**一个读循环**——它们是同一个读操作的两个上限，分两处写
必然有一套先写出来、后一套被忘掉（`kb/ai/transport.py` 的 `B23`/`B29`）。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Final, Protocol

from . import config
from .models import PinnedTarget

_READ_CHUNK: Final[int] = 8192

#: 响应头里我们要找的那一个。用常量而不是字面量：`headers.get` 的大小写
#: 在 httpx 里已归一，但假实现（dict）未必，所以取值时统一转小写比较。
_CONTENT_TYPE: Final[str] = "content-type"


class DownloadError(Exception):
    """下载失败。**不是编程错误**，调用方收成 `SkippedMedia` 继续下一条。

    为什么是异常而不是返回值：这三种失败根本没有响应体可看（超体积时字节被丢掉、
    超时时连接被中断），而 3xx/4xx/5xx 有响应。混成一个返回值会让
    `status=0` 这种哨兵值出现在类型里，每个调用点都必须记住它意味着什么。
    """


class DownloadTooLarge(DownloadError):
    """读到的字节超过 `max_bytes`。**不保留截断的字节**：半张图比没有更糟。"""


class DownloadTimeout(DownloadError):
    """总耗时超过 `deadline`。慢速滴流（每 9 秒 1 字节）只能靠这个拦住。"""


class Downloader(Protocol):
    """测试与生产共用的一个方法。假下载器照这个形状写。

    `deadline` 是**绝对时刻**（与 `clock()` 同一基准），不是相对秒数：
    传相对秒数会让调用方每次都要自己加一次，而「加在哪一刻」决定了上限是否准确。
    """

    def get(
        self, target: PinnedTarget, *, max_bytes: int, deadline: float
    ) -> tuple[int, dict[str, str], bytes]: ...


class HttpxDownloader:
    """`httpx` 的装配。重试与结果分类都不在这里（重试属 `jobs.attempts`，
    分类属 `persist`）。"""

    def __init__(
        self,
        *,
        client_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """`client_factory` 与 `clock` 都是普通构造参数，不 monkeypatch：
        前者让本文件的用例完全不联网，后者让「总时长守卫」的用例毫秒级跑完。

        `client_factory` 延迟到 `get` 里才取默认值（不能在这里 `import httpx`）。
        """
        self._client_factory = client_factory
        self._clock = clock

    def _factory(self) -> Callable[..., Any]:
        if self._client_factory is not None:
            return self._client_factory
        import httpx

        return httpx.Client

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        return time.monotonic()

    def get(
        self, target: PinnedTarget, *, max_bytes: int, deadline: float
    ) -> tuple[int, dict[str, str], bytes]:
        """发一次 GET，按块读，超字节或超时即抛。

        连接的是 `target.connect_url`（IP 字面量），同时带 `Host` 头与
        `sni_hostname`。三者缺一不可：只换 URL 不补头，现象是「所有封面 403
        或证书错误」。

        `httpx` 的 `extensions={"sni_hostname": ...}` 会透传到 httpcore 的 TLS
        握手参数（0.28 实测），所以「连 IP、验证真域名证书」两者同时成立，
        不需要关掉证书校验。
        """
        import httpx

        if max_bytes <= 0:
            raise ValueError("max_bytes 必须是正数")

        factory = self._factory()
        client = factory(
            # 容器里残留代理变量会让请求走到不存在的地址然后卡死
            trust_env=False,
            timeout=httpx.Timeout(
                connect=config.CONNECT_SEC,
                read=config.READ_SEC,
                write=config.CONNECT_SEC,
                pool=config.CONNECT_SEC,
            ),
            # 一个 302 → 127.0.0.1 能绕过 SSRF 判定的全部三层
            follow_redirects=False,
        )
        try:
            return self._read(client, target, max_bytes, deadline)
        finally:
            client.close()

    def _read(
        self,
        client: Any,
        target: PinnedTarget,
        max_bytes: int,
        deadline: float,
    ) -> tuple[int, dict[str, str], bytes]:
        """按块读，两个守卫共用一次循环。

        `iter_bytes()` 返回的块大小不保证等于 `_READ_CHUNK`（httpx 内部按
        transport 的投递粒度给），所以累计判定的粒度按实际块长算，不假设块长。
        """
        import httpx

        headers = {"Host": target.host_header}
        collected: list[bytes] = []
        total = 0

        try:
            with client.stream(
                "GET",
                target.connect_url,
                headers=headers,
                extensions={"sni_hostname": target.sni_hostname},
            ) as response:
                for chunk in response.iter_bytes(_READ_CHUNK):
                    if self._now() > deadline:
                        raise DownloadTimeout(
                            f"总耗时超过上限（{config.REQUEST_TOTAL_SEC:.0f} 秒）"
                        )
                    total += len(chunk)
                    if total > max_bytes:
                        # 不保留截断字节：半张图会被浏览器当正常图渲染成灰块，
                        # 比 404 更难查
                        raise DownloadTooLarge(
                            f"响应超过 {max_bytes} 字节上限（已读 {total}）"
                        )
                    collected.append(chunk)

                return (
                    int(response.status_code),
                    {str(k).lower(): str(v) for k, v in response.headers.items()},
                    b"".join(collected),
                )
        except httpx.TimeoutException as exc:
            raise DownloadTimeout(f"超时：{exc}") from exc
        except httpx.HTTPError as exc:
            # 不把 httpx 的异常类型漏到上层：让 persist 认识 httpx 的异常，
            # 等于把传输实现的细节漏出本模块
            raise DownloadError(f"{type(exc).__name__}: {exc}") from exc


def content_type_of(headers: dict[str, str]) -> str:
    """从响应头取 Content-Type。缺头返回空串（`sniff` 会转去查字节头）。"""
    for key, value in headers.items():
        if key.lower() == _CONTENT_TYPE:
            return value
    return ""

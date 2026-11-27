"""传输层：唯一真正发网络请求的那一小块。

**为什么必须单独成文件**（设计 §3.1 / §6.1）：这是测试的核心接缝，也是
三处「装配逻辑」的唯一落点——`trust_env=False`（`B21`）、总超时守卫（`B23`）、
256KB 截断（`B29`）。把整层打桩会让这三处一起被换掉，用例变成自证。

**两个设计约束，都基于实测事实：**

1. **`httpx.Timeout` 没有 `total` 字段。** 实测 0.28.1 只有
   `connect` / `read` / `write` / `pool`。`read` 是「单次读操作等多久」，
   服务端每 9 秒吐一个字节就能让一次请求无限延长而不触发任何超时。
   所以「总 20 秒」由 `post()` 里的按块读循环自己计时——与 `B29` 的截断
   共用同一个循环：一次循环同时管住「读太多」与「读太久」，不需要线程或信号。
2. **不能用 `signal.alarm` 或线程 `join(timeout)` 兜。** `SIGALRM` 会打断正在
   进行的数据库操作且 Windows 上没有；超时后线程仍在跑（Python 无法强杀），
   等于把「卡死」换成「泄漏」。

**`client_factory` 与 `clock` 都是可注入的**：前者让本文件的测试完全不联网，
后者让「总 20 秒守卫」的用例毫秒级跑完。两者都是普通构造参数，不 monkeypatch。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Final, Protocol

import httpx

from .config import AiConfig

#: `Retry-After` 头。只认秒数（`§9` 第 9 条）：按 HTTP 日期解析要多 15 行且
#: 依赖本地时钟正确，认不出时退回 `mark_failed` 的 1/2 分钟退避，行为仍确定。
RETRY_AFTER_HEADER: Final[str] = "retry-after"

_READ_CHUNK: Final[int] = 8192


@dataclass(frozen=True)
class TransportResponse:
    """一次 HTTP 交换的结果。**非 200 也是一条正常结果，不是异常。**

    为什么不在传输层判死 4xx/5xx：解析层要看到响应体才能给出「带前 200 字」的
    错误（`B1`/`B28`）。在传输层抛异常会把 HTML 网关页丢在门外，
    错误消息里就只剩「HTTP 502」，排障时看不到网关说了什么。
    """

    status: int
    content_type: str
    body: str
    truncated: bool = False
    retry_after_sec: float = 0.0


class TransportError(Exception):
    """网络层失败。`kind` 只有四个值，与 `FailureCategory` 一一对应。

    **为什么是异常而不是返回值**：网络失败时根本没有 HTTP 响应可看，而
    4xx/5xx 有响应。两者混成一个返回值会让 `status=0` 这种哨兵值出现在类型里，
    每个调用点都必须记住它意味着什么。异常让「没有响应」这件事在类型上显式。

    **为什么继承 `Exception` 而不是裸 dataclass**：调用方要用
    `except TransportError` 拦它。裸对象只能靠 `isinstance` 判断返回值，
    而这条路径上层是「直接 `response = transport.post(...)`」的写法，
    没有返回值可判。

    字段用普通属性而非 dataclass：异常需要可变的 `args` 参与 `str()`，
    dataclass 的 `__eq__`/`__repr__` 与 `Exception.args` 相互干扰。
    """

    def __init__(
        self, *, kind: str, message: str, retry_after_sec: float = 0.0
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after_sec = retry_after_sec


class Transport(Protocol):
    """测试与生产共用的一个方法。`ScriptedTransport` 照着这个形状写。"""

    def post(
        self,
        url: str,
        headers: dict[str, str],
        body: str,
        config: AiConfig,
    ) -> TransportResponse:  # pragma: no cover - 协议无实现
        ...


def parse_retry_after(headers: dict[str, str]) -> float:
    """从响应头取 `Retry-After` 秒数。认不出（缺头 / 非数字 / 负数）时返回 0.0。

    返回 0.0 而不是 `None`：调用方只需要「有值就按它退避、没有就按指数退避」
    这一种判断，`0.0` 天然充当「没有」。用 `None` 会让每个调用点都写一次
    空值判断。
    """
    raw = ""
    for key, value in headers.items():
        if key.lower() == RETRY_AFTER_HEADER:
            raw = value.strip()
            break
    if not raw:
        return 0.0
    try:
        seconds = float(raw)
    except ValueError:
        # HTTP 日期字符串（`Wed, 21 Oct 2026 07:28:00 GMT`）走这里
        return 0.0
    return seconds if seconds > 0 else 0.0


class HttpxTransport:
    """`httpx` 的装配。两件事不在这里做：重试（属 `jobs.attempts`）、
    结果分类（属 `AiClient`）。
    """

    def __init__(
        self,
        *,
        client_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._client_factory: Callable[..., Any] = client_factory or httpx.Client
        self._clock = clock or time.monotonic

    def post(
        self,
        url: str,
        headers: dict[str, str],
        body: str,
        config: AiConfig,
    ) -> TransportResponse:
        """发一次 POST，按块读响应，超 256KB 或超总时长即停。

        四类异常各翻成一个 `kind`，**都不往外抛原始 `httpx` 异常**：让上层
        认识 `httpx` 的异常类型，等于把传输实现的细节漏到 worker。
        """
        client = self._client_factory(
            # B21：容器里残留 HTTP_PROXY/HTTPS_PROXY 会让请求走到不存在的地址
            # 然后卡死。这一行是「仍然直连」的唯一保证。
            trust_env=False,
            # 连接 5 / 读 10。总 20 秒不在这里（httpx 没有 total 字段），
            # 由下面的读循环自己计时。
            timeout=httpx.Timeout(
                connect=config.timeout_connect,
                read=config.timeout_read,
                write=config.timeout_connect,
                pool=config.timeout_connect,
            ),
            follow_redirects=False,
        )
        try:
            return self._read_response(client, url, headers, body, config)
        except TransportError:
            raise
        except httpx.ConnectTimeout as exc:
            # 「连不上」不是「连上了没数据」。两者落地动作不同：连接类退避
            # 3 分钟（避免每 1 分钟打一次 DNS），读超时走 1/2 分钟指数退避。
            raise TransportError(kind="connect", message=f"建连超时：{exc}") from exc
        except httpx.ConnectError as exc:
            raise TransportError(kind="connect", message=f"连接失败：{exc}") from exc
        except httpx.ReadTimeout as exc:
            raise TransportError(kind="timeout", message=f"读超时：{exc}") from exc
        except httpx.HTTPError as exc:
            raise TransportError(
                kind="unexpected", message=f"{type(exc).__name__}: {exc}"
            ) from exc
        finally:
            client.close()

    def _read_response(
        self,
        client: Any,
        url: str,
        headers: dict[str, str],
        body: str,
        config: AiConfig,
    ) -> TransportResponse:
        """按块读，边读边判「读太多」与「读太久」。

        两个守卫共用一次循环的理由：它们是同一个读操作的两个上限。分成两处
        就会有两套循环、两次计时，而其中一套必然先被写出来、后一套被忘掉。
        """
        started = self._clock()
        collected: list[bytes] = []
        total = 0
        truncated = False

        with client.stream("POST", url, headers=headers, content=body) as response:
            for chunk in response.iter_bytes():
                # 总时长守卫：慢速滴流（每 9 秒 1 字节）下 read 超时永远不触发
                if self._clock() - started > config.timeout_total:
                    raise TransportError(
                        kind="timeout",
                        message=f"总耗时超过 {config.timeout_total:.0f} 秒上限",
                    )
                total += len(chunk)
                if total > config.max_response_bytes:
                    # 只保留到上限为止：后面的块既不取用也不进内存（B29）
                    allowed = config.max_response_bytes - (total - len(chunk))
                    if allowed > 0:
                        collected.append(chunk[:allowed])
                    truncated = True
                    break
                collected.append(chunk)

            return TransportResponse(
                status=int(response.status_code),
                content_type=response.headers.get("content-type", ""),
                body=_decode(b"".join(collected), truncated=truncated),
                truncated=truncated,
                retry_after_sec=parse_retry_after(dict(response.headers)),
            )


def _decode(raw: bytes, *, truncated: bool) -> str:
    """字节 → 文本。**只对截断路径丢弃不完整的尾部字节。**

    ## 为什么不能一律 `errors="replace"`

    `max_response_bytes` 是 262144，它**不是 UTF-8 字符宽度的整数倍**。
    响应里有多字节字符（中文 3 字节、emoji 4 字节）时，截断点必然可能落在
    字符中间：

    ```
    用 999 字节的中文块：第 263 块越界，allowed = 406，406 % 3 = 1
    ├─ 前 405 字节是 135 个完整汉字
    └─ 第 406 字节是某个汉字的第一个字节
    ```

    `errors="replace"` 会把那半个字节变成 `U+FFFD`（替换字符），于是
    `jobs.error` 里拼进去的「响应前 200 字」末尾多一个 ``。
    虽然 `truncated=True` 时响应直接判 `TOO_LARGE` 失败、不写 `items.summary`
    （**不污染数据**），但排障时看到的原文缺一个字会误导人。

    ## 为什么只在 `truncated=True` 时才修剪

    非截断响应出现非法 UTF-8，说明**上游给的就是坏字节**（或 `Content-Type`
    声明与实际不符）。那种情况必须保留 `errors="replace"` 让问题可见——
    悄悄修剪等于把「上游坏了」伪装成「响应正常，只是末尾少一个字」。

    换句话说：`truncated` 是**我们知道且预期的截断**，可以修剪；
    别的非法 UTF-8 是**未知故障**，必须暴露。

    ## 为什么先试整体解码再修剪

    **必须先做一次 `raw.decode("utf-8")`**。截断点恰好落在字符边界上时
    （例如全是 ASCII 的 `xxx…`，或中文长度正好整 3 字节对齐），整体解码
    本来就成功，一个字节都不该丢——先修剪会把合法字节砍掉。

    这是实测踩到的：第一版无条件从 `drop=1` 起步，结果一条「块长不整除上限」
    的用例红在 `262143 != 262144`（应为 262144，实际少 1 字节），
    因为纯 ASCII 的 `x` 也被砍了一个。

    只有整体解码失败（= 尾部有不完整字符）才进入修剪循环。

    ## 为什么回退最多 4 字节

    UTF-8 单字符最长 4 字节。截断点最多切掉最后一个字符的 1–3 个起始字节，
    所以回退 1–3 字节必能找到合法边界；循环上界写 4 留一格余量。
    找不到就走 `replace` 兜底——正常输入下到不了那里，但真到了也不该让整个
    请求抛异常（那会把「响应过大」变成「传输失败」，错误分类从 `TOO_LARGE`
    变成 `UNEXPECTED`，排障更难）。
    """
    try:
        # 快路径：整体合法就直接返回。截断点正好落在字符边界时走这里，
        # 一个字节都不会丢（纯 ASCII 响应永远走这条）。
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        if not truncated:
            # 非截断：非法 UTF-8 属上游故障，保留替换字符让问题可见
            return raw.decode("utf-8", errors="replace")

    # 走到这里 = truncated 且尾部有不完整字符
    for drop in range(1, 5):
        if drop >= len(raw):
            break
        try:
            return raw[:-drop].decode("utf-8")
        except UnicodeDecodeError:
            continue
    # 兜底：正常到不了（raw 全是坏字节的极端情况）
    return raw.decode("utf-8", errors="replace")

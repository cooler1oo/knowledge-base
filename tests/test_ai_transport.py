"""传输层：`Transport` 协议、`TransportResponse` / `TransportError`、`HttpxTransport`。

**这一层是整轮测试的核心接缝**（设计 §4.2）。它存在的理由有三条，每一条都在
下面的用例里有对应：

1. `B21` 断言「容器里有 `HTTPS_PROXY` 时请求仍直连成功」。这只有在
   `HttpxTransport` 真的被构造、`trust_env=False` 真的被传进去时才成立——
   把 `AiClient` 整体打桩，这条会变成「假货返回了成功」。
2. `B23` 断言「单次调用总耗时 ≤25 秒」。`httpx.Timeout` 只有
   `connect`/`read`/`write`/`pool` 四个字段、**没有 `total`**（实测 0.28.1），
   服务端每 9 秒吐一个字节就能让一次请求无限延长。总超时只能由这里的
   「按块读 + 累计计时」自己守。
3. `B29` 断言「超 256KB 即停读，不把整个响应读进内存」。同一条读循环里判。

**整个文件不产生任何真实网络请求。** 所有用例都通过 `client_factory` 注入一个
按剧本返回的假 httpx 客户端；时钟也注入，所以「20 秒总守卫」的用例是
毫秒级跑完的，不是真等 20 秒。
"""

from __future__ import annotations

import json

import httpx
import pytest

from kb.ai.config import AiConfig
from kb.ai.transport import (
    HttpxTransport,
    TransportError,
    TransportResponse,
    parse_retry_after,
)


def _cfg(**overrides: object) -> AiConfig:
    base: dict[str, object] = {
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-secret-value",
        "model": "gpt-4o-mini",
    }
    base.update(overrides)
    return AiConfig(**base)  # type: ignore[arg-type]


# ── 假 httpx 客户端：按剧本吐字节块 ─────────────────────────────


class _FakeStream:
    """`client.stream(...)` 的上下文管理器。形状照抄 `httpx.Response`。

    `status_code` 与 `headers` 必须一起提供：真 `httpx` 的 `stream()` 返回的
    就是 `Response` 本身，假货少了这两个属性，`HttpxTransport` 会在
    `response.status_code` 处炸——那样的红是假货契约不完整，不是实现有问题
    （`tests/test_worker_loop.py` 的 `_Extractor` 注释里记过同一个坑）。
    """

    def __init__(
        self,
        chunks: list[bytes],
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self._raises = raises
        self.status_code = status
        self.headers = headers or {}

    def __enter__(self) -> _FakeStream:
        if self._raises is not None:
            raise self._raises
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def iter_bytes(self) -> object:
        for chunk in self._chunks:
            yield chunk


class _FakeClient:
    """记下构造参数（B21 要断言 `trust_env`），并返回预置的响应。"""

    def __init__(
        self,
        *,
        status: int = 200,
        chunks: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._status = status
        self._chunks = chunks if chunks is not None else []
        self._headers = headers or {}
        self._raises = raises
        self.streamed: list[dict[str, object]] = []

    def stream(self, method: str, url: str, **kwargs: object) -> _FakeStream:
        self.streamed.append({"method": method, "url": url, **kwargs})
        return _FakeStream(
            self._chunks,
            status=self._status,
            headers=self._headers,
            raises=self._raises,
        )

    def close(self) -> None:
        return None


class _Factory:
    """`client_factory`：记下被传进来的构造参数，返回预置的假客户端。"""

    def __init__(self, client: _FakeClient) -> None:
        self._client = client
        self.kwargs: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> _FakeClient:
        self.kwargs.append(kwargs)
        return self._client


class _Clock:
    """假时钟。每次调用前进固定秒数，让「总 20 秒守卫」毫秒级可测。"""

    def __init__(self, step: float = 0.0) -> None:
        self._now = 1000.0
        self._step = step

    def __call__(self) -> float:
        current = self._now
        self._now += self._step
        return current


def _transport(
    *,
    status: int = 200,
    chunks: list[bytes] | None = None,
    headers: dict[str, str] | None = None,
    raises: Exception | None = None,
    step: float = 0.0,
) -> tuple[HttpxTransport, _Factory, _FakeClient]:
    client = _FakeClient(status=status, chunks=chunks, headers=headers, raises=raises)
    factory = _Factory(client)
    return (
        HttpxTransport(client_factory=factory, clock=_Clock(step)),
        factory,
        client,
    )


# ── B21：代理变量 ───────────────────────────────────────────────


def test_容器有代理变量时仍直连_trust_env为假_B21():
    """B21：唯一能验证这条的是「构造 httpx 客户端时 `trust_env=False`」。

    `httpx` 默认读 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`。容器里残留一个
    指向 `127.0.0.1:9` 的代理变量，请求会走到不存在的地址然后卡死超时。
    `trust_env=False` 是一行就能关掉的开关，而它只有在真的被传进去时才有用。
    """
    transport, factory, _client = _transport(chunks=[b"{}"])
    transport.post(
        "https://api.example.com/v1/chat/completions",
        headers={},
        body="{}",
        config=_cfg(),
    )
    assert factory.kwargs, "没构造 httpx 客户端"
    assert factory.kwargs[0]["trust_env"] is False, (
        f"必须显式 trust_env=False，实际构造参数：{factory.kwargs[0]}"
    )


def test_超时按配置的三级数值传下去_AI_12():
    """连接 5 秒 / 读 10 秒。总 20 秒由本层读循环守（`httpx` 没有 total 字段）。"""
    transport, factory, _client = _transport(chunks=[b"{}"])
    transport.post("https://x.invalid/v1/c", headers={}, body="{}", config=_cfg())

    timeout = factory.kwargs[0]["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 5.0
    assert timeout.read == 10.0


# ── 正常响应 ───────────────────────────────────────────────────


def test_200_响应把状态与正文带出来():
    body = json.dumps({"summary": "x" * 20}).encode()
    transport, _factory, _client = _transport(
        chunks=[body], headers={"content-type": "application/json"}
    )
    response = transport.post(
        "https://x.invalid/v1/c", headers={}, body="{}", config=_cfg()
    )
    assert isinstance(response, TransportResponse)
    assert response.status == 200
    assert response.body == body.decode()
    assert response.truncated is False
    assert response.content_type.startswith("application/json")


def test_响应体按_utf8_解码中文不乱码():
    payload = json.dumps({"summary": "这是一段中文摘要"}, ensure_ascii=False)
    transport, _factory, _client = _transport(chunks=[payload.encode("utf-8")])
    response = transport.post(
        "https://x.invalid/v1/c", headers={}, body="{}", config=_cfg()
    )
    assert "这是一段中文摘要" in response.body


def test_多个字节块按顺序拼接():
    transport, _factory, _client = _transport(chunks=[b'{"a"', b":", b"1}"])
    response = transport.post(
        "https://x.invalid/v1/c", headers={}, body="{}", config=_cfg()
    )
    assert response.body == '{"a":1}'


# ── B29：256KB 截断 ────────────────────────────────────────────


def test_响应超过256KB时截断读取并标记truncated_B29():
    """B29：**不把整个响应读进内存**。按块读，累加超限即停。

    造 300 个 1KB 块（共 300KB）。断言：① `truncated=True`；
    ② 只读了 256KB 左右就停（后续块没被取用）。
    """
    chunk = b"x" * 1024
    transport, _factory, client = _transport(chunks=[chunk] * 300)
    response = transport.post(
        "https://x.invalid/v1/c", headers={}, body="{}", config=_cfg()
    )

    assert response.truncated is True
    assert len(response.body) <= 256 * 1024
    assert len(response.body) >= 256 * 1024 - 1024, "不该远小于上限"


def test_正好256KB不算截断_B29():
    chunk = b"x" * 1024
    transport, _factory, _client = _transport(chunks=[chunk] * 256)
    response = transport.post(
        "https://x.invalid/v1/c", headers={}, body="{}", config=_cfg()
    )
    assert response.truncated is False
    assert len(response.body) == 256 * 1024


def test_块长不整除上限时保留最后一块的余量_B29():
    """B29 的盲区用例（QA 变异测试发现，2026-11-27）。

    **为什么需要这一条**：`max_response_bytes` 是 262144，正好等于
    `1024 × 256`。上面两条用例都用 1024 字节的块，第 257 块越界时
    `allowed = 262144 - 262144 = 0` **恒为 0**，于是
    `transport.py` 里 `if allowed > 0:` 那个分支**永远不执行**——
    它是死代码，把整行删掉上面两条照样绿（实测 18 条传输层用例全过）。

    缺陷不在实现（截断行为正确），在**测试只覆盖了「块长整除上限」这一种情况**。
    真实响应的分块长度由 TCP/HTTP 决定，几乎不会正好整除 262144。

    本用例用 1000 字节块：第 263 块越界时 `allowed = 144`，
    真正走到 `chunk[:allowed]`，从而能抓住「丢弃尾部余量」这类回归。
    """
    chunk = b"x" * 1000
    transport, _factory, _client = _transport(chunks=[chunk] * 400)
    response = transport.post(
        "https://x.invalid/v1/c", headers={}, body="{}", config=_cfg()
    )

    assert response.truncated is True
    # 必须**正好** 262144：少一个字节说明尾部余量被丢了，
    # 多一个字节说明越界了。
    assert len(response.body) == 262144, (
        f"截断后的长度应为 {262144}，实际 {len(response.body)}；"
        "少则尾部余量被丢，多则越界"
    )


def test_截断处不产生替换字符_B29():
    """截断点落在多字节字符中间时，`decode(errors="replace")` 会产出 U+FFFD。

    这条守的是「错误消息里的响应原文可读」：截断后 `snippet` 会拼进
    `jobs.error`，里面出现半个汉字虽然不影响数据（`truncated=True` 时
    响应直接判失败、不入库），但会让排障时看到的原文缺字。

    用 3 字节/字的中文块构造：1000 字节不是 3 的整数倍，
    必然有一块会切在字符中间。
    """
    chunk = "中".encode("utf-8") * 333  # 999 字节，不是 3 的整数倍倍数关系
    transport, _factory, _client = _transport(chunks=[chunk] * 400)
    response = transport.post(
        "https://x.invalid/v1/c", headers={}, body="{}", config=_cfg()
    )

    assert response.truncated is True
    assert "\ufffd" not in response.body, "截断处产生了替换字符，响应原文会缺字"


def test_非截断响应遇非法UTF8仍产生替换字符_B29():
    """**反向守门**：非截断时的非法 UTF-8 必须暴露，不能被悄悄修剪。

    `_decode` 对截断路径会丢弃不完整的尾部字节（让 `jobs.error` 里的原文
    不缺字）。但**这条修剪绝不能应用到非截断响应**：

    - `truncated=False` 说明我们读到了响应的**全部字节**
    - 此时出现非法 UTF-8，意味着**上游给的就是坏字节**，或 `Content-Type`
      声明与实际不符
    - 悄悄修剪等于把「上游坏了」伪装成「响应正常，只是末尾少一个字」，
      故障被藏起来

    所以判据是：`truncated` 是**我们知道且预期的**截断 → 可修剪；
    别的非法 UTF-8 是**未知故障** → 必须让 `U+FFFD` 出现在 `body` 里，
    `parse_summary` 会因 JSON 解析失败报 `BAD_RESPONSE`，排障时能看到坏字节。

    构造：4 字节 `b"\\xe4\\xb8"`（「中」的前两个字节，不完整）+ 一个完整汉字，
    总长远小于 256KB，不触发截断。
    """
    transport, _factory, _client = _transport(chunks=[b"\xe4\xb8", "中".encode()])
    response = transport.post(
        "https://x.invalid/v1/c", headers={}, body="{}", config=_cfg()
    )

    assert response.truncated is False, "这条用例构造的响应不该被截断"
    assert "\ufffd" in response.body, (
        "非截断的非法 UTF-8 被悄悄修剪了，上游故障会被藏起来"
    )


def test_截断点正好在字符边界时不丢字节_B29():
    """截断点恰好落在 UTF-8 字符边界时，**一个字节都不能丢**。

    ⚠️ 这条是修 `_decode` 时实测踩到的回归：第一版无条件从 `drop=1` 起步，
    结果纯 ASCII 响应（截断点在任意字节都是合法边界）也被砍了 1 字节，
    `test_块长不整除上限时保留最后一块的余量_B29` 红在 `262143 != 262144`。

    正确逻辑是「先试整体解码，失败才修剪」。这条用纯 ASCII 构造，
    断言长度**正好**等于上限。
    """
    chunk = b"x" * 1000
    transport, _factory, _client = _transport(chunks=[chunk] * 400)
    response = transport.post(
        "https://x.invalid/v1/c", headers={}, body="{}", config=_cfg()
    )

    assert response.truncated is True
    assert len(response.body) == 256 * 1024, (
        f"截断点本是合法边界，不该丢字节：{len(response.body)}"
    )


# ── B23：超时 ──────────────────────────────────────────────────


def test_读超时归为TIMEOUT类别且不抛异常_B23():
    """B23：`httpx.ReadTimeout` → `TransportError(kind='timeout')`。

    不抛原始 `httpx` 异常：上层按类别决定落地动作，让它认识 `httpx` 的异常
    类型等于把传输实现的细节漏到 worker。
    """
    transport, _factory, _client = _transport(raises=httpx.ReadTimeout("读超时了"))
    with pytest.raises(TransportError) as caught:
        transport.post("https://x.invalid/v1/c", headers={}, body="{}", config=_cfg())
    assert caught.value.kind == "timeout"


def test_慢速滴流时总耗时仍被20秒总守卫截断_B23():
    """B23 的硬骨头：服务端每 9 秒吐一个字节，`read` 超时永远不触发。

    假时钟每取一次前进 9 秒。3 块之后累计 27 秒 > 20 秒，必须判 TIMEOUT
    而不是继续读下去。**这条不靠真实计时**，所以毫秒级跑完。
    """
    transport, _factory, _client = _transport(chunks=[b"a", b"b", b"c", b"d"], step=9.0)
    with pytest.raises(TransportError) as caught:
        transport.post("https://x.invalid/v1/c", headers={}, body="{}", config=_cfg())
    assert caught.value.kind == "timeout"


def test_快响应不会被总守卫误伤_B23():
    """总守卫不能把正常快响应也判超时。"""
    transport, _factory, _client = _transport(chunks=[b"a", b"b"], step=0.1)
    response = transport.post(
        "https://x.invalid/v1/c", headers={}, body="{}", config=_cfg()
    )
    assert response.status == 200


# ── B22：连接失败 ──────────────────────────────────────────────


def test_连接失败归为CONNECT类别且不抛异常_B22():
    transport, _factory, _client = _transport(
        raises=httpx.ConnectError("[Errno 11001] getaddrinfo failed")
    )
    with pytest.raises(TransportError) as caught:
        transport.post("https://x.invalid/v1/c", headers={}, body="{}", config=_cfg())
    assert caught.value.kind == "connect"
    assert "getaddrinfo" in caught.value.message or "Connect" in caught.value.message


def test_建连超时归为CONNECT而不是TIMEOUT_B22():
    """`ConnectTimeout` 是「连不上」，`ReadTimeout` 是「连上了没数据」。

    两者落地动作不同：连接类退避 3 分钟（避免每 1 分钟打一次 DNS），
    读超时走 1/2 分钟指数退避。混成一个类别会让 B22 与 B23 都判不准。
    """
    transport, _factory, _client = _transport(raises=httpx.ConnectTimeout("建连超时"))
    with pytest.raises(TransportError) as caught:
        transport.post("https://x.invalid/v1/c", headers={}, body="{}", config=_cfg())
    assert caught.value.kind == "connect"


def test_其他网络异常归为unexpected():
    transport, _factory, _client = _transport(raises=httpx.HTTPError("说不清"))
    with pytest.raises(TransportError) as caught:
        transport.post("https://x.invalid/v1/c", headers={}, body="{}", config=_cfg())
    assert caught.value.kind == "unexpected"


# ── B25：Retry-After ──────────────────────────────────────────


def test_429带RetryAfter时把秒数带出来_B25():
    transport, _factory, _client = _transport(
        status=429, chunks=[b"{}"], headers={"retry-after": "60"}
    )
    response = transport.post(
        "https://x.invalid/v1/c", headers={}, body="{}", config=_cfg()
    )
    assert response.status == 429
    assert response.retry_after_sec == 60.0


def test_RetryAfter是日期字符串时当作没有_9节第9条():
    """PRD 允许两种格式，只认秒数。按日期解析要多 15 行且依赖本地时钟正确；
    认不出时退回 1/2 分钟退避，行为仍然确定。
    """
    transport, _factory, _client = _transport(
        status=429,
        chunks=[b"{}"],
        headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"},
    )
    response = transport.post(
        "https://x.invalid/v1/c", headers={}, body="{}", config=_cfg()
    )
    assert response.retry_after_sec == 0.0


def test_RetryAfter不存在时为0():
    assert parse_retry_after({}) == 0.0
    assert parse_retry_after({"retry-after": ""}) == 0.0
    assert parse_retry_after({"retry-after": "-5"}) == 0.0
    assert parse_retry_after({"retry-after": "30"}) == 30.0


# ── 请求形状 ───────────────────────────────────────────────────


def test_post用_POST_方法并带上请求体():
    transport, _factory, client = _transport(chunks=[b"{}"])
    transport.post(
        "https://x.invalid/v1/chat/completions",
        headers={"Authorization": "Bearer k"},
        body='{"model":"m"}',
        config=_cfg(),
    )
    call = client.streamed[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://x.invalid/v1/chat/completions"
    assert call["content"] == '{"model":"m"}'
    assert call["headers"] == {"Authorization": "Bearer k"}


def test_状态码非200也返回响应而不是抛异常_B28():
    """解析层要看到响应体才能给出「带前 200 字」的错误（`B1`/`B28`）。
    传输层在这里判死会把 HTML 网关页丢在门外，错误消息就没内容了。
    """
    transport, _factory, _client = _transport(
        status=502, chunks=[b"<html>502 Bad Gateway</html>"]
    )
    response = transport.post(
        "https://x.invalid/v1/c", headers={}, body="{}", config=_cfg()
    )
    assert response.status == 502
    assert "502 Bad Gateway" in response.body

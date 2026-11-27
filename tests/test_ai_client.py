"""`AiClient`：一次调用的完整语义（拼请求、调传输、翻结果类型）。

**客户端内不做重试**（AI-12 的硬要求）。这条不是洁癖：一次任务在客户端里偷偷
重试 2 次，数据库只记 1 次 `attempts`，界面上「重试了几次」就是错的，而且
`mark_failed` 的指数退避完全失效——真正意义上的「两套计数」，必然有一天打架。

**客户端不判 HTTP 状态的语义**。`401` 与 `500` 的区别由 `TransportResponse.status`
带给 worker，客户端只负责把它翻成 `FailureCategory`。这样 `B26`（一次判死）
与 `B12`（消耗一次尝试）在同一个地方落地，不需要两处各判一次。
"""

from __future__ import annotations

import json
import logging

import pytest

from kb.ai import client as client_mod
from kb.ai import outcomes
from kb.ai.client import AiClient
from kb.ai.config import AiConfig
from kb.ai.transport import TransportError, TransportResponse


def _cfg(**overrides: object) -> AiConfig:
    base: dict[str, object] = {
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-super-secret-value",
        "model": "gpt-4o-mini",
    }
    base.update(overrides)
    return AiConfig(**base)  # type: ignore[arg-type]


class ScriptedTransport:
    """按剧本依次返回 `TransportResponse` 或抛 `TransportError`。

    自带 `calls` 与 `seen`：
    - `calls` 是**普通实例属性**而不是 property——测试要能直接读
      `transport.calls == 1`，用 `MagicMock` 时「没被调用」与「被调用了但返回
      None」在断言上无法区分（设计 §4.2）。
    - `seen` 记录每次的 `(url, headers, body)`，供「请求体含某字段」这类断言。
    """

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls = 0
        self.seen: list[tuple[str, dict[str, str], str]] = []

    def post(
        self,
        url: str,
        headers: dict[str, str],
        body: str,
        config: AiConfig,
    ) -> TransportResponse:
        self.calls += 1
        self.seen.append((url, headers, body))
        if not self.script:
            raise AssertionError("剧本用光了，说明客户端多调了一次")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, TransportResponse)
        return item


def _ok_body(
    summary: str = "这是一段足够长的中文摘要内容",
    tags: list[str] | None = None,
    usage: dict[str, int] | None = None,
) -> str:
    payload: dict[str, object] = {"summary": summary, "tags": tags or []}
    if usage is not None:
        payload["usage"] = usage
    return json.dumps(payload, ensure_ascii=False)


def _response(body: str, status: int = 200) -> TransportResponse:
    return TransportResponse(status=status, content_type="application/json", body=body)


# ── A1：一次调用 ───────────────────────────────────────────────


def test_成功调用只发一次请求_A1():
    """A1：一条内容一次模型调用。不是两次、不是零次。"""
    transport = ScriptedTransport([_response(_ok_body(tags=["健康"]))])
    client = AiClient(config=_cfg(), transport=transport)

    outcome = client.summarize("标题", "正文" * 600, ((5, "健康", ""),))

    assert transport.calls == 1
    assert isinstance(outcome, outcomes.SummarizeOk)
    assert outcome.summary == "这是一段足够长的中文摘要内容"
    assert outcome.tags == ("健康",)


def test_客户端内不做重试_AI_12():
    """AI-12：喂两份失败剧本，`transport` 只被调 1 次。

    若客户端偷偷重试，这里会是 2。这条断言是「重试统一由 `jobs.attempts` 管」
    在代码层面可被一眼确认的证据。
    """
    transport = ScriptedTransport(
        [_response("<html>500</html>", status=500), _response(_ok_body())]
    )
    client = AiClient(config=_cfg(), transport=transport)

    outcome = client.summarize("标题", "正文" * 600, ())

    assert transport.calls == 1, "客户端内部不得重试，重试由 jobs.attempts 管"
    assert isinstance(outcome, outcomes.SummarizeFailed)


# ── 请求体形状 ─────────────────────────────────────────────────


def test_请求体含response_format为json_object():
    """取 openai-python 的字段口径（设计「复用调研」）：能让格式错误率接近零。

    但**仍保留解析失败的重试路径**——不是所有兼容端点都真的实现了这个参数。
    """
    transport = ScriptedTransport([_response(_ok_body())])
    client = AiClient(config=_cfg(), transport=transport)

    client.summarize("标题", "正文" * 600, ())

    body = json.loads(transport.seen[0][2])
    assert body["response_format"] == {"type": "json_object"}
    assert body["model"] == "gpt-4o-mini"
    assert body["messages"], "没带 messages"


def test_请求带_bearer_授权头():
    transport = ScriptedTransport([_response(_ok_body())])
    client = AiClient(config=_cfg(), transport=transport)

    client.summarize("标题", "正文" * 600, ())

    headers = transport.seen[0][1]
    assert headers["Authorization"] == "Bearer sk-super-secret-value"
    assert "application/json" in headers["Content-Type"]


def test_请求_url_由_base_url_拼出_chat_completions():
    transport = ScriptedTransport([_response(_ok_body())])
    client = AiClient(
        config=_cfg(base_url="https://api.example.com/v1"), transport=transport
    )

    client.summarize("标题", "正文" * 600, ())

    assert transport.seen[0][0] == "https://api.example.com/v1/chat/completions"


def test_base_url_末尾多余斜杠不会拼出双斜杠():
    """用户手填配置最常见的一种写法。`//chat/completions` 在多数网关上是 404。"""
    transport = ScriptedTransport([_response(_ok_body())])
    client = AiClient(
        config=_cfg(base_url="https://api.example.com/v1/"), transport=transport
    )

    client.summarize("标题", "正文" * 600, ())

    assert transport.seen[0][0] == "https://api.example.com/v1/chat/completions"


# ── 非 200 状态码（B12 / B26 / B25 / B24）──────────────────────


def test_非200状态码不进解析_B12():
    """B12：HTTP 500 时不该拿 HTML/空 body 去解析——错误消息会变成
    「不是合法 JSON」，把真正的原因（服务端 500）盖掉。
    """
    transport = ScriptedTransport([_response("not json at all", status=500)])
    client = AiClient(config=_cfg(), transport=transport)

    outcome = client.summarize("标题", "正文" * 600, ())

    assert isinstance(outcome, outcomes.SummarizeFailed)
    assert outcome.category is outcomes.FailureCategory.HTTP_5XX
    assert outcome.http_status == 500
    assert "503" in outcome.message or "500" in outcome.message


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx归为HTTP_5XX_B24(status):
    transport = ScriptedTransport([_response("boom", status=status)])
    client = AiClient(config=_cfg(), transport=transport)
    outcome = client.summarize("标题", "正文" * 600, ())
    assert isinstance(outcome, outcomes.SummarizeFailed)
    assert outcome.category is outcomes.FailureCategory.HTTP_5XX


@pytest.mark.parametrize("status", [401, 403])
def test_401与403归为AUTH_B26(status):
    """B26：密钥错属于不可自愈错误，一次判死。

    `error` 要含状态码与「检查 api_key」字样——重试 3 次只是白等 3 分钟，
    且用户看不到真正原因（`§8.2` 的错误消息形态）。
    """
    transport = ScriptedTransport([_response("unauthorized", status=status)])
    client = AiClient(config=_cfg(), transport=transport)

    outcome = client.summarize("标题", "正文" * 600, ())

    assert isinstance(outcome, outcomes.SummarizeFailed)
    assert outcome.category is outcomes.FailureCategory.AUTH
    assert outcome.http_status == status
    assert str(status) in outcome.message
    assert "api_key" in outcome.message


def test_429归为HTTP_429并带上重试秒数_B25():
    transport = ScriptedTransport(
        [
            TransportResponse(
                status=429,
                content_type="application/json",
                body="rate limited",
                retry_after_sec=60.0,
            )
        ]
    )
    client = AiClient(config=_cfg(), transport=transport)

    outcome = client.summarize("标题", "正文" * 600, ())

    assert isinstance(outcome, outcomes.SummarizeFailed)
    assert outcome.category is outcomes.FailureCategory.HTTP_429
    assert outcome.retry_after_sec == 60.0


def test_未分类的4xx归为UNEXPECTED():
    transport = ScriptedTransport([_response("bad", status=418)])
    client = AiClient(config=_cfg(), transport=transport)
    outcome = client.summarize("标题", "正文" * 600, ())
    assert isinstance(outcome, outcomes.SummarizeFailed)
    assert outcome.category is outcomes.FailureCategory.UNEXPECTED


# ── 传输层异常翻类型（B22 / B23）───────────────────────────────


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("timeout", outcomes.FailureCategory.TIMEOUT),
        ("connect", outcomes.FailureCategory.CONNECT),
        ("unexpected", outcomes.FailureCategory.UNEXPECTED),
    ],
)
def test_传输异常按kind翻成失败类别_B22_B23(kind, expected):
    """传输层只报 `kind`，类别映射在这里一次定死。

    两处各判一次的话，「`ConnectTimeout` 算 CONNECT 还是 TIMEOUT」这种分歧
    会出现在两个文件里，而测试只会覆盖其中一处。
    """
    transport = ScriptedTransport([TransportError(kind=kind, message="x")])
    client = AiClient(config=_cfg(), transport=transport)

    outcome = client.summarize("标题", "正文" * 600, ())

    assert isinstance(outcome, outcomes.SummarizeFailed)
    assert outcome.category is expected


# ── B29：截断的响应 ───────────────────────────────────────────


def test_传输层标了truncated就归为TOO_LARGE_B29():
    """B29：截断的响应即使能解析出 JSON 也不用——它是残片，内容不可信。"""
    transport = ScriptedTransport(
        [
            TransportResponse(
                status=200,
                content_type="application/json",
                body=_ok_body(),
                truncated=True,
            )
        ]
    )
    client = AiClient(config=_cfg(), transport=transport)

    outcome = client.summarize("标题", "正文" * 600, ())

    assert isinstance(outcome, outcomes.SummarizeFailed)
    assert outcome.category is outcomes.FailureCategory.TOO_LARGE


# ── D2：usage 缺失 ────────────────────────────────────────────


def test_响应不带usage时按字符数估算token_D2():
    """D2：按「字符数 × 1.0」估算 token 数记账（保守偏高，宁可提前停）。

    估算放在**客户端**而不是解析层：解析层拿不到「实际送了多少字符」，
    只有这里同时拥有请求体与响应。
    """
    transport = ScriptedTransport([_response(_ok_body())])
    client = AiClient(config=_cfg(), transport=transport)

    outcome = client.summarize("标题", "正文" * 600, ())

    assert isinstance(outcome, outcomes.SummarizeOk)
    assert outcome.usage.prompt_tokens > 0, "缺 usage 时必须按字符数估算，不能记 0"
    assert outcome.usage.completion_tokens > 0
    # 估算值应当与摘要长度同量级，不是随便一个数
    assert outcome.usage.completion_tokens >= len(outcome.summary) * 0.9


def test_响应带usage时用响应的值不叠加估算_D1():
    transport = ScriptedTransport(
        [_response(_ok_body(usage={"prompt_tokens": 1200, "completion_tokens": 150}))]
    )
    client = AiClient(config=_cfg(), transport=transport)

    outcome = client.summarize("标题", "正文" * 600, ())

    assert isinstance(outcome, outcomes.SummarizeOk)
    assert outcome.usage.prompt_tokens == 1200
    assert outcome.usage.completion_tokens == 150


# ── §8.2：日志不得含密钥 ───────────────────────────────────────


def test_api_key不写入日志_8点2节(caplog):
    """`§8.2`：日志会进 `journalctl`，密钥进去等于泄露。

    让调用成功与失败各跑一次，把 `kb.ai` 全部日志抓起来断言不含密钥。
    """
    caplog.set_level(logging.DEBUG, logger="kb.ai")

    ok_transport = ScriptedTransport([_response(_ok_body())])
    AiClient(config=_cfg(), transport=ok_transport).summarize("标题", "正文" * 600, ())

    fail_transport = ScriptedTransport([_response("boom", status=500)])
    AiClient(config=_cfg(), transport=fail_transport).summarize(
        "标题", "正文" * 600, ()
    )

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "sk-super-secret-value" not in joined, f"密钥进了日志：{joined}"


def test_日志含调用结果但不含完整响应体_8点2节(caplog):
    """只留前 200 字。完整响应体可能很长，也可能带模型回显的敏感内容。"""
    caplog.set_level(logging.DEBUG, logger="kb.ai")
    long_body = "x" * 5000
    transport = ScriptedTransport([_response(long_body)])
    AiClient(config=_cfg(), transport=transport).summarize("标题", "正文" * 600, ())

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "x" * 500 not in joined, "完整响应体进了日志"


# ── 客户端不做的事情（守边界）─────────────────────────────────


def test_客户端不读环境变量也不读数据库_E6():
    """`E6`：`kb/ai/` 下不得出现 `import os` / `os.environ` / `getenv`。"""
    import inspect

    for module in (client_mod,):
        source = inspect.getsource(module)
        assert "import os" not in source
        assert "os.environ" not in source
        assert "getenv" not in source

    assert not hasattr(client_mod, "os")

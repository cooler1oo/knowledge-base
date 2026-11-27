"""AI 层配置与结果类型：常量口径、预估下限、依赖清单。

这一组的断言不是「代码跑通」，而是**数值与清单**对不对：

- `G6` 要的是 `pyproject.toml` 的 `dependencies` 里有 `httpx`。本机靠 `parsehub`
  的传递依赖就能 `import httpx`，所以「import 成功」这条断言拦不住任何东西，
  必须解析清单本身。
- `D3` 能不能成立取决于「本次预估」有没有 0.001 元下限：一条 100 字的内容按
  单价算是 0.0005 元，`0.999 + 0.0005 < 1.0`，闸门拦不住，`D3` 的「零次 HTTP
  请求」直接失败。所以下限是需求支点，不是防御性代码。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from kb.ai import config as ai_config
from kb.ai import outcomes

ROOT = Path(__file__).resolve().parent.parent


# ── G6：依赖清单 ────────────────────────────────────────────────


def test_依赖清单里必须有_httpx_G6():
    """AI-12 / G6：查 `[project].dependencies`，不是查 `import httpx` 成不成功。

    本机的 `httpx` 是 `parsehub` 捎带装上的（实测 0.28.1，`Required-by` 是
    `f2` 与 `parsehub`）。换干净机器后 `import` 是否成功取决于上游有没有改，
    所以只有清单能作为证据。
    """
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    assert any(dep.split(">")[0].split("=")[0].strip() == "httpx" for dep in deps), (
        f"dependencies 里没有 httpx：{deps}"
    )


def test_httpx_版本下界是_0_28_G6():
    """锁下界的理由：0.28 是实测过 `trust_env=False` 与三级超时签名的版本。"""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = [d for d in data["project"]["dependencies"] if d.startswith("httpx")]
    assert deps, "dependencies 里没有 httpx"
    assert ">=0.28" in deps[0], f"httpx 版本下界不对：{deps[0]}"


# ── 常量口径 ────────────────────────────────────────────────────


def test_三级超时常量为_5_10_20_AI_12():
    """AI-12：连接 5 秒 / 读 10 秒 / 总 20 秒。

    三个值只允许有一处定义（`kb/ai/config.py`），client 与 transport 都从这里取。
    两处各写一份的话，改超时会出现「连接 5 秒、总超时还是 20」这种半更新。
    """
    assert ai_config.TIMEOUT_CONNECT == 5.0
    assert ai_config.TIMEOUT_READ == 10.0
    assert ai_config.TIMEOUT_TOTAL == 20.0


def test_响应体上限是_256KB_B29():
    assert ai_config.MAX_RESPONSE_BYTES == 256 * 1024


def test_默认单价与预算默认值_AI_05_AI_06():
    """单价 0.002 元/千 token（AI-06 的金额口径），预算默认 20 元（AI-05）。

    预算默认值只在 `kb/store/usage.py` 一处，它的断言在 `tests/test_usage_store.py`
    ——那里才是消费它的地方（`D5`/`D13`/`D16`）。这个用例只钉单价，
    因为单价是 AI 层自己算预估时用的。
    """
    assert ai_config.DEFAULT_PRICE_CNY_PER_1K == 0.002


def test_估算假定输出_150_token():
    """§6.5：摘要 ≤100 字 + 标签名，按 150 token 估输出，偏保守。"""
    assert ai_config.ESTIMATE_OUTPUT_TOKENS == 150


# ── §6.5：预估下限（D3 的支点）──────────────────────────────────


def test_预估费用有下限_6点5节():
    """100 字内容的预估必须被抬到 0.001，否则 `D3` 拦不住。

    算一遍：0.002 × (100 + 150) / 1000 = 0.0005 元。
    `0.999 + 0.0005 = 0.9995 < 1.0` → 闸门放行 → `D3` 的「零请求」不成立。
    """
    cfg = ai_config.AiConfig(base_url="https://x.invalid/v1", api_key="k", model="m")
    assert cfg.estimate_cny(100) == pytest.approx(ai_config.MIN_ESTIMATE_CNY)
    assert cfg.estimate_cny(0) == pytest.approx(ai_config.MIN_ESTIMATE_CNY)


def test_长文本预估按单价算不加下限_6点5节():
    """50 万字符的内容预估远大于下限，下限不得压低真实预估。"""
    cfg = ai_config.AiConfig(base_url="https://x.invalid/v1", api_key="k", model="m")
    assert cfg.estimate_cny(500_000) == pytest.approx(0.002 * 500_150 / 1000)


def test_预估下限加累计恰好等于上限时判定为超限_6点5节():
    """`0.999 + 0.001 == 1.0 >= 1.0`，这条等式就是下限存在的理由。"""
    cfg = ai_config.AiConfig(base_url="https://x.invalid/v1", api_key="k", model="m")
    assert 0.999 + cfg.estimate_cny(100) >= 1.0


# ── from_values ────────────────────────────────────────────────


def _values(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "ai_base_url": "https://api.example.com/v1",
        "ai_api_key": "sk-test",
        "ai_model": "gpt-4o-mini",
    }
    base.update(overrides)
    return base


def test_from_values_填默认单价():
    cfg = ai_config.AiConfig.from_values(_values())
    assert cfg.price_cny_per_1k == ai_config.DEFAULT_PRICE_CNY_PER_1K
    assert cfg.timeout_connect == ai_config.TIMEOUT_CONNECT


def test_from_values_读自定义单价():
    cfg = ai_config.AiConfig.from_values(_values(ai_price_cny_per_1k=0.01))
    assert cfg.price_cny_per_1k == pytest.approx(0.01)


def test_from_values_单价写成字符串也能认():
    """`settings` 是 jsonb，用户手改可能写成 `"0.01"`，不能因此崩掉。"""
    cfg = ai_config.AiConfig.from_values(_values(ai_price_cny_per_1k="0.01"))
    assert cfg.price_cny_per_1k == pytest.approx(0.01)


def test_from_values_单价非法时回落到默认值():
    """写坏的配置不该让整条流水线停掉：回默认值比抛异常更可恢复。"""
    cfg = ai_config.AiConfig.from_values(_values(ai_price_cny_per_1k="不是数字"))
    assert cfg.price_cny_per_1k == ai_config.DEFAULT_PRICE_CNY_PER_1K


def test_missing_keys_认出缺失的键名():
    """报错要说缺哪个键，否则用户不知道该去填什么（§8.2 的错误消息形态）。"""
    assert ai_config.missing_keys({"ai_base_url": "u", "ai_model": "m"}) == (
        "ai_api_key",
    )


def test_missing_keys_空串与空白视为缺失_AI_04():
    """AI-04：缺任一**或值为空串**即视为未配置。"""
    values = _values(ai_api_key="   ")
    assert ai_config.missing_keys(values) == ("ai_api_key",)


def test_missing_keys_三键齐备时为空():
    assert ai_config.missing_keys(_values()) == ()


def test_missing_keys_全缺时按固定顺序列出_AI_04():
    assert ai_config.missing_keys({}) == ai_config.REQUIRED_KEYS


def test_from_values_缺键时抛_ValueError():
    with pytest.raises(ValueError, match="ai_api_key"):
        ai_config.AiConfig.from_values({"ai_base_url": "u", "ai_model": "m"})


# ── §4.3：三种结果形态 ─────────────────────────────────────────


def test_结果类型三种形态可区分_4点3节():
    """成功 / 跳过 / 失败必须是类型层面的区分，不能是布尔 + 异常。

    理由（§4.3）：`title_only` 与 `no_text` 不是错误，用异常表达会被
    `run_once` 那条「任何异常都收成任务失败」的兜底吞掉，`attempts` 被 +1，
    `B15`/`B16` 断言的 `attempts=0` 直接不成立。
    """
    ok = outcomes.SummarizeOk(summary="一段十四个字的摘要内容吧")
    skipped = outcomes.SummarizeSkipped(reason="title_only")
    failed = outcomes.SummarizeFailed(
        category=outcomes.FailureCategory.BAD_RESPONSE, message="x"
    )

    assert isinstance(ok, outcomes.SummarizeOk)
    assert not isinstance(ok, outcomes.SummarizeSkipped)
    assert isinstance(skipped, outcomes.SummarizeSkipped)
    assert not isinstance(skipped, outcomes.SummarizeFailed)
    assert isinstance(failed, outcomes.SummarizeFailed)
    assert not isinstance(failed, outcomes.SummarizeOk)


def test_失败类别逐个存在_4点5节():
    expected = {
        "BAD_RESPONSE",
        "TOO_LARGE",
        "TIMEOUT",
        "CONNECT",
        "HTTP_5XX",
        "HTTP_429",
        "AUTH",
        "UNEXPECTED",
    }
    assert {member.name for member in outcomes.FailureCategory} == expected


def test_SummarizeOk_默认没有标签也没有用量():
    ok = outcomes.SummarizeOk(summary="x" * 20)
    assert ok.tags == ()
    assert isinstance(ok.usage, outcomes.TokenUsage)


def test_SummarizeFailed_默认字段可省():
    failed = outcomes.SummarizeFailed(
        category=outcomes.FailureCategory.TIMEOUT, message="读超时"
    )
    assert failed.http_status == 0
    assert failed.retry_after_sec == 0.0
    assert failed.snippet == ""


# ── 包入口的惰性导出（PEP 562）────────────────────────────────
#
# `kb/ai/__init__.py` 刻意把 `AiClient` / `HttpxTransport` 做成惰性导出，
# 好让纯函数单测在没装 `httpx` 的环境里也能跑（`E6`）。这段行为只在
# 「访问属性时」发生，静态 import 测不到，必须显式访问一次。


def test_惰性导出能取到AiClient():
    import kb.ai as pkg

    assert pkg.AiClient.__name__ == "AiClient"


def test_惰性导出能取到HttpxTransport():
    import kb.ai as pkg

    assert pkg.HttpxTransport.__name__ == "HttpxTransport"


def test_惰性导出取不到的名字抛_AttributeError():
    """拼错名字时要明确报错，而不是返回一个 `None` 让错误推迟到别处。"""
    import kb.ai as pkg

    with pytest.raises(AttributeError, match="没有这个名字|has no attribute"):
        _ = pkg.NoSuchThing  # type: ignore[attr-defined]


def test_build_client_返回真实客户端且带传输():
    """`build_client` 是装配的唯一入口，`__main__` 不该知道 `HttpxTransport` 存在。"""
    from kb.ai import build_client
    from kb.ai.transport import HttpxTransport

    cfg = ai_config.AiConfig(base_url="https://x.invalid/v1", api_key="k", model="m")
    client = build_client(cfg)
    assert isinstance(client.transport, HttpxTransport)
    assert client.config is cfg


def test_纯函数模块不依赖httpx_E6():
    """`E6`：`parse` / `validate` / `prompts` 不 import `httpx`。

    这条是「AI 层的纯函数单测能在干净环境跑」的静态证据：三个模块的源码里
    不出现 `httpx`。行为层面的证据是 `tests/test_ai_summarize.py` 与
    `tests/test_ai_tag.py` 不 import 任何需要 `httpx` 的模块。
    """
    import inspect

    from kb.ai import parse, prompts, validate

    for module in (parse, prompts, validate):
        source = inspect.getsource(module)
        assert "import httpx" not in source, f"{module.__name__} 依赖 httpx"


def test_kb_ai_里不得出现三处进程环境写法_E6():
    """`E6`：`grep -c "import os\\|os.environ\\|getenv" kb/ai/*.py` 全为 0。

    **这条把 grep 断言搬进测试。** 为什么必要：`E6` 是 PRD 里写死的验收，
    但它的执行方式是「人跑一次 grep」——而 `grep` 也会命中注释。本文件第一版
    就在模块 docstring 里写了那三个字面量当说明，结果 `config.py` 计数是 2。
    写成测试之后，「加注释时顺手带上那几个字」会立刻红。

    只扫 `.py` 源码，不扫 `__pycache__`（`.pyc` 是二进制，命中与否无意义）。
    """
    hits: dict[str, int] = {}
    for path in sorted((ROOT / "kb" / "ai").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        count = sum(
            text.count(token) for token in ("import os", "os.environ", "getenv")
        )
        if count:
            hits[path.name] = count
    assert not hits, f"kb/ai/ 里出现了进程环境写法（E6 要求为 0）：{hits}"

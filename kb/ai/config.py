"""AI 层配置：`AiConfig` 与全部数值常量。

**这一层不读 `settings`、不读进程环境变量、不碰标准库 `os` 模块。**
`E6` 的验收方式是 `kb/ai/` 下 `grep` 这三类写法计数全为 0——所以本文件
连注释里都不写那几个字面量（注释也算 grep 的输入，写了就会让那条断言变红）。

配置由 `kb/worker/ai_runtime.load_config()` 从 `settings` 表读出后拼成
`AiConfig` 传进来（设计 §4.4、PRD §5.7）。这样做有两个直接好处：

1. 配置只有一个权威源。两处（表 + 进程环境）必然漂移：改了表里的值、
   环境没改，AI 层用旧值且没有任何地方报错。
2. AI 层变成纯函数，`E6` 要求的「单测不建 `conn` fixture 也能跑」成立。

数值常量只允许在本文件出现一次。`client.py` 与 `transport.py` 都从这里取——
两处各写一份的话，改超时会出现「连接 5 秒、总超时还是 20」这种半更新。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

# ── 超时（AI-12：连接 5 秒 / 读 10 秒 / 总 20 秒）──────────────────
#
# "总 20 秒" 不是 `httpx.Timeout` 的字段：实测 httpx 0.28.1 的
# `httpx.Timeout` 只有 connect / read / write / pool 四个字段，**没有 total**。
# `read` 超时是「单次读操作等多久」，服务端每 9 秒吐一个字节就能让一次请求
# 无限延长。所以总超时由 `HttpxTransport.post` 里「按块读 + 累计计时」自己守
# （设计 §6.1 末尾），这两个常量喂给那个读循环。
TIMEOUT_CONNECT: Final[float] = 5.0
TIMEOUT_READ: Final[float] = 10.0
TIMEOUT_TOTAL: Final[float] = 20.0

# ── 响应体上限（B29）────────────────────────────────────────────
#
# 平板内存预算紧，单条响应无上限是隐患。超限即停读并归为 TOO_LARGE。
MAX_RESPONSE_BYTES: Final[int] = 256 * 1024

# ── 计价与预估（§6.5）───────────────────────────────────────────

#: 单价默认值。单位：元 / 1000 token。`settings['ai_price_cny_per_1k']` 可覆盖。
DEFAULT_PRICE_CNY_PER_1K: Final[float] = 0.002

#: 预估时假定的输出 token 数。摘要 ≤100 字 + 标签名，按「字符数 × 1.0」估算约
#: 100–150 token，取 150 偏保守（宁可提前停）。
ESTIMATE_OUTPUT_TOKENS: Final[int] = 150

#: **本次预估的下限。没有它 `D3` 就不成立。**
#:
#: 一条 100 字的内容按单价算是 `0.002 × 250 / 1000 = 0.0005` 元。
#: 当月累计 0.999 时：`0.999 + 0.0005 = 0.9995 < 1.0` → 闸门放行 →
#: `D3` 断言的「零次 HTTP 请求」直接失败。加下限后
#: `0.999 + 0.001 = 1.0 ≥ 1.0`，拦得住。
#:
#: 副作用：当月累计在离上限 0.001 元以内时会提前停一次。偏差方向是
#: 「少花钱」，与 `D2` 的「宁可提前停」一致。
MIN_ESTIMATE_CNY: Final[float] = 0.001

# ── 配置键名（与 PRD §8.1 定死的一致）───────────────────────────

#: 三个**必需**键。缺任一（或值为空串）即视为「未配置」（AI-04）。
REQUIRED_KEYS: Final[tuple[str, ...]] = ("ai_base_url", "ai_api_key", "ai_model")

#: 可选键。缺了就取默认值，不影响「是否已配置」的判定。
PRICE_KEY: Final[str] = "ai_price_cny_per_1k"

# ── 摘要长度（AI-13 ①②）───────────────────────────────────────
#
# 只在这里定义一处：`prompts.py` 写进 prompt 的说明、`parse.py` 的判据、
# worker 的日志都用这三个常量。三处各写一份数字，改一处就有两处悄悄过期。
#
# 上限 100 是列表页一行放得下的字数；下限 10 挡住空串、"无"、"好的"这类敷衍输出。

SUMMARY_MIN_CHARS: Final[int] = 10
SUMMARY_MAX_CHARS: Final[int] = 100


@dataclass(frozen=True)
class AiConfig:
    """一次调用所需的全部配置。构造后不可变——worker 每轮现读现拼。"""

    base_url: str
    api_key: str
    model: str
    price_cny_per_1k: float = DEFAULT_PRICE_CNY_PER_1K
    timeout_connect: float = TIMEOUT_CONNECT
    timeout_read: float = TIMEOUT_READ
    timeout_total: float = TIMEOUT_TOTAL
    max_response_bytes: int = MAX_RESPONSE_BYTES

    def estimate_cny(self, chars_in: int) -> float:
        """本次调用的预估费用（元），带 0.001 下限。

        `chars_in` 是**实际送给模型的输入字符数**（不是条目正文长度）：
        正文被截到前 3000 字、标题必带，这些都会改变真实成本。
        """
        raw = self.price_cny_per_1k * (chars_in + ESTIMATE_OUTPUT_TOKENS) / 1000.0
        return max(raw, MIN_ESTIMATE_CNY)

    @classmethod
    def from_values(cls, values: dict[str, Any]) -> AiConfig:
        """从 `settings` 读出的键值字典拼出配置。

        三键缺任一抛 `ValueError`（消息里带缺的键名，见 `§8.2` 的错误消息形态）。
        **正常情况下调用方会先用 `missing_keys()` 判一次并走「推迟 30 分钟」分支**，
        这个异常是给「装配 bug」留的显式失败路径，不是常规控制流。
        """
        missing = missing_keys(values)
        if missing:
            raise ValueError(f"AI 未配置（缺 {', '.join(missing)}）")
        return cls(
            base_url=str(values["ai_base_url"]).strip(),
            api_key=str(values["ai_api_key"]).strip(),
            model=str(values["ai_model"]).strip(),
            price_cny_per_1k=_as_price(values.get(PRICE_KEY)),
        )


def missing_keys(values: dict[str, Any]) -> tuple[str, ...]:
    """返回缺失（或值为空白）的**必需**键名，按 `REQUIRED_KEYS` 的固定顺序。

    固定顺序的理由：`E1`/`E2` 的错误消息会被断言含某个键名，顺序随机
    会让「缺 ai_api_key」与「缺 ai_model」的消息在不同轮次里互换。
    """
    missing: list[str] = []
    for key in REQUIRED_KEYS:
        raw = values.get(key)
        if raw is None or not str(raw).strip():
            missing.append(key)
    return tuple(missing)


def _as_price(raw: object) -> float:
    """单价收口。写坏的配置回落到默认值而不是抛异常。

    「宁可跑起来花默认价，也不让整条流水线停掉」——用户手改 `settings` 时
    常见的是写成 `"0.01"` 这类字符串（jsonb 里就是字符串），转得动就转。
    """
    if raw is None:
        return DEFAULT_PRICE_CNY_PER_1K
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_PRICE_CNY_PER_1K
    return value if value > 0 else DEFAULT_PRICE_CNY_PER_1K

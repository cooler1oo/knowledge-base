"""ParseHub 适配层：把第三方库的技术细节关在这一个文件里。

为什么要单独一层：抓取路由只关心「拿到一个 payload」，不该知道 ParseHub 是
异步的、需要单例、异常叫 `FallbackError`。换掉 ParseHub（比如切 §4.9 的 CDP 退路）
时只改这个文件。

`duration` 的毫秒→秒换算**不在**这里做，在 `normalize.py`。理由：换抓取实现后
仍然要换算，放进适配层会让退路实现重复一遍。
"""

from __future__ import annotations

from typing import Any

# 单例：ParseHub 内部维护 session，每次 new 一个会白白重建 TLS 连接
_hub: Any = None


def get_hub() -> Any:
    """惰性创建单例。导入 parsehub 很慢（57 个依赖），不要放在模块顶层。"""
    global _hub
    if _hub is None:
        from parsehub import ParseHub

        _hub = ParseHub()
    return _hub


def reset_hub() -> None:
    """测试用：清掉单例，避免用例之间互相污染。"""
    global _hub
    _hub = None


async def parse(url: str) -> object:
    """抓一条 URL，返回 ParseHub 的原始 result 对象。

    不在这里捕获异常：`router.extract()` 已经做了统一的降级，重复捕获会让
    「哪一层负责降级」变得含糊。
    """
    return await get_hub().parse(url)

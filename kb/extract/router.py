"""抓取路由：按域名决定走哪条路，并把失败收成「可入库的记录」。

三条设计原则：

1. **失败不抛异常。** 抓取失败要让记录进库并标「处理失败」，不是让 worker 崩掉
   （技术方案 §4.10「第三层降级」）。所以 `extract()` 永远返回 `ExtractOutcome`。
2. **小红书也走抓取，不返回 unsupported。** AGENTS.md 写「小红书必须能接」，
   指的是**入口必须接受**；内容抓不到是内容层的失败，要如实记在 `error` 里，
   而不是在入口就拒收。这两件事混在一起会让用户以为「存不进去」。
3. **域名匹配按标签边界。** `notdouyin.com` 不能被 `douyin.com` 命中——
   子串匹配是最容易踩的坑，用 `.endswith("." + domain) or == domain`。

`extract()` 是异步的：ParseHub 的 `parse()` 是 async。同步入口留给 worker 用
`asyncio.run()` 包一层即可。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from .normalize import ExtractResult, normalize_payload

# 走 ParseHub 的域名（技术方案 §4.4 的平台清单）
_PARSEHUB_DOMAINS: tuple[str, ...] = (
    "douyin.com",
    "iesdouyin.com",
    "xiaohongshu.com",
    "xhslink.com",
    "bilibili.com",
    "b23.tv",
    "weibo.com",
    "weibo.cn",
    "zhihu.com",
    "kuaishou.com",
    "toutiao.com",
    "douban.com",
    "tieba.baidu.com",
    "coolapk.com",
    "xiaoheihe.cn",
    "zuiyou.com",
    "pipix.com",
    "jandan.net",
    "bangumi.tv",
)

ROUTE_PARSEHUB = "parsehub"
ROUTE_ARTICLE = "article"
ROUTE_UNSUPPORTED = "unsupported"


class ParseHubLike(Protocol):
    """只依赖一个方法，测试能塞假实现，不把 ParseHub 焊死在这里。"""

    async def parse(self, url: str) -> object: ...


@dataclass(frozen=True)
class ExtractOutcome:
    """一次抓取的结果。`ok=False` 时 `kind` 一定是 `link`、`error` 一定有值。"""

    ok: bool
    route: str
    kind: str
    data: ExtractResult | None = None
    error: str = ""


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return ""


def _is_http(url: str) -> bool:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.hostname)


def _matches(host: str, domain: str) -> bool:
    """按标签边界匹配：`v.douyin.com` 命中 `douyin.com`，`notdouyin.com` 不命中。"""
    return host == domain or host.endswith("." + domain)


def choose_route(url: str) -> str:
    """决定这条 URL 该走哪条抓取路线。非 http(s) 一律 unsupported。"""
    if not _is_http(url):
        return ROUTE_UNSUPPORTED
    host = _host_of(url)
    for domain in _PARSEHUB_DOMAINS:
        if _matches(host, domain):
            return ROUTE_PARSEHUB
    # 没登记过的域名不等于「不支持」：任何网页都先按文章试一遍，
    # 抓不到再降级。报「不支持」会让用户以为连存都存不了。
    return ROUTE_ARTICLE


async def extract(
    url: str,
    *,
    hub: ParseHubLike | None = None,
    article_fetcher: object | None = None,
) -> ExtractOutcome:
    """抓一条 URL。永不抛异常——失败也是返回一个可入库的结果。

    `article_fetcher` 本轮不做（R3 只打通抖音）；传了也没用，走 ParseHub 之外的
    路线一律返回失败并说明「文章抓取尚未实现」，而不是假装成功。
    """
    route = choose_route(url)
    if route == ROUTE_UNSUPPORTED:
        return ExtractOutcome(
            ok=False,
            route=route,
            kind="link",
            error=f"不支持的链接（只接受 http/https）：{url[:80]}",
        )

    if route == ROUTE_ARTICLE:
        return ExtractOutcome(
            ok=False,
            route=route,
            kind="link",
            error="文章抓取尚未实现（R3 只打通抖音），已保留链接",
        )

    if hub is None:
        return ExtractOutcome(
            ok=False,
            route=route,
            kind="link",
            error="抓取器未注入，无法抓取",
        )

    try:
        raw = await hub.parse(url)
    except Exception as exc:  # noqa: BLE001 抓取层的职责就是把异常收成记录，见模块 docstring 第 1 条
        return ExtractOutcome(
            ok=False,
            route=route,
            kind="link",
            error=f"{type(exc).__name__}: {exc}",
        )

    payload = _payload_of(raw)
    if payload is None:
        return ExtractOutcome(
            ok=False,
            route=route,
            kind="link",
            error="抓取返回空（无法解析出任何字段）",
        )

    data = normalize_payload(payload)
    # 标题与原始链接都空的，等于什么都没抓到：按 §4.10 降级，别存一条空壳
    if not data.title and not data.raw_url:
        return ExtractOutcome(
            ok=False,
            route=route,
            kind="link",
            error="抓取结果为空（title 与 raw_url 均缺失）",
        )

    return ExtractOutcome(ok=True, route=route, kind=data.kind, data=data)


def _payload_of(raw: object) -> dict[str, object] | None:
    """把 parsehub 的 result 对象或已 to_dict 的 dict 统一成 dict。"""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    to_dict = getattr(raw, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, dict):
            return result
    return None

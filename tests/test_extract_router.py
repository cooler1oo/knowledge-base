"""抓取器单测：路由、降级、失败记录。

不连网。真抓取在 `tests/test_extract_live.py`（标 external，本机默认跳过）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict[str, object]:
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


class FakeHub:
    """按 URL 返回预置结果或抛预置异常，用来验路由与降级。"""

    def __init__(
        self,
        payloads: dict[str, dict[str, object]] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.payloads = payloads or {}
        self.errors = errors or {}
        self.calls: list[str] = []

    async def parse(self, url: str) -> object:
        self.calls.append(url)
        if url in self.errors:
            raise self.errors[url]
        payload = self.payloads.get(url)
        if payload is None:
            raise AssertionError(f"测试未预置该 URL 的返回：{url}")

        class _Result:
            def __init__(self, data: dict[str, object]) -> None:
                self._data = data

            def to_dict(self) -> dict[str, object]:
                return self._data

        return _Result(payload)


# ── 路由：哪些域名走 ParseHub ──────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://v.douyin.com/abc/",
        "https://www.douyin.com/video/123",
        "https://www.xiaohongshu.com/explore/abc",
        "https://b23.tv/abc",
        "https://www.bilibili.com/video/BV1xx",
        "https://weibo.com/123/abc",
        "https://www.zhihu.com/question/1",
        "https://www.kuaishou.com/short-video/1",
        "https://www.toutiao.com/article/1",
    ],
)
def test_这些平台走_parsehub(url: str):
    from kb.extract.router import choose_route

    assert choose_route(url) == "parsehub"


def test_公众号与普通文章走_article():
    """静态页用 httpx + trafilatura 更省事，不必启动 ParseHub 的签名链路。"""
    from kb.extract.router import choose_route

    assert choose_route("https://mp.weixin.qq.com/s/abc") == "article"
    assert choose_route("https://example.com/blog/post") == "article"


def test_未知域名也走_article_兜底():
    """任何 http 链接都要有个去处，不能因为没登记域名就报「不支持」。"""
    from kb.extract.router import choose_route

    assert choose_route("https://some-unknown-site.example/x") == "article"


def test_非_http_链接直接判无法抓取():
    from kb.extract.router import choose_route

    assert choose_route("ftp://a.com/x") == "unsupported"
    assert choose_route("") == "unsupported"


def test_域名匹配不受大小写与端口影响():
    from kb.extract.router import choose_route

    assert choose_route("HTTPS://V.DOUYIN.COM/abc/") == "parsehub"
    assert choose_route("https://v.douyin.com:443/abc/") == "parsehub"


def test_长得像但不是的域名不算命中():
    """`notdouyin.com` 不该被 `douyin.com` 命中。子串匹配是会踩的坑。"""
    from kb.extract.router import choose_route

    assert choose_route("https://notdouyin.com/x") == "article"
    assert choose_route("https://douyin.com.evil.example/x") == "article"


# ── 抓取成功 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_抓取视频成功_返回归一后的结果():
    from kb.extract.router import extract

    url = "https://v.douyin.com/abc/"
    hub = FakeHub(payloads={url: fixture("douyin_video")})

    result = await extract(url, hub=hub)

    assert result.ok is True
    assert result.data is not None
    assert result.data.kind == "video"
    assert result.data.media[0].duration_sec == 445
    assert hub.calls == [url]


@pytest.mark.asyncio
async def test_抓取图文成功_返回归一后的结果():
    from kb.extract.router import extract

    url = "https://v.douyin.com/def/"
    hub = FakeHub(payloads={url: fixture("douyin_image")})

    result = await extract(url, hub=hub)

    assert result.ok is True
    assert result.data is not None
    assert result.data.kind == "image"
    assert result.data.media[0].role == "image"


# ── 抓取失败：降级而不是抛异常（技术方案 §4.10）──────────────────


@pytest.mark.asyncio
async def test_抓取抛异常时降级为_link_并记录原因():
    """这是「记录不能丢」的落点：失败也要能在界面上看到，不是静默丢弃。"""
    from kb.extract.router import extract

    url = "https://v.douyin.com/abc/"
    hub = FakeHub(errors={url: RuntimeError("被风控拦截")})

    result = await extract(url, hub=hub)

    assert result.ok is False
    assert result.data is None
    assert result.kind == "link"
    assert "被风控拦截" in result.error


@pytest.mark.asyncio
async def test_抓取返回空时也降级():
    from kb.extract.router import extract

    url = "https://v.douyin.com/abc/"
    hub = FakeHub(payloads={url: {}})

    result = await extract(url, hub=hub)

    # 空 payload 归一后 title/url 都是空，视为没抓到
    assert result.ok is False
    assert "空" in result.error


@pytest.mark.asyncio
async def test_抓取异常信息要带异常类型便于排障():
    from kb.extract.router import extract

    url = "https://v.douyin.com/abc/"
    hub = FakeHub(errors={url: TimeoutError("超时")})

    result = await extract(url, hub=hub)

    assert "TimeoutError" in result.error


@pytest.mark.asyncio
async def test_非_http_链接不去调_hub():
    from kb.extract.router import extract

    hub = FakeHub()

    result = await extract("ftp://a.com/x", hub=hub)

    assert result.ok is False
    assert result.error
    assert hub.calls == []  # 根本没调


# ── 小红书：入口必须接受，内容抓不到要如实说 ─────────────────────


@pytest.mark.asyncio
async def test_小红书失败时降级为_link_而不是不支持():
    """AGENTS.md 写「小红书必须能接」。这里的语义是：入口接受、内容抓不到要如实记录。

    不能返回 unsupported —— 那会让用户以为存不进去。
    """
    from kb.extract.router import extract

    url = "https://www.xiaohongshu.com/explore/abc"
    hub = FakeHub(
        errors={url: RuntimeError("所有解析方式均失败: 被风控拦截")},
    )

    result = await extract(url, hub=hub)

    assert result.ok is False
    assert result.kind == "link"
    assert result.route == "parsehub"  # 走的是抓取，不是「不支持」
    assert "风控" in result.error

"""ParseHub 适配层单测：单例行为与异常透传。

不连网——用 monkeypatch 替换真实 ParseHub。
"""

from __future__ import annotations

import pytest

from kb.extract import parsehub_adapter


class _FakeHub:
    def __init__(self, payload: object = None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[str] = []

    async def parse(self, url: str) -> object:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.fixture(autouse=True)
def _clean_singleton():
    """每个用例前后都清掉单例，避免互相污染。"""
    parsehub_adapter.reset_hub()
    yield
    parsehub_adapter.reset_hub()


@pytest.mark.asyncio
async def test_两次调用复用同一个_hub_实例(monkeypatch):
    """ParseHub 内部维护 session，重复创建会白建 TLS 连接。"""
    created: list[int] = []

    class _Factory:
        def __call__(self) -> _FakeHub:
            created.append(1)
            return _FakeHub(payload={"ok": True})

    import parsehub

    monkeypatch.setattr(parsehub, "ParseHub", _Factory())
    await parsehub_adapter.parse("https://a.example/x")
    await parsehub_adapter.parse("https://a.example/y")

    assert len(created) == 1, "重复创建了 ParseHub 实例"


@pytest.mark.asyncio
async def test_reset_hub_之后会重新创建(monkeypatch):
    created: list[int] = []

    class _Factory:
        def __call__(self) -> _FakeHub:
            created.append(1)
            return _FakeHub(payload={"ok": True})

    import parsehub

    monkeypatch.setattr(parsehub, "ParseHub", _Factory())
    await parsehub_adapter.parse("https://a.example/x")
    parsehub_adapter.reset_hub()
    await parsehub_adapter.parse("https://a.example/y")

    assert len(created) == 2


@pytest.mark.asyncio
async def test_异常原样抛出_不在这里吞掉():
    """降级由 router.extract() 统一做。这里再吞一次会让「谁负责降级」变含糊。"""
    hub = _FakeHub(error=RuntimeError("被风控拦截"))
    parsehub_adapter._hub = hub

    with pytest.raises(RuntimeError, match="被风控拦截"):
        await parsehub_adapter.parse("https://www.xiaohongshu.com/explore/abc")


@pytest.mark.asyncio
async def test_把_url_原样传给_hub():
    hub = _FakeHub(payload={"ok": True})
    parsehub_adapter._hub = hub

    await parsehub_adapter.parse("https://v.douyin.com/abc/")

    assert hub.calls == ["https://v.douyin.com/abc/"]

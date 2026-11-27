"""真机抓取测试：调真实网络，抓真实链接。

**默认不跑**（标 `external`）。理由：CI 与本机跑全量时不该依赖外网与抖音可用性，
否则「抖音改版」会让整个测试套件变红，掩盖真正的代码回归。

跑法：
    python -m pytest tests/test_extract_live.py -q -m external

判据（对应 R3 的「命令行能抓到一条抖音的完整元数据」）：
1. `ok=True`，不是降级
2. `kind` 是 video 或 image_text/image（不是 link）
3. `title` 非空
4. 视频有 `duration_sec` 且是合理的秒数（不是 445071 那种毫秒值）
"""

from __future__ import annotations

import pytest

from kb.extract.parsehub_adapter import parse, reset_hub
from kb.extract.router import extract

pytestmark = pytest.mark.external

# 2026-11-27 实测可抓的两条真实链接（用户本人提供）
VIDEO_URL = "https://v.douyin.com/whXfWvfHnIQ/"
IMAGE_URL = "https://v.douyin.com/5gjBAuYo7PE/"
# 已知抓不到：小红书无 cookie 被风控（技术方案 §4.4 v9.11）
XHS_URL = "https://www.xiaohongshu.com/explore/6553f0e9000000003c00d4ab"


@pytest.fixture(autouse=True)
def _fresh_hub():
    reset_hub()
    yield
    reset_hub()


@pytest.mark.asyncio
async def test_真实抖音视频能抓到完整元数据():
    result = await extract(VIDEO_URL, hub=_HubProxy())

    assert result.ok is True, f"抓取失败：{result.error}"
    assert result.data is not None
    data = result.data

    assert data.kind == "video"
    assert data.title.strip(), "标题是空的"
    assert data.raw_url.startswith("https://")
    assert len(data.media) >= 1

    media = data.media[0]
    assert media.role == "video"
    assert media.url.startswith("https://")
    assert media.ext == "mp4"
    # 关键：毫秒没被直接写进来。445071 毫秒 = 445 秒；若断言 < 3600 能抓住这个错
    assert media.duration_sec is not None
    assert 0 < media.duration_sec < 3600, (
        f"时长不合理，可能没做毫秒换算：{media.duration_sec}"
    )


@pytest.mark.asyncio
async def test_真实抖音图文能抓到():
    result = await extract(IMAGE_URL, hub=_HubProxy())

    assert result.ok is True, f"抓取失败：{result.error}"
    assert result.data is not None
    data = result.data

    assert data.kind in {"image", "image_text"}
    assert data.title.strip()
    assert len(data.media) >= 1
    assert data.media[0].role == "image"
    assert data.media[0].duration_sec is None, "图片不该有时长"


@pytest.mark.asyncio
async def test_小红书抓不到时降级为_link_并带原因():
    """这条是「如实记录失败」的活证据。不是断言它该失败，而是断言：
    失败时必须降级成 link + 有可见原因，而不是抛异常或返回 unsupported。"""
    result = await extract(XHS_URL, hub=_HubProxy())

    assert result.route == "parsehub", "小红书不该被判为 unsupported"
    if result.ok:
        # 上游哪天放开了，这条会走到这里——那说明方案 §4.4 该更新了
        pytest.skip(f"小红书这次抓到了（{result.data.kind}），方案 §4.4 需要更新")
    assert result.kind == "link"
    assert result.error


class _HubProxy:
    """把 adapter 的 parse 包成 router 认识的 hub 形状。"""

    async def parse(self, url: str) -> object:
        return await parse(url)

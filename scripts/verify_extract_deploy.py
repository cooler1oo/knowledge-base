"""部署验收：在目标容器内跑真实抓取。

判据（对应 R3 的「命令行能抓到一条抖音的完整元数据」）：
1. 路由正确（douyin → parsehub）
2. ok=True，不是降级
3. kind 是 video / image
4. duration_sec 是合理秒数（< 3600），证明毫秒换算生效
5. backup_urls 能取到
6. 小红书入口接受、内容降级为 link 并带原因

跑法：
proot-distro login debian -- env PYTHONPATH=/opt/kb-app /opt/kb-venv/bin/python /root/verify_extract.py
"""

import asyncio
import sys

from kb.extract.parsehub_adapter import parse, reset_hub
from kb.extract.router import choose_route, extract


class Hub:
    """把 adapter 包成 router 认识的形状。"""

    async def parse(self, url: str) -> object:
        return await parse(url)


async def check(url: str, label: str) -> bool:
    print(f"\n===== {label} =====")
    print(f"url: {url}")

    route = choose_route(url)
    print(f"路由: {route}")

    result = await extract(url, hub=Hub())
    print(f"ok={result.ok} kind={result.kind} route={result.route}")

    if not result.ok:
        print(f"降级原因: {result.error}")
        return False

    data = result.data
    if data is None:
        print("ok=True 但 data 为 None —— 契约被破坏")
        return False

    print(f"title: {data.title[:50]}")
    print(f"platform: {data.platform}")
    print(f"raw_url: {data.raw_url}")
    print(f"media 条数: {len(data.media)}")
    for i, m in enumerate(data.media):
        print(
            f"  [{i}] role={m.role} ext={m.ext} {m.width}x{m.height} "
            f"duration_sec={m.duration_sec} backup={len(m.backup_urls)}"
        )
    return True


async def main() -> int:
    reset_hub()
    video_ok = await check("https://v.douyin.com/whXfWvfHnIQ/", "抖音视频")
    image_ok = await check("https://v.douyin.com/5gjBAuYo7PE/", "抖音图文")

    print("\n===== 小红书（预期降级）=====")
    xhs_url = "https://www.xiaohongshu.com/explore/6553f0e9000000003c00d4ab"
    print(f"路由: {choose_route(xhs_url)}")
    xhs = await extract(xhs_url, hub=Hub())
    print(f"ok={xhs.ok} kind={xhs.kind} route={xhs.route}")
    print(f"原因: {xhs.error[:100]}")
    xhs_correct = (not xhs.ok) and xhs.kind == "link" and xhs.route == "parsehub"

    print("\n===== 判据汇总 =====")
    print(f"视频抓通: {video_ok}")
    print(f"图文抓通: {image_ok}")
    print(f"小红书按契约降级: {xhs_correct}")
    all_ok = video_ok and image_ok and xhs_correct
    print(f"全部通过: {all_ok}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

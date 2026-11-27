"""R3 补充验收：B站、多图图文、内存与耗时。

为什么要单独跑：这三项都是上一轮标为「未验证」的，用户明确要求验掉。
不猜，全部取实测输出。

跑法：proot-distro login debian -- env PYTHONPATH=/opt/kb-app /opt/kb-venv/bin/python /root/verify_extra.py
"""

import asyncio
import json
import resource
import sys
import time

from kb.extract.parsehub_adapter import parse, reset_hub
from kb.extract.router import choose_route, extract


class Hub:
    async def parse(self, url: str) -> object:
        return await parse(url)


def peak_rss_mb() -> float:
    """取本进程峰值内存（Linux ru_maxrss 单位是 KB）。"""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_maxrss / 1024.0


async def timed_extract(url: str, label: str) -> dict:
    """抓一条并计时、记录耗时与内存增量。"""
    print(f"\n===== {label} =====")
    print(f"url: {url}")
    print(f"路由: {choose_route(url)}")

    mem_before = peak_rss_mb()
    t0 = time.monotonic()
    try:
        result = await extract(url, hub=Hub())
    except Exception as exc:  # noqa: BLE001 验收脚本要看到任何异常
        elapsed = time.monotonic() - t0
        print(f"异常: {type(exc).__name__}: {str(exc)[:200]}")
        return {"label": label, "ok": False, "elapsed": elapsed, "error": str(exc)}
    elapsed = time.monotonic() - t0
    mem_after = peak_rss_mb()

    print(f"ok={result.ok} kind={result.kind} route={result.route}")
    print(f"耗时: {elapsed:.2f} 秒")
    print(f"峰值内存: {mem_after:.1f} MB（抓取前 {mem_before:.1f} MB）")

    info: dict = {
        "label": label,
        "ok": result.ok,
        "kind": result.kind,
        "elapsed": elapsed,
        "peak_mb": mem_after,
    }

    if result.ok and result.data:
        d = result.data
        print(f"title: {d.title[:50]}")
        print(f"platform: {d.platform}")
        print(f"media 条数: {len(d.media)}")
        for i, m in enumerate(d.media[:5]):
            print(
                f"  [{i}] role={m.role} ext={m.ext} "
                f"{m.width}x{m.height} duration_sec={m.duration_sec}"
            )
        if len(d.media) > 5:
            print(f"  …另有 {len(d.media) - 5} 条")
        info["media_count"] = len(d.media)
        info["title"] = d.title
    else:
        print(f"降级原因: {result.error[:150]}")
        info["error"] = result.error
    return info


async def main() -> int:
    reset_hub()
    results: list[dict] = []

    # 多图图文（用户提供，据称多图）
    results.append(
        await timed_extract("https://v.douyin.com/ICOA4F0vtTY/", "抖音多图图文")
    )

    # B站（排行榜 API 取到的真实 BV 号）
    for bv in ("BV1esyZz7ExF", "BV1oSyJz1EiC"):
        results.append(
            await timed_extract(f"https://www.bilibili.com/video/{bv}", f"B站 {bv}")
        )

    print("\n" + "=" * 60)
    print("汇总（JSON，便于回贴）")
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

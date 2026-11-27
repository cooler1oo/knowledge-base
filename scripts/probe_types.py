"""R3 探针三：不同类型内容的返回结构对照。

视频已经验过（media 是 dict，含 url/ext/thumb_url/width/height/backup_urls/duration）。
这个探针看图文：type 是什么、media 是 dict 还是 list。

为什么要这一步：写映射代码前必须知道「有几种形状」，否则只会处理视频、
遇到图文就崩。方案 §4.4 写「抖音图文 ✅」，但没说返回结构。
"""

import asyncio
import sys


async def probe(url: str, label: str) -> None:
    from parsehub import ParseHub

    hub = ParseHub()
    print(f"\n===== {label} =====", flush=True)
    print(f"url: {url}", flush=True)
    try:
        result = await hub.parse(url)
    except Exception as exc:  # noqa: BLE001 探针要看原始异常
        print(f"抛异常: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
        return
    if result is None:
        print("返回 None", flush=True)
        return

    data = result.to_dict()
    print(f"result 类型: {type(result).__name__}", flush=True)
    print(f"platform: {data.get('platform')!r}", flush=True)
    print(f"type    : {data.get('type')!r}", flush=True)
    print(f"title   : {(data.get('title') or '')[:70]!r}", flush=True)
    print(f"content : {(data.get('content') or '')[:70]!r}", flush=True)
    print(f"raw_url : {data.get('raw_url')!r}", flush=True)

    media = data.get("media")
    print(f"media 类型: {type(media).__name__}", flush=True)
    if isinstance(media, list):
        print(f"media 长度: {len(media)}", flush=True)
        for i, item in enumerate(media[:3]):
            if isinstance(item, dict):
                print(f"  [{i}] keys={list(item.keys())}", flush=True)
                print(
                    f"      url={str(item.get('url'))[:60]}… "
                    f"ext={item.get('ext')} "
                    f"w={item.get('width')} h={item.get('height')}",
                    flush=True,
                )
            else:
                print(f"  [{i}] {type(item).__name__}: {str(item)[:80]}", flush=True)
    elif isinstance(media, dict):
        print(f"media keys: {list(media.keys())}", flush=True)
        print(f"  ext={media.get('ext')} duration={media.get('duration')}", flush=True)


async def main() -> int:
    targets = [
        (
            "https://www.xiaohongshu.com/explore/6553f0e9000000003c00d4ab",
            "小红书（图文）",
        ),
    ]
    if len(sys.argv) > 1:
        targets = [(sys.argv[1], "命令行指定")]
    for url, label in targets:
        await probe(url, label)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""R3 探针四：抖音图文与视频的字段差异实测。

为什么要单独跑：我在前置探测里写「ImageRef 没有 backup_urls / duration」，
那是读源码推断的。图文实测后发现 media 的 to_dict 里**有** backup_urls，
推断错了，必须用实测订正。

同时验证 duration 只在视频出现、图文的 ext 是图片格式。
"""

import asyncio
import json
import sys

TARGETS = [
    ("https://v.douyin.com/5gjBAuYo7PE/", "抖音图文（用户提供）"),
    ("https://v.douyin.com/whXfWvfHnIQ/", "抖音视频（用户提供）"),
]


async def probe(url: str, label: str) -> dict:
    from parsehub import ParseHub

    print(f"\n{'=' * 62}\n===== {label} =====\n{url}", flush=True)
    hub = ParseHub()
    try:
        result = await hub.parse(url)
    except Exception as exc:  # noqa: BLE001 探针要看原始异常
        print(f"抛异常: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
        return {"error": f"{type(exc).__name__}: {exc}"}

    data = result.to_dict()
    media = data.get("media")

    print(f"result 类 : {type(result).__name__}", flush=True)
    print(f"type      : {data.get('type')!r}", flush=True)
    print(f"media 类型: {type(media).__name__}", flush=True)

    items: list[dict] = []
    if isinstance(media, list):
        items = [m for m in media if isinstance(m, dict)]
    elif isinstance(media, dict):
        items = [media]

    print(f"条目数    : {len(items)}", flush=True)
    all_keys: set[str] = set()
    for i, item in enumerate(items):
        keys = sorted(item.keys())
        all_keys |= set(keys)
        print(f"  [{i}] keys = {keys}", flush=True)
        print(
            f"      ext={item.get('ext')!r} "
            f"w={item.get('width')} h={item.get('height')} "
            f"duration={item.get('duration')!r} "
            f"backup_urls={len(item.get('backup_urls') or [])}",
            flush=True,
        )

    print(f"\n全部字段并集: {sorted(all_keys)}", flush=True)
    print(f"有 duration? {'duration' in all_keys}", flush=True)
    print(f"有 backup_urls? {'backup_urls' in all_keys}", flush=True)

    out = f"/root/probe4_{data.get('type')}.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    print(f"原始数据已存: {out}", flush=True)
    return data


async def main() -> int:
    targets = TARGETS
    if len(sys.argv) > 2:
        targets = [(sys.argv[1], sys.argv[2])]
    for url, label in targets:
        await probe(url, label)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

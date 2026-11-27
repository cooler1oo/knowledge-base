"""R3 探针二：搞清 ParseHub 返回对象的真实结构。

探针一已经证明「能抓通」，但 `to_dict()` 的形状与方案 §4.3 写的契约不一致
（方案写 media 是列表，实际是对象）。这个探针把结构照原样打出来，用于决定
映射代码怎么写，不靠猜。

同时抓一条图文链接做对照——抖音图文的 type 与 media 形状可能不同。
"""

import asyncio
import sys


def describe(name: str, obj: object, depth: int = 0, max_depth: int = 3) -> None:
    pad = "  " * depth
    if depth > max_depth:
        print(f"{pad}{name}: ...（更深略）", flush=True)
        return
    origin = type(obj).__name__
    if isinstance(obj, dict):
        print(f"{pad}{name}: dict({len(obj)}) keys={list(obj.keys())}", flush=True)
        for k, v in obj.items():
            describe(k, v, depth + 1, max_depth)
    elif isinstance(obj, list):
        print(f"{pad}{name}: list({len(obj)})", flush=True)
        if obj:
            describe("[0]", obj[0], depth + 1, max_depth)
    elif isinstance(obj, str):
        short = obj if len(obj) <= 70 else obj[:70] + "…"
        print(f"{pad}{name}: str = {short}", flush=True)
    else:
        print(f"{pad}{name}: {origin} = {obj}", flush=True)


async def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://v.douyin.com/whXfWvfHnIQ/"
    from parsehub import ParseHub

    hub = ParseHub()
    print(f"[探针2] 目标: {url}", flush=True)
    result = await hub.parse(url)
    if result is None:
        print("[探针2] 返回 None", flush=True)
        return 2

    print(f"\n[探针2] result 类型: {type(result).__name__}", flush=True)
    print("[探针2] 公有属性/方法：", flush=True)
    for attr in dir(result):
        if attr.startswith("_"):
            continue
        try:
            value = getattr(result, attr)
        except Exception as exc:  # noqa: BLE001 探针要看属性是否可读
            print(f"  {attr}: <读取失败 {type(exc).__name__}>", flush=True)
            continue
        if callable(value):
            print(f"  {attr}: <方法>", flush=True)
        else:
            print(f"  {attr}: {type(value).__name__}", flush=True)

    print("\n[探针2] to_dict() 结构：", flush=True)
    data = result.to_dict()
    describe("root", data)

    print("\n[探针2] media 的真实类型：", flush=True)
    media = data.get("media")
    print(f"  type(media) = {type(media).__name__}", flush=True)
    if isinstance(media, dict):
        print(f"  media 的键 = {list(media.keys())}", flush=True)
        print(f"  media.ext = {media.get('ext')!r}", flush=True)
        print(f"  media.duration = {media.get('duration')!r}", flush=True)
        print(f"  backup_urls 数量 = {len(media.get('backup_urls') or [])}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""R3 前置探针：ParseHub 到底能不能抓到一条真实抖音链接。

只做一件事——抓，然后原样打印拿到什么。不写产品代码。
判据：title 非空、type 有值、media 有内容。

跑法（平板上）：
proot-distro login debian -- /opt/kb-venv/bin/python /root/probe_parsehub.py <url>
"""

import asyncio
import json
import sys
import traceback


async def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://v.douyin.com/whXfWvfHnIQ/"
    print(f"[探针] 目标: {url}", flush=True)

    from parsehub import ParseHub

    hub = ParseHub()
    try:
        result = await hub.parse(url)
    except Exception as exc:  # noqa: BLE001 探针就是要看原始异常长什么样
        print(f"[探针] 抓取抛异常: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        return 2

    if result is None:
        print("[探针] 返回 None —— 抓到空", flush=True)
        return 3

    print(f"[探针] result 类型: {type(result).__name__}", flush=True)
    try:
        data = result.to_dict()
    except Exception as exc:  # noqa: BLE001 探针要看 to_dict 是否可用
        print(f"[探针] to_dict 失败: {type(exc).__name__}: {exc}", flush=True)
        print(f"[探针] 退化为 repr: {result!r}"[:2000], flush=True)
        return 4

    print("[探针] to_dict 结果：", flush=True)
    print(
        json.dumps(data, ensure_ascii=False, indent=2, default=str)[:4000], flush=True
    )

    # 三条判据
    title = (data or {}).get("title") or ""
    kind = (data or {}).get("type") or ""
    media = (data or {}).get("media") or []
    print("\n[探针] === 判据 ===", flush=True)
    print(f"  title 非空: {bool(title.strip())}  ({title[:50]})", flush=True)
    print(f"  type 有值 : {bool(kind)}  ({kind})", flush=True)
    print(f"  media 数量: {len(media)}", flush=True)
    return 0 if (title.strip() and kind) else 5


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

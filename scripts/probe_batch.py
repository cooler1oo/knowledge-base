"""`INDEX_BATCH` 标定：2000 条文档按 200 / 500 / 1000 三档各灌一遍，取最快的一档。

判据是「不超时且总耗时最短」。脚本只输出实测数，取值写进
`kb/search/config.py` 的 `INDEX_BATCH` 注释（设计 §11 第 6 条）。

跑法（平板容器内）：

```
proot-distro login debian -- bash -lc 'python3 /opt/probe_batch.py'
```
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:7700"
KEY = "atd5HCnkgP1oGUSJeYqene2xNu9fYIlj"
INDEX = "probe_batch"
TOTAL = 2000
BATCHES = (200, 500, 1000)


def call(method: str, path: str, body: object = None) -> tuple[int, object]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def wait_task(uid: int) -> str:
    for _ in range(200):
        _, task = call("GET", f"/tasks/{uid}")
        if isinstance(task, dict) and task.get("status") in {"succeeded", "failed"}:
            return str(task.get("status"))
        time.sleep(0.05)
    return "timeout"


def _docs(start: int, count: int) -> list[dict[str, object]]:
    """造 `count` 条文档，id 从 `start` 起。

    字段长度按**真实抓取结果的中位数**取，不是空串：空文档测出的请求体大小
    会低估一个数量级，据此选出的批量在实际数据上会超时。

    口径来源：R3 真机抓取的公众号/知乎正文中位数约 400 字，摘要约 60 字，
    AI 标签 2–3 个。抖音条目正文短但标题长，取这两类的中间值。
    """
    content = (
        "先把音频波形对齐，再用自动卡点。矩阵营销的三个误区里第一条是买量，"
        "第二条是没有内容供给计划，第三条是把预算全压在一条爆款上。"
    )  # 约 60 字，重复 7 次约 420 字
    summary = "手机端剪映的三个卡点办法，先把音频波形对齐再自动卡点，成片会干净很多。"
    return [
        {
            "id": start + i,
            "title": f"第 {start + i} 条抖音短视频剪辑技巧分享",
            "summary": summary,
            "content_text": content * 7,
            "ocr_text": "",
            "tags": ["视频", "工具", "运营"],
            "author": "王五",
            "kind": "video",
            "read_status": "unread",
            "saved_at": "2026-11-22T10:00:00+08:00",
            "saved_at_ts": 1795312800 + i,
            "source_domain": "v.douyin.com",
            "status": "done",
        }
        for i in range(count)
    ]


def main() -> None:
    print(
        f"{'批量':<8}{'单批字节':<12}{'单次往返':<12}{'请求数':<8}"
        f"{'提交耗时':<10}{'写完耗时':<10}{'备注'}"
    )
    print("-" * 88)
    for size in BATCHES:
        call("DELETE", f"/indexes/{INDEX}")
        status, created = call("POST", "/indexes", {"uid": INDEX, "primaryKey": "id"})
        if isinstance(created, dict):
            wait_task(int(created["taskUid"]))
        call(
            "PATCH",
            f"/indexes/{INDEX}/settings",
            {
                "searchableAttributes": [
                    "title",
                    "summary",
                    "tags",
                    "ocr_text",
                    "content_text",
                    "author",
                ]
            },
        )

        requests = 0
        last_bytes = 0
        last_ms = 0.0
        last_uid = 0
        started = time.monotonic()
        failed = ""
        for start in range(0, TOTAL, size):
            docs = _docs(start, min(size, TOTAL - start))
            payload = json.dumps({"documents": docs}).encode()
            last_bytes = len(payload)
            t0 = time.monotonic()
            status, body = call(
                "POST", f"/indexes/{INDEX}/documents", {"documents": docs}
            )
            last_ms = (time.monotonic() - t0) * 1000
            requests += 1
            if status != 202:
                failed = f"HTTP {status} {body}"
                break
            if isinstance(body, dict):
                last_uid = int(body.get("taskUid", 0))
        # ⚠️ 上一步的 `elapsed` 只量到「Meili 接收完」，不是「写完」——
        # 写作业是异步队列，202 立即返回。真正的口径是等到最后一个任务 succeeded。
        submitted = (time.monotonic() - started) * 1000
        wait_task(last_uid)
        elapsed = (time.monotonic() - started) * 1000
        print(
            f"{size:<8}{last_bytes:<12}{last_ms:<12.0f}{requests:<8}"
            f"{submitted:<10.0f}{elapsed:<10.0f}{failed or 'ok'}"
        )

    # 校验最后灌进去的条数
    _, stats = call("GET", f"/indexes/{INDEX}/stats")
    print()
    print(f"{INDEX} 最终文档数：{stats}")
    call("DELETE", f"/indexes/{INDEX}")


if __name__ == "__main__":
    main()

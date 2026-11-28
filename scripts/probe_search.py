"""R6 前置探测：Meilisearch 中文检索实况。

目的（E-09 同构）：在写任何检索代码之前，先用真 Meili 验证三条假设：
  A1 中文无空格时，二字词能否命中（M6-03 的前提）
  A2 多词查询是否按覆盖度排序（M6-02 的前提）
  A3 summary / tags / ocr_text 是否真的可被搜到（M6-01 五路的前提）

不通过就换实现，不改判据。
"""

import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:7700"
KEY = "atd5HCnkgP1oGUSJeYqene2xNu9fYIlj"
INDEX = "probe_r6"


def call(method: str, path: str, body: object = None) -> tuple[int, object]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def wait_task(uid: int) -> str:
    import time

    for _ in range(60):
        _, task = call("GET", f"/tasks/{uid}")
        if isinstance(task, dict) and task.get("status") in {"succeeded", "failed"}:
            return str(task.get("status")) + " " + str(task.get("error") or "")
        time.sleep(0.3)
    return "timeout"


def main() -> None:
    call("DELETE", f"/indexes/{INDEX}")
    status, created = call("POST", "/indexes", {"uid": INDEX, "primaryKey": "id"})
    print(f"[1] 建索引 {status} {created}")
    wait_task(int(created["taskUid"]))

    settings = {
        "searchableAttributes": [
            "title",
            "summary",
            "content_text",
            "ocr_text",
            "tags",
            "author",
        ],
        "filterableAttributes": ["kind", "tags", "saved_at", "read_status", "status"],
        "sortableAttributes": ["saved_at"],
    }
    status, st = call("PATCH", f"/indexes/{INDEX}/settings", settings)
    print(f"[2] 设索引参数 {status} {st}")
    print("    " + wait_task(int(st["taskUid"])))

    docs = [
        {
            "id": 1,
            "title": "矩阵营销的三个误区",
            "summary": "讲私域流量池怎么建，以及为什么不该一开始就投广告",
            "content_text": "很多人做矩阵号第一件事是买量，其实应该先想清楚内容供给。",
            "ocr_text": None,
            "tags": ["营销", "运营"],
            "author": "张三",
            "kind": "article",
            "saved_at": "2026-11-20T10:00:00+08:00",
            "read_status": "unread",
            "status": "done",
        },
        {
            "id": 2,
            "title": "看完这篇你就懂了三体问题",
            "summary": "科幻小说里的物理设定解读",
            "content_text": "三体问题本质是三个天体在引力下的运动不可长期预测。",
            "ocr_text": None,
            "tags": ["科普"],
            "author": "李四",
            "kind": "article",
            "saved_at": "2026-11-21T10:00:00+08:00",
            "read_status": "read",
            "status": "done",
        },
        {
            "id": 3,
            "title": "抖音短视频剪辑技巧",
            "summary": "手机端剪映的三个卡点办法",
            "content_text": "先把音频波形对齐，再用自动卡点。",
            "ocr_text": "剪映卡点教学",
            "tags": ["视频", "工具"],
            "author": "王五",
            "kind": "video",
            "saved_at": "2026-11-22T10:00:00+08:00",
            "read_status": "unread",
            "status": "done",
        },
        {
            "id": 4,
            "title": "红烧肉的家常做法",
            "summary": "不用炒糖色也能上色",
            "content_text": "五花肉冷水下锅，焯水后煸出油。",
            "ocr_text": None,
            "tags": ["做饭"],
            "author": "赵六",
            "kind": "article",
            "saved_at": "2026-11-23T10:00:00+08:00",
            "read_status": "read",
            "status": "done",
        },
    ]
    status, ad = call("POST", f"/indexes/{INDEX}/documents", docs)
    print(f"[3] 灌 4 条 {status} {ad}")
    print("    " + wait_task(int(ad["taskUid"])))

    cases = [
        ("A1 二字词「营销」", "营销"),
        ("A1 二字词「三体」", "三体"),
        ("A1 二字词「剪辑」", "剪辑"),
        ("A2 多词「抖音 剪辑」", "抖音 剪辑"),
        ("A2 多词「矩阵 营销」", "矩阵 营销"),
        ("A3 只在 summary「私域流量」", "私域流量"),
        ("A3 只在 ocr_text「剪映卡点」", "剪映卡点"),
        ("A3 只在 tags「科普」", "科普"),
        ("A3 只在 author「王五」", "王五"),
        ("A4 不存在的词「量子纠缠」", "量子纠缠"),
    ]
    print()
    print(f"{'用例':<28}{'命中数':<8}{'排序（id:分数）'}")
    print("-" * 70)
    for label, query in cases:
        status, res = call(
            "POST",
            f"/indexes/{INDEX}/search",
            {"q": query, "limit": 10, "showRankingScore": True},
        )
        if not isinstance(res, dict):
            print(f"{label:<28}ERR {status} {res}")
            continue
        hits = res.get("hits", [])
        detail = ", ".join(
            f"{h.get('id')}:{round(float(h.get('_rankingScore', 0)), 3)}" for h in hits
        )
        print(f"{label:<28}{len(hits):<8}{detail or '（无）'}")

    # 筛选器组合（M6-05 的前提）
    print()
    for label, flt in [
        ("筛选 kind=video", 'kind = "video"'),
        ("筛选 tags=营销", 'tags = "营销"'),
        ("筛选 read_status=unread", 'read_status = "unread"'),
        ("组合 kind=article AND tags=科普", 'kind = "article" AND tags = "科普"'),
        ("时间范围 saved_at >= 2026-11-22", 'saved_at >= "2026-11-22"'),
    ]:
        status, res = call(
            "POST", f"/indexes/{INDEX}/search", {"q": "", "limit": 10, "filter": flt}
        )
        if not isinstance(res, dict):
            print(f"{label:<34}ERR {status} {res}")
            continue
        print(f"{label:<34}{[h.get('id') for h in res.get('hits', [])]}")

    call("DELETE", f"/indexes/{INDEX}")
    print("\n[9] 清理完毕")


if __name__ == "__main__":
    main()

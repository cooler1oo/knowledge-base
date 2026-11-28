"""R6 探测：Meilisearch 中文检索实况 + **本轮最终索引设置下的分数标定**。

目的（E-09 同构）：在写检索代码之前与之后，用真 Meili 验证假设。
**不通过就换实现，不改判据。**

| 组 | 验什么 | 前提是什么 |
|---|---|---|
| A1 | 中文二字词能否命中（M6-03） | 检索要能凭两个汉字找到东西 |
| A2 | 多词是否按覆盖度排序（M6-02） | 「抖音 剪辑」不该输给只含一个词的 |
| A3 | summary / tags / ocr_text / author 是否可搜（M6-01 五路） | 缺一路就是功能级缺口 |
| A4 | 不存在的词返回 0 条 | 唯一能抓住「q 被丢掉、退化成列表接口」的用例 |
| **A5** | `rankingScoreThreshold` 是否生效 + **四个分数一起记录** | 分数下限要靠真机标定，不能拍 |
| **A6** | 数值型 epoch 秒做时间范围筛选 | 字符串比较是字典序，带时区偏移会静默错 |
| **A7** | 中英混排标题里英文词是否可搜 | 决定 `localizedAttributes` 要不要设 |

## A5 为什么是四个数

阈值必须同时满足 `垃圾分 < 阈值 < min(标签分, 正文分)`，**只满足一侧就是错的**：

```
垃圾分  <  阈值  <  min(标签单独命中分, 正文单独命中分)
0.285   <  0.30  <  min(0.667, 正文分)
```

只盯标签分不够：`searchableAttributes` 从「title, summary, content_text,
ocr_text, tags, author」改成「title, summary, tags, ocr_text, content_text,
author」后，**标签升权（第 5 → 第 3）、正文降权（第 3 → 第 5）**。只在
`content_text` 里命中的文档分数会低于原顺序下的值。若它掉到 0.30 以下，
用户搜正文里的词就搜不到——那是功能级故障，不是调参问题。

## 跑在哪

**必须在平板容器内跑**（`127.0.0.1:7700` 从 Windows 不可达）：

```
ssh -i ~/.ssh/kb_y900_ed25519 -p 8022 u0_a201@100.124.156.47 \
  "proot-distro login debian -- bash -lc 'python3 /opt/probe_search.py'"
```

不通过就把脚本与输出一起放进交接包，不改判据。
"""

from __future__ import annotations

import json
import statistics
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:7700"
KEY = "atd5HCnkgP1oGUSJeYqene2xNu9fYIlj"
INDEX = "probe_r6"

#: 被标定的阈值。与 `kb/search/config.py` 的 `MIN_SCORE` 必须一致——
#: 本脚本`--check` 模式下若不相等会显式报出来（两处不一致时，标定结论作废）。
THRESHOLD = 0.30

_settings: dict[str, object] = {
    # **本轮的最终顺序**（设计 §3.2）。与旧探测相比改了两处：
    # `tags` 从第 5 提到第 3（标签升权）、`content_text` 从第 3 降到第 5（正文降权）。
    "searchableAttributes": [
        "title",
        "summary",
        "tags",
        "ocr_text",
        "content_text",
        "author",
    ],
    "filterableAttributes": ["kind", "tags", "read_status", "saved_at_ts"],
    "sortableAttributes": ["saved_at_ts"],
    "pagination": {"maxTotalHits": 1000},
}


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
    for _ in range(80):
        _, task = call("GET", f"/tasks/{uid}")
        if isinstance(task, dict) and task.get("status") in {"succeeded", "failed"}:
            return str(task.get("status")) + " " + str(task.get("error") or "")
        time.sleep(0.25)
    return "timeout"


def _docs() -> list[dict[str, object]]:
    """5 条探测文档。

    比旧版多的是第 5 条：`title` / `summary` / `tags` / `ocr_text` 都不含
    「挂耳咖啡」，只有 `content_text` 含它。这条不存在的后果是
    **A5 的「正文单独命中分」这一格永远是空的**，而它是「阈值会不会截掉
    正文命中」这个判断的唯一依据。
    """
    return [
        {
            "id": 1,
            "title": "矩阵营销的三个误区",
            "summary": "讲私域流量池怎么建，以及为什么不该一开始就投广告",
            "content_text": "很多人做矩阵号第一件事是买量，其实应该先想清楚内容供给。",
            "ocr_text": "",
            "tags": ["营销", "运营"],
            "author": "张三",
            "kind": "article",
            "saved_at": "2026-11-20T10:00:00+08:00",
            "saved_at_ts": 1795140000,
            "read_status": "unread",
            "status": "done",
        },
        {
            "id": 2,
            "title": "看完这篇你就懂了三体问题",
            "summary": "科幻小说里的物理设定解读",
            "content_text": "三体问题本质是三个天体在引力下的运动不可长期预测。",
            "ocr_text": "",
            "tags": ["科普"],
            "author": "李四",
            "kind": "article",
            "saved_at": "2026-11-21T10:00:00+08:00",
            "saved_at_ts": 1795226400,
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
            "saved_at_ts": 1795312800,
            "read_status": "unread",
            "status": "done",
        },
        {
            "id": 4,
            "title": "红烧肉的家常做法",
            "summary": "不用炒糖色也能上色",
            "content_text": "五花肉冷水下锅，焯水后煸出油。",
            "ocr_text": "",
            "tags": ["做饭"],
            "author": "赵六",
            "kind": "article",
            "saved_at": "2026-11-23T10:00:00+08:00",
            "saved_at_ts": 1795399200,
            "read_status": "read",
            "status": "done",
        },
        {
            # A5 的「正文单独命中」样本。四个可搜字段里只有 content_text 含关键词。
            "id": 5,
            "title": "随手记一条",
            "summary": "",
            "content_text": "今天试了下挂耳咖啡的冲泡比例，一比十五比一次注水好喝。",
            "ocr_text": "",
            "tags": [],
            "author": "",
            "kind": "text",
            "saved_at": "2026-11-24T10:00:00+08:00",
            "saved_at_ts": 1795485600,
            "read_status": "unread",
            "status": "done",
        },
        {
            # A7 的样本：中英混排标题。决定 `localizedAttributes` 要不要设。
            "id": 6,
            "title": "Figma 插件推荐",
            "summary": "设计师常用的十个插件",
            "content_text": "自动布局之后再看插件会更省事。",
            "ocr_text": "",
            "tags": ["设计"],
            "author": "",
            "kind": "article",
            "saved_at": "2026-11-25T10:00:00+08:00",
            "saved_at_ts": 1795572000,
            "read_status": "unread",
            "status": "done",
        },
    ]


def search(
    query: str,
    *,
    limit: int = 10,
    threshold: float | None = None,
    filter_expr: str = "",
    sort: list[str] | None = None,
) -> dict[str, object]:
    """发一次搜索，返回完整响应体（含 `processingTimeMs`）。"""
    payload: dict[str, object] = {
        "q": query,
        "limit": limit,
        "showRankingScore": True,
    }
    if threshold is not None:
        payload["rankingScoreThreshold"] = threshold
    if filter_expr:
        payload["filter"] = filter_expr
    if sort:
        payload["sort"] = sort
    status, res = call("POST", f"/indexes/{INDEX}/search", payload)
    if not isinstance(res, dict):
        raise RuntimeError(f"搜索失败 {status}: {res}")
    return res


def _score_line(res: dict[str, object]) -> str:
    """`id:分数` 列表 + 本次 `processingTimeMs`。"""
    hits = res.get("hits", [])
    assert isinstance(hits, list)
    detail = ", ".join(
        f"{h.get('id')}:{round(float(h.get('_rankingScore', 0)), 3)}" for h in hits
    )
    ms = res.get("processingTimeMs", "?")
    return f"{len(hits):<4}{detail or '（无）':<44} {ms}ms"


#: 收集每次搜索的 `processingTimeMs`，用于算 P95 并校准 `SEARCH_TIMEOUT_SEC`。
#: 模块级列表而不是返回值传递：`run_case` 是打印用的便捷函数，
#: 让它多返回一个耗时会让每个调用点都要接一次。
_timings: list[float] = []


def run_case(label: str, query: str, **kwargs: object) -> dict[str, object]:
    res = search(query, **kwargs)  # type: ignore[arg-type]
    ms = res.get("processingTimeMs")
    if isinstance(ms, (int, float)):
        _timings.append(float(ms))
    print(f"{label:<34}{_score_line(res)}")
    return res


def main() -> int:
    print(f"Meili 版本：{call('GET', '/version')[1]}")
    print(f"阈值 THRESHOLD = {THRESHOLD}")
    print()

    call("DELETE", f"/indexes/{INDEX}")
    status, created = call("POST", "/indexes", {"uid": INDEX, "primaryKey": "id"})
    print(f"[1] 建索引 {status} {created}")
    if not isinstance(created, dict):
        print("建索引失败，后续全部无意义。先确认 Meili 在跑且 KEY 正确。")
        return 1
    wait_task(int(created["taskUid"]))

    status, st = call("PATCH", f"/indexes/{INDEX}/settings", _settings)
    print(f"[2] 设索引参数（本轮最终顺序）{status}")
    if isinstance(st, dict):
        print("    " + wait_task(int(st["taskUid"])))

    docs = _docs()
    status, ad = call("POST", f"/indexes/{INDEX}/documents", docs)
    print(f"[3] 灌 {len(docs)} 条 {status}")
    if isinstance(ad, dict):
        print("    " + wait_task(int(ad["taskUid"])))

    print()
    print(f"{'用例':<34}{'命中':<4}{'排序（id:分数）':<44} 耗时")
    print("-" * 96)

    # ── A1 / A2 / A3 / A4：与旧版一致的 10 例 ──────────────────
    run_case("A1 二字词「营销」", "营销")
    run_case("A1 二字词「三体」", "三体")
    run_case("A1 二字词「剪辑」", "剪辑")
    run_case("A2 多词「抖音 剪辑」", "抖音 剪辑")
    run_case("A2 多词「矩阵 营销」", "矩阵 营销")
    run_case("A3 只在 summary「私域流量」", "私域流量")
    run_case("A3 只在 ocr_text「剪映卡点」", "剪映卡点")
    run_case("A3 只在 tags「科普」", "科普")
    run_case("A3 只在 author「王五」", "王五")
    run_case("A4 不存在的词「量子纠缠」", "量子纠缠")
    run_case("A3 只在 content_text「挂耳咖啡」", "挂耳咖啡")

    # ── A5：分数下限的四个数 + 截断生效 ────────────────────────
    print()
    print("A5 · 分数标定（阈值必须同时满足 垃圾分 < 阈值 < min(标签分, 正文分)）")
    print("-" * 96)

    garbage = run_case("A5-① 垃圾召回「三体」(不截断)", "三体", threshold=None)
    garbage_hits = garbage.get("hits", [])
    assert isinstance(garbage_hits, list)
    garbage_scores = sorted(float(h.get("_rankingScore", 0)) for h in garbage_hits)
    garbage_score = garbage_scores[0] if len(garbage_scores) > 1 else 0.0

    tag_only = run_case("A5-② 标签单独命中「科普」", "科普", threshold=None)
    tag_hits = tag_only.get("hits", [])
    assert isinstance(tag_hits, list)
    tag_score = float(tag_hits[0].get("_rankingScore", 0)) if tag_hits else float("nan")

    body_only = run_case("A5-③ 正文单独命中「挂耳咖啡」", "挂耳咖啡", threshold=None)
    body_hits = body_only.get("hits", [])
    assert isinstance(body_hits, list)
    body_score = (
        float(body_hits[0].get("_rankingScore", 0)) if body_hits else float("nan")
    )

    truncated = run_case(
        f"A5-④ 「三体」+ 阈值 {THRESHOLD}", "三体", threshold=THRESHOLD
    )
    trunc_hits = truncated.get("hits", [])
    assert isinstance(trunc_hits, list)

    print()
    print("A5 的四个数（贴进 config.py 的 MIN_SCORE 注释与交接包）：")
    print(f"    垃圾召回分（正文里的「三个误区」）: {garbage_score:.3f}")
    print(f"    标签单独命中分（tags 第 3 位）    : {tag_score:.3f}")
    print(f"    正文单独命中分（content_text 第5位）: {body_score:.3f}")
    print(f"    阈值                              : {THRESHOLD:.3f}")
    print()

    ok_threshold = True
    if garbage_score and not (garbage_score < THRESHOLD):
        print(f"  ✗ 垃圾分 {garbage_score:.3f} 不高于阈值 → 截不住噪音")
        ok_threshold = False
    upper = min(x for x in (tag_score, body_score) if x == x)  # NaN 视为缺失
    if upper == upper and not (THRESHOLD < upper):
        print(
            f"  ✗ 阈值 {THRESHOLD:.3f} 不低于 min(标签, 正文)={upper:.3f} → 会截掉真命中"
        )
        ok_threshold = False
    if body_score != body_score:
        print(
            "  ⚠️ 拿不到正文单独命中分（0 命中或分数为 null）。"
            "按设计 §5.4 的降级处置：MIN_SCORE 保守设为 0.05，并记入待明确事项。"
        )
        ok_threshold = False

    # 截断是否真的生效：不截断时 2 条，加阈值后只剩 1 条
    if len(garbage_hits) >= 2 and len(trunc_hits) != 1:
        print(
            f"  ✗ rankingScoreThreshold 未按预期生效："
            f"不截断 {len(garbage_hits)} 条，截断后 {len(trunc_hits)} 条（应 1 条）。"
            "处置：改为在 service.py 里按 _rankingScore 客户端过滤（设计 §11 第 1 条）"
        )
        ok_threshold = False
    elif len(trunc_hits) == 1:
        print(f"  ✓ 阈值生效：「三体」从 {len(garbage_hits)} 条截到 1 条")

    # ── A6：数值型 epoch 秒的时间范围筛选 ─────────────────────
    print()
    print("A6 · 数值型时间筛选（saved_at_ts，epoch 秒）")
    print("-" * 96)

    # 2026-11-22 北京时间 00:00 = 1795276800；11-23 00:00 = 1795363200
    day_start = 1795276800
    next_day = 1795363200
    in_day = search(
        "",
        limit=10,
        filter_expr=f"saved_at_ts >= {day_start} AND saved_at_ts < {next_day}",
    )
    in_hits = in_day.get("hits", [])
    assert isinstance(in_hits, list)
    ids_in_day = sorted(int(str(h.get("id"))) for h in in_hits)
    print(f"{'A6-① 11-22 当天（半开区间）':<34}{ids_in_day}")
    if ids_in_day == [3]:
        print("  ✓ 只有 11-22 那条（id=3），11-23 00:00 那条没被装进来")
    else:
        print(f"  ✗ 期望 [3]，实际 {ids_in_day}")

    since = search("", limit=10, filter_expr=f"saved_at_ts >= {next_day}")
    since_hits = since.get("hits", [])
    assert isinstance(since_hits, list)
    ids_since = sorted(int(str(h.get("id"))) for h in since_hits)
    print(f"{'A6-② saved_at_ts >= 11-23 00:00':<34}{ids_since}")
    # 期望 [4, 5, 6]：11-23、11-24、11-25 三条。**11-22 那条（id=3）必须不在**——
    # 这一条才是「半开区间」的判据；把 11-23 和 11-22 混在一起验会看不出边界对不对。
    if ids_since == [4, 5, 6]:
        print("  ✓ 含 11-23 / 11-24 / 11-25 三条，不含 11-22 那条")
    else:
        print(f"  ✗ 期望 [4, 5, 6]，实际 {ids_since}")

    sorted_res = search(
        "",
        limit=10,
        filter_expr=f"saved_at_ts >= {day_start}",
        sort=["saved_at_ts:desc"],
    )
    sorted_hits = sorted_res.get("hits", [])
    assert isinstance(sorted_hits, list)
    order = [int(str(h.get("id"))) for h in sorted_hits]
    print(f"{'A6-③ 按 saved_at_ts 倒序':<34}{order}")
    if order == [6, 5, 4, 3]:
        print("  ✓ 倒序正确（6 → 5 → 4 → 3）")
    else:
        print(f"  ✗ 期望 [6, 5, 4, 3]，实际 {order}")

    # ── A7：中英混排里的英文词 ─────────────────────────────────
    print()
    print("A7 · 中英混排（决定 localizedAttributes 要不要设）")
    print("-" * 96)
    for label, word, expect_hit in [
        ("A7-① 英文词 figma", "figma", True),
        ("A7-② 中文词 插件", "插件", True),
        ("A7-③ 大小写 Figma", "Figma", True),
    ]:
        res = run_case(label, word)
        hits = res.get("hits", [])
        assert isinstance(hits, list)
        got = any(int(str(h.get("id"))) == 6 for h in hits)
        mark = "✓" if got == expect_hit else "✗"
        print(f"     {mark} id=6 {'命中' if got else '未命中'}")
        if not got and word.lower() == "figma":
            print(
                "       若 ① 未命中而 ② 命中：设 localizedAttributes 前先复跑 A7 全组，"
                "按设计 §11 第 3 条只在真的红时才开"
            )

    # ── 筛选器组合 ─────────────────────────────────────────────
    print()
    print("筛选器组合（M6-05 的前提）")
    print("-" * 96)
    for label, flt in [
        ('kind = "video"', 'kind = "video"'),
        ('tags = "营销"', 'tags = "营销"'),
        ('read_status = "unread"', 'read_status = "unread"'),
        ('kind = "article" AND tags = "科普"', 'kind = "article" AND tags = "科普"'),
        (
            "tags AND 两维（营销 + 运营）",
            'tags = "营销" AND tags = "运营"',
        ),
    ]:
        status, res = call(
            "POST",
            f"/indexes/{INDEX}/search",
            {"q": "", "limit": 10, "filter": flt},
        )
        if not isinstance(res, dict):
            print(f"  {label:<40}ERR {status} {res}")
            continue
        ids = [h.get("id") for h in res.get("hits", [])]
        print(f"  {label:<40}{ids}")

    # ── processingTimeMs 的 P95 ────────────────────────────────
    print()
    print("processingTimeMs 统计（用于校准 SEARCH_TIMEOUT_SEC）")
    print("-" * 96)
    if _timings:
        p95 = (
            statistics.quantiles(_timings, n=20)[-1]
            if len(_timings) >= 20
            else max(_timings)
        )
        print(
            f"    样本 {len(_timings)} 个，max={max(_timings):.0f}ms，P95≈{p95:.0f}ms"
        )
        print(
            f"    P95 × 3 = {p95 * 3:.0f}ms；当前 SEARCH_TIMEOUT_SEC = 3000ms；"
            f"{'无需上调' if p95 * 3 <= 3000 else '需上调（设计 §11 第 5 条）'}"
        )
        print(
            "    ⚠️ 6 条文档下这个数不代表 1000 条量级的 P95，"
            "只作为「没有明显异常」的证据。"
        )

    call("DELETE", f"/indexes/{INDEX}")
    print()
    print("[9] 清理完毕")
    print()
    print(
        "结论：",
        "A5 阈值判据全部满足"
        if ok_threshold
        else "A5 阈值判据**未**全部满足，见上面的 ✗ / ⚠️",
    )
    return 0 if ok_threshold else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""检索层的全部数值常量与环境变量名。**一处定义，其余文件只 import。**

## 为什么常量必须集中一处

设计 §10 把「索引 uid 只在 `config.py` 出现一次」定为硬约定，理由是排障时
要能一眼看到「这套代码认的索引叫什么」。散落各处的字面量在改名时必然漏一处，
而漏掉的那一处不会报错——它只是把文档写进了另一个索引，然后搜索页永远少几条。
`tests/test_search_arch.py` 会逐文件数这个字符串，第二处出现就红。

## 为什么环境变量名也在这里

worker 与 api 必须读**同一套**变量名。`kb/api/app.py` 的 `KB_MEILI_URL` 与
`kb/worker` 侧的默认值如果各写一份，默认值改一处就会出现
「api 连 A、worker 连 B」，两边都健康但索引写进去搜不到。
"""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

# ── 索引 ────────────────────────────────────────────────────────

#: 索引 uid。**这个字符串在本包内只允许出现这一次**（设计 §10）。
INDEX_UID = "kb_items"

#: 主键字段名。值 = `items.id`，所以 upsert 天然幂等。
INDEX_PRIMARY_KEY = "id"

# ── 环境变量 ────────────────────────────────────────────────────

MEILI_URL_ENV = "KB_MEILI_URL"
MEILI_KEY_ENV = "KB_MEILI_KEY"

#: 与 `kb/api/app.py` 的既有默认值一致（设计 §10「环境变量」）。
DEFAULT_MEILI_URL = "http://127.0.0.1:7700"

# ── 分数下限 ────────────────────────────────────────────────────

#: 相关度下限。低于它的命中被 Meili 服务端截断（`rankingScoreThreshold`）。
#:
#: ## 标定依据（A5 实测，2026-11-28，平板真机 Meili 1.58.0，`scripts/probe_search.py`）
#:
#: 阈值必须同时满足 `垃圾分 < 阈值 < min(标签分, 正文分)`，只满足一侧就是错的：
#:
#: | 样本 | 分数 | 作用 |
#: |---|---|---|
#: | 「三体」的垃圾召回（正文里的「三个误区」） | **0.285** | 必须截掉 → 阈值下界 |
#: | 「科普」只命中 `tags`（第 3 位字段） | **0.833** | 必须保留 → 阈值上界 |
#: | 「挂耳咖啡」只命中 `content_text`（第 5 位字段） | **0.991** | 必须保留 → 阈值上界 |
#:
#: `0.285 < 0.30 < min(0.833, 0.991) = 0.833`，两个边界都满足。
#: 同一次探测的另一条判据也过了：`q=三体` 不截断 2 条、加 `rankingScoreThreshold=0.30`
#: 后剩 1 条（0.285 那条被截），证明该参数在 1.58.0 上按文档生效。
#:
#: ## 这两个数为什么不是设计阶段记的 0.667 / 未知
#:
#: 设计 §5.4 记的 0.667 是**旧字段顺序**（`tags` 在第 5 位）下的标签分。
#: §3.2 把 `tags` 提到第 3 位之后标签升权，实测升到 0.833；正文从第 3 位
#: 降到第 5 位的代价没有出现——正文单独命中仍有 0.991，因为该文档没有别的
#: 字段与关键词竞争（`min(标签, 正文)` 这一侧在本轮样本上没有收紧）。
#:
#: ## 为什么不是 0（不截断）
#:
#: 取消截断会把 0.285 那条误召回放回结果里，等于用「多几条噪音」换「不漏正文」，
#: 方向反了（设计 §5.4）。
MIN_SCORE = 0.30

# ── 批量与超时 ──────────────────────────────────────────────────

#: 单次写索引的文档条数。
#:
#: ## 实测（2026-11-28，平板真机 Meili 1.58.0，`scripts/probe_batch.py`）
#:
#: 2000 条文档按 200 / 500 / 1000 三档各灌一遍。文档字段长度按 R3 真机抓取
#: 的中位数造（正文约 420 字、摘要 37 字、3 个标签），不是空串——
#: 空文档测出的请求体大小会低估一个数量级。
#:
#: | 批量 | 单批字节 | 单次往返 | 请求数 | 提交完 2000 条 | 全部写完 |
#: |---|---|---|---|---|---|
#: | **200** | 644KB | 12ms | 10 | 180ms | **241ms** |
#: | 500 | 1.61MB | 46ms | 4 | 240ms | 324ms |
#: | 1000 | 3.22MB | 48ms | 2 | 181ms | 273ms |
#:
#: 200 档**写完耗时最短**，且单批请求体最小——写作业失败重传的代价、进程内存
#: 峰值都是最小的那一档。500/1000 档减少请求次数并没有换来更快：Meili 内部
#: 按任务队列串行处理，请求大小不改变总处理量，只改变单次阻塞时长。
INDEX_BATCH = 200

#: 一次搜索请求的总超时。取「用户等搜索结果的可接受上限」，不是实测值：
#: 真机 `processingTimeMs` 在 6 条文档下 max=2ms（18 次样本），
#: 这个量级标不出有意义的 P95（0 秒级全是噪声）。
#: 校准方法写进设计 §11 第 5 条：1000 条量级复跑后取 P95，若 `P95 × 3 > 3 秒` 再上调。
SEARCH_TIMEOUT_SEC = 3.0

#: 等一个 Meili 任务（改设置 / 建索引）完成的上限。
#: 只有这两类操作等任务；写文档不等（`add_documents` 拿到 `taskUid` 就算接收）。
TASK_WAIT_SEC = 10.0

#: 熔断时长。确认检索服务挂了之后，这段时间内直接走兜底，
#: 不再每搜一次白等 3 秒超时。
CIRCUIT_TTL_SEC = 30.0

# ── index 步的等待与推迟 ────────────────────────────────────────

#: 上游有阻塞时，index 步推迟多久再试。30 秒的依据：抓取 10–20 秒、
#: AI 调用 3–10 秒，30 秒后上游的典型状态是已完成。
UPSTREAM_WAIT_SEC = 30.0

#: 只等「90 秒内就要到期」的上游（设计 §4.2）。被 `defer` 到 30 分钟后的
#: `summarize` 不阻塞索引，否则 AI 未配置时新内容半小时搜不到。
UPSTREAM_SOON_SEC = 90.0

#: 检索服务不可达时推迟多久。比 `UPSTREAM_WAIT_SEC` 长一倍：
#: 重启 Meili 不是秒级的事，60 秒内反复重试只会把日志刷满。
DEFER_MEILI_DOWN_SEC = 60.0

#: 检索服务 401/403 时推迟多久。**不判死**（设计 §4.4）：
#: 密钥填错是可修的配置问题，判死之后要手工改库才能恢复。
DEFER_MEILI_UNAUTHORIZED_SEC = 600.0

# ── 查询与分页 ──────────────────────────────────────────────────

#: 查询文本上限。Meili 只看前 10 个词，超长部分是噪音（设计 §5.3）。
MAX_QUERY_LEN = 64

#: 兜底 SQL 的词块上限。每个词展开成 5 个 `ilike`，5 个词就是 25 个条件；
#: 再多会把 Seq Scan 的表达式求值成本推上去，而第 6 个词的边际收益接近 0。
MAX_TERMS = 5

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MAX_OFFSET = 999

# ── 时区（设计 §3.9）──────────────────────────────────────────

#: `from` / `to` 里「日期」部分的解释时区。**只出现在本文件一次**。
#:
#: 三条依据：① 用户在中国；② 平板 PG 的 `TimeZone` 实测是 `PRC`
#: （`kb/worker/loop.py` 的 `_db_now` docstring 留着原始输出）；
#: ③ 技术方案 §11.2 的 T19 已定「存 UTC，按 Asia/Shanghai 切今天」。
#: 检索是「切今天」的第二个使用点，必须与第一处同口径。
DISPLAY_TZ = "Asia/Shanghai"

#: `fallback` 模式里 `field_hits` 的分母：可命中的字段一共 5 个
#: （title / summary / content_text / ocr_text / tags）。API 出的
#: `rankingScore = field_hits / 5`，取值范围与 Meili 的 `_rankingScore` 一致。
FALLBACK_FIELD_COUNT = 5


def display_tz() -> ZoneInfo:
    """`DISPLAY_TZ` 的 `ZoneInfo` 实例。

    **不让调用方自己 `ZoneInfo("Asia/Shanghai")`**：那样字面量会出现第二处，
    而 `tests/test_search_arch.py` 只允许它在 `config.py` 里。
    缺 tzdata 的环境里 `ZoneInfo` 会抛 `ZoneInfoNotFoundError`，
    直接暴露比退回 UTC 好——退回 UTC 会让「按日期筛」静默少 8 小时的内容。
    """
    return ZoneInfo(DISPLAY_TZ)


def meili_url() -> str:
    """检索服务地址。默认值与 `kb/api/app.py` 的 `AppConfig.from_env` 完全一致。"""
    return os.environ.get(MEILI_URL_ENV, DEFAULT_MEILI_URL)


def meili_key() -> str:
    """检索服务密钥。默认空串 = 无鉴权（本机开发时的 Meili 就是这样）。"""
    return os.environ.get(MEILI_KEY_ENV, "")


#: `searchableAttributes` 的顺序**就是权重序**：Meili 的 `attributeRank`
#: 按数组位置排序，第 1 位命中的文档排在只命中第 5 位的前面。
#:
#: 顺序照技术方案 §6.7 定的五路（标题 > 摘要 > 标签 > OCR > 正文），
#: 尾部补 `author`。**不要把 `content_text` 提前**：正文动辄上千字，
#: 提前会让「正文里偶然出现这个词」压过「标题就是这个意思」。
SEARCHABLE_ATTRIBUTES: tuple[str, ...] = (
    "title",
    "summary",
    "tags",
    "ocr_text",
    "content_text",
    "author",
)

#: 筛选字段。`saved_at_ts` 是整数 epoch 秒——Meili 的字符串比较是字典序，
#: 带时区偏移的 ISO 串在同一时刻会给出不同答案（设计 §3.1）。
FILTERABLE_ATTRIBUTES: tuple[str, ...] = (
    "kind",
    "tags",
    "read_status",
    "saved_at_ts",
)

SORTABLE_ATTRIBUTES: tuple[str, ...] = ("saved_at_ts",)

#: 返回给调用方的字段。**不含 `content_text` / `ocr_text`**：
#: 列表页不需要正文全文，不含它们让每条命中少传几 KB。
DISPLAYED_ATTRIBUTES: tuple[str, ...] = (
    "id",
    "title",
    "summary",
    "tags",
    "kind",
    "read_status",
    "saved_at",
    "saved_at_ts",
    "source_domain",
)

#: Meili 单次搜索能返回的最大命中总数（`pagination.maxTotalHits`）。
#:
#: ## 必须 ≥ `MAX_OFFSET + MAX_LIMIT`
#:
#: 这个数与下面两个分页常量是**一组**，三者互相约束。第一版各写一个值
#: （999 / 50 / 1000），`offset=999&limit=50` 要求返回第 1000–1049 条，
#: 而 `maxTotalHits=1000` 会把越界部分**静默截掉**——用户看到「翻到 1000 条
#: 之后就没有了」，且没有任何提示说明是被上限切断的。QA 抓到这条。
#:
#: 取 1050（= 999 + 50 + 1 的整数化）而不是把 `MAX_OFFSET` 降到 950：
#: **降 offset 上限是无理由砍掉用户能翻到的范围**，而 Meili 多存 50 条的
#: 内存代价可以忽略。上界由 `tests/test_search_query.py` 的分页常量用例
#: 校验（`assert MAX_OFFSET + MAX_LIMIT <= MAX_TOTAL_HITS`），改任何一个
#: 而不同步其余两个就会立刻红。
#:
#: 个人自用库远低于这个量级；设上限是为了让 Meili 的内存占用可预测。
MAX_TOTAL_HITS = 1050


def index_settings() -> dict[str, object]:
    """`indexer.ensure_index` 一次 PATCH 的设置体（设计 §3.2）。

    **不设 `rankingRules`**：显式写在文档里的规则名会随版本改名
    （1.58 的默认列表已经出现 `attributeRank` / `wordPosition` 这两个新名）。
    用默认值 + `scripts/probe_search.py` 守住排序行为，比锁死名字更抗升级。

    **不设 `localizedAttributes`**：真机探测已证明自动识别把中文交给
    Charabia 的 jieba 词典分词（二字词命中 0.98–1.0）。强制 `locales: ["cmn"]`
    会把该字段的英文内容也按中文切，中英混排反而更差。

    **不设 `matchingStrategy`**：默认的 `last` 在命中不足 `limit` 时从末尾
    逐词放宽，这是「凭印象少一个字也能搜到」的机制来源；改成 `all` 会让
    「抖音 剪辑 卡点」这种多一个词的查询直接 0 结果（设计 §5.2）。
    """
    return {
        "searchableAttributes": list(SEARCHABLE_ATTRIBUTES),
        "filterableAttributes": list(FILTERABLE_ATTRIBUTES),
        "sortableAttributes": list(SORTABLE_ATTRIBUTES),
        "displayedAttributes": list(DISPLAYED_ATTRIBUTES),
        "pagination": {"maxTotalHits": MAX_TOTAL_HITS},
    }

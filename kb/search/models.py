"""检索层的契约类型：索引文档、查询、命中、降级模式、异常族。

**这里是 `kb/search/` 与外部世界唯一的类型约定**（设计 §3.1 / §3.3 / §3.4 / §3.6）。
调用方只认这些 dataclass 与异常，不认 HTTP 细节、不认 SQL。

## 三条硬约定

1. **索引内字段 snake_case，与 DB 列同名。** rebuild 时逐条对账不用做名字映射；
   camelCase 只在 `kb/api/search.py` 一处生成。
2. **`saved_at` 与 `saved_at_ts` 同时存。** 前者供展示（带时区的 ISO 串），
   后者供筛选与排序（epoch 秒整数）。Meili 的字符串比较是字典序，
   `"2026-11-22T02:00:00+00:00"` 与 `"2026-11-22T10:00:00+08:00"` 是同一时刻
   但逐字比较给出相反答案——范围筛选会静默错。
3. **三个异常分开。** 它们的处置完全不同（defer 60s / defer 600s / mark_failed），
   合并成一个之后每个调用点都要自己判状态码，判错一次就是「配置错误被判死」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .config import MIN_SCORE


class SearchMode(str, Enum):
    """这次结果是在哪种质量下得到的。**必须透出到 API**（设计 §3.7）。

    继承 `str` 是为了 `json.dumps` 能直接序列化——`SearchMode.MEILI` 的
    JSON 表达就是 `"meili"`，不需要在每个序列化点写 `.value`。
    """

    MEILI = "meili"
    FALLBACK = "fallback"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class IndexDoc:
    """一条要被写进 Meili 的文档。字段集合与设计 §3.1 的表逐字一致。

    字段顺序有意义：`tests/test_search_arch.py` 断言的是一个**有序**元组，
    改顺序等于改契约（虽然 Meili 不在意顺序，但「字段集合悄悄多一个」这类
    漂移只有逐字比对才抓得住）。

    | 字段 | 可搜权重 | 可筛选 | 可排序 |
    |---|---|---|---|
    | `title` | 1（最高） | — | — |
    | `summary` | 2 | — | — |
    | `tags` | 3 | 数组等值 | — |
    | `ocr_text` | 4 | — | — |
    | `content_text` | 5 | — | — |
    | `author` | 6（最低） | — | — |
    | `kind` / `read_status` | — | 等值 | — |
    | `saved_at_ts` | — | 范围 | ✅ |
    """

    id: int
    title: str
    summary: str
    tags: tuple[str, ...]
    ocr_text: str
    content_text: str
    author: str
    kind: str
    read_status: str
    saved_at: str
    saved_at_ts: int
    source_domain: str

    def to_meili_dict(self) -> dict[str, object]:
        """翻成 Meili 的请求体。

        `tags` 出**列表**而不是元组：`json.dumps` 不认元组（会抛
        `TypeError: Object of type tuple is not JSON serializable`），
        而这条转换放在这里而不是每个调用点，才不会有一处忘了转。

        `summary` / `ocr_text` / `author` 的空值统一出空串而不是 `None`：
        Meili 对 `null` 字段的处理是「该字段不存在」，与「该字段为空串」在
        `displayedAttributes` 上的表现不同（前者整键消失，前端要判 `undefined`）。
        """
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "tags": list(self.tags),
            "ocr_text": self.ocr_text,
            "content_text": self.content_text,
            "author": self.author,
            "kind": self.kind,
            "read_status": self.read_status,
            "saved_at": self.saved_at,
            "saved_at_ts": self.saved_at_ts,
            "source_domain": self.source_domain,
        }


@dataclass(frozen=True)
class SearchFilter:
    """筛选条件。**维度内怎么连**是语义决定，不是实现细节。

    - `kinds` 维度内 **OR**：勾两个类型 = 「视频或文章都看」
    - `tags` 维度内 **AND**：勾两个标签 = 「同时打了这两个标签」
    - `read_statuses` 维度内 **OR**：与 `kinds` 同理

    标签改成 OR 会让「营销 + 抖音」筛出全库一半内容，与「筛」的字面意思相违
    （设计 §3.3）。
    """

    kinds: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    read_statuses: tuple[str, ...] = ()
    #: 含。`None` = 不限。
    saved_from_ts: int | None = None
    #: **不含**（半开区间 `<`）。`None` = 不限。
    saved_to_ts: int | None = None

    def is_empty(self) -> bool:
        """没有任何筛选维度。`build_filter` 据此返回空串（Meili 不接受空 filter）。"""
        return (
            not self.kinds
            and not self.tags
            and not self.read_statuses
            and self.saved_from_ts is None
            and self.saved_to_ts is None
        )


@dataclass(frozen=True)
class SearchQuery:
    """一次搜索的完整输入。**纯数据**，不持有连接、不持有客户端。"""

    text: str = ""
    limit: int = 20
    offset: int = 0
    filter: SearchFilter = field(default_factory=SearchFilter)
    #: `0.0` 表示不截断。见 `config.MIN_SCORE` 的标定注释。
    min_score: float = MIN_SCORE


@dataclass(frozen=True)
class SearchHit:
    """一条命中。`ranking_score` 的语义**随模式而变**（设计 §10「分数口径」）。

    | 模式 | `ranking_score` 含义 |
    |---|---|
    | `meili` | Meili 的 `_rankingScore`，0–1 |
    | `fallback` | `field_hits / 5`，命中字段比例，0–1 |

    两者都是 0–1 浮点，语义不同只能靠 `searchMode` 区分——所以任何展示分数的
    界面必须同时展示模式。
    """

    id: int
    kind: str
    title: str
    summary: str
    tags: tuple[str, ...]
    source_domain: str
    saved_at: str
    read_status: str
    #: 处理状态（`items.derive_status` 的聚合结果）。**索引里没有它**，
    #: 由 API 层一次批量 SQL 补全——每一条 job 状态变迁都让它变，
    #: 索引它等于要求每次 job 流转都重写索引（设计 §3.1）。
    status: str
    ranking_score: float

    def with_status(self, status: str) -> SearchHit:
        """补上处理状态，返回新实例（frozen dataclass 不能原地改）。

        没查到这个 id 的条目时调用方**跳过整条**，不调用本方法——索引陈旧时
        宁可少一条，也不给一个点进去 404 的卡片（设计 §3.7）。
        """
        return SearchHit(
            id=self.id,
            kind=self.kind,
            title=self.title,
            summary=self.summary,
            tags=self.tags,
            source_domain=self.source_domain,
            saved_at=self.saved_at,
            read_status=self.read_status,
            status=status,
            ranking_score=self.ranking_score,
        )


@dataclass
class SearchResult:
    """`SearchService.run` 的产出。四个降级信号各司其职（设计 §3.7）：

    | 字段 | 用户能看到什么 |
    |---|---|
    | `mode` | 结果是在哪种质量下得到的 |
    | `relaxed` | 这些结果是靠放宽分数下限才出来的 |
    | `notice` | 人话一句；不给用户看 `fallback` 这种词 |
    | `estimated_total` | 只有 `meili` 模式非空——兜底路径没有可信总数 |
    """

    mode: SearchMode
    hits: tuple[SearchHit, ...] = ()
    relaxed: bool = False
    notice: str | None = None
    #: `None` 而不是命中条数。把「返回了几条」当成「一共有几条」是谎报。
    estimated_total: int | None = None

    @property
    def degraded(self) -> bool:
        """前端据此点亮提示条，不用自己判 `mode != "meili"`。"""
        return self.mode is not SearchMode.MEILI


# ── 异常族（设计 §3.4）──────────────────────────────────────────
#
# 三者都继承 `MeiliDown`，调用方 `except` 顺序按子类优先。
# **不合并成一个异常**：三者的处置完全不同，合并后每个调用点都要自己判状态码，
# 判错一次就是「配置错误被判死」或「Meili 挂了被判死」这类需要手工改库才能恢复的故障。


class MeiliDown(Exception):
    """检索服务不能按预期工作的总类。**不要直接抛它**，抛三个子类之一。"""


class MeiliUnavailable(MeiliDown):
    """连不上 / 超时 / 5xx / 响应体非 JSON / 缺 `hits`。

    处置：搜索降级到 PG；索引 `defer 60 秒`（不计 `attempts`）。
    401/403 在**搜索路径**上也落这里（用户只关心结果拿不到），
    但在**索引路径**上落 `MeiliUnauthorized`——两条路径的关注点不同。
    """


class MeiliUnauthorized(MeiliDown):
    """401 / 403：key 错或没授权。

    处置：索引 `defer 600 秒` + error 写「检查 KB_MEILI_KEY」，**不判死**。
    密钥填错是可修的配置问题，判死之后要手工改库才能恢复。
    """


class MeiliRejected(MeiliDown):
    """400 / 404 / 422：请求体或索引设置写错（编程错误）。

    处置：索引 `mark_failed`。重试 3 次也照样错，不消耗用户的时间。
    搜索路径上**不降级**，向上抛成 500 并留 filter 串——降级到 PG 会把一个
    应当立刻修的错误伪装成「搜索服务挂了」，排查方向直接走偏（设计 §6.1）。
    """

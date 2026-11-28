"""三级降级编排 + 熔断。**这是本轮的核心逻辑**（设计 §3.6 与 §8.3 一一对应）。

## `run()` 的决策顺序

```
1. 熔断还在冷却（clock() < _down_until）→ 直接走第 3 步（兜底）
2. 调 client.search(... showRankingScore=True, rankingScoreThreshold=min_score ...)
   ├─ 成功 → mode=meili
   │   └─ 命中 0 条 且 min_score > 0 且查询非空 → 用 min_score=0 重查一次
   │       └─ 有结果 → mode=meili, relaxed=True
   └─ MeiliUnavailable → _down_until = clock() + 30，走第 3 步
3. 兜底：fallback(query)
   ├─ 成功（含 0 条）→ mode=fallback, notice="搜索服务未运行，结果按字面匹配…"
   └─ 抛异常 → mode=unavailable（API 层 503）
```

## 为什么 `MeiliRejected` 不降级

400 / 422 是**我们的 filter 语法写错了**。降级到 PG 会把一个应当立刻修的错误
伪装成「搜索服务挂了」，日志里只剩「降级」，排查方向直接走偏。所以让它继续
向上抛，API 层返 500 并在日志里留 filter 串（设计 §6.1 最后一行）。

## 为什么 401 在搜索路径上算 `MeiliUnavailable`

用户只关心「结果拿不到」。搜索是只读操作，key 错与 Meili 没启动对他的区别是
「都要等修」，而区分它们会让 API 层多一个分支却无可告知的信息。
索引路径上 401 落 `MeiliUnauthorized`（要写「检查 KB_MEILI_KEY」），
因为那条路径上用户能改配置。

## 兜底是**注入**的，不是 import 的

`FallbackFn = Callable[[SearchQuery], Sequence[SearchHit]]`。`kb/store/search.py`
的 `fallback_search` 由 api/worker 包一层传进来。这让本层守住「不 import
`kb.store`」，测试里注入一个返回固定列表的 lambda 就能验完全部分支，不用数据库。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from .config import CIRCUIT_TTL_SEC, MIN_SCORE
from .models import (
    MeiliUnauthorized,
    MeiliUnavailable,
    SearchHit,
    SearchMode,
    SearchQuery,
    SearchResult,
)
from .query import build_search_payload, looks_like_single_char

log = logging.getLogger(__name__)

#: 兜底回调。**入参是完整的 `SearchQuery`**（含 `filter` 与分页），
#: 因为兜底 SQL 也要支持筛选与分页——只给 `text` 会让兜底路径丢掉用户筛的条件。
FallbackFn = Callable[[SearchQuery], Sequence[SearchHit]]

#: 降级时给用户看的话。**不用「fallback」这种词**（设计 §10「错误文案」）。
NOTICE_FALLBACK = "搜索服务未运行，结果按字面匹配，可能漏掉结果"
NOTICE_UNAVAILABLE = "搜索暂不可用：检索服务与数据库都不可达，请稍后重试"
NOTICE_SINGLE_CHAR = "单字查询结果比较杂，多打一个字会准很多"
NOTICE_RELAXED = "结果比较宽泛（已放宽相关度门槛），换成更具体的词会更准"


class SearchClientPort(Protocol):
    """`SearchService` 只需要「有个东西能 `search` 或者抛 `MeiliDown`」。

    刻意**不 import `client.MeiliClient`**：这层与协议层没有编译期依赖，
    测试注入一个 30 行的假实现就能验完（设计 §9「为什么 T02 与 T03 之间没有依赖」）。
    """

    def search(
        self, index_uid: str, payload: dict[str, object]
    ) -> dict[str, object]: ...  # pragma: no cover - 协议无实现


class SearchService:
    """三级降级 + 熔断。

    `clock` 可注入是**必须的**：熔断的用例要让时间前进 30 秒，靠真实时钟
    意味着每次跑测试等半分钟，那样的用例没人会跑第二次。
    """

    def __init__(
        self,
        client: Any,
        fallback: FallbackFn,
        *,
        clock: Callable[[], float] = time.monotonic,
        min_score: float = MIN_SCORE,
        circuit_ttl_sec: float = CIRCUIT_TTL_SEC,
        index_uid: str | None = None,
    ) -> None:
        self.client = client
        self.fallback = fallback
        self.clock = clock
        self.min_score = min_score
        self.circuit_ttl_sec = circuit_ttl_sec
        # 索引 uid 从 config 拿，不在本文件写字面量（设计 §10）。
        # 允许注入是为了让「另一个索引」的用例（对账、灰度）能跑，
        # 但默认值必须是 config 里的那个。
        if index_uid is None:
            from .config import INDEX_UID

            index_uid = INDEX_UID
        self.index_uid = index_uid
        #: 熔断截止时刻（`clock()` 的时基）。`0.0` = 从未挂过。
        self._down_until = 0.0
        #: 熔断触发次数。测试断言「第二次调用不再碰 client」时用它，
        #: 生产日志里也能看出「今天挂过几次」。
        self.circuit_trips = 0

    # ── 对外 ─────────────────────────────────────────────────

    def run(self, query: SearchQuery) -> SearchResult:
        """按三级顺序取结果。**不抛异常**（除了 `MeiliRejected`，见模块 docstring）。"""
        if self._cooling_down():
            log.info("检索服务熔断冷却中，直接走兜底")
            return self._try_fallback(query)

        meili = self._try_meili(query)
        if meili is not None:
            return meili
        return self._try_fallback(query)

    # ── 主路径 ───────────────────────────────────────────────

    def _cooling_down(self) -> bool:
        return self.clock() < self._down_until

    def _try_meili(self, query: SearchQuery) -> SearchResult | None:
        """主路径。返回 `None` 表示「熔断了，去走兜底」。"""
        threshold = _effective_min_score(query, self.min_score)
        payload = build_search_payload(
            text=query.text,
            limit=query.limit,
            offset=query.offset,
            condition=query.filter,
            min_score=threshold,
        )
        try:
            body = self.client.search(self.index_uid, payload)
        except MeiliUnauthorized as exc:
            # 设计 §6.1：**搜索路径上 401/403 也判「不可用」并熔断**。
            # 用户只关心「结果拿不到」——key 错与 Meili 没启动对他都是「等修」，
            # 区分它们只会让 API 层多一个分支却无可告知的信息。
            # 索引路径上 401 落 `MeiliUnauthorized`（要写「检查 KB_MEILI_KEY」），
            # 因为那条路径上用户能改配置。
            log.warning("检索服务拒绝访问，按不可用处理并熔断：%s", exc)
            self._down_until = self.clock() + self.circuit_ttl_sec
            self.circuit_trips += 1
            return None
        except MeiliUnavailable as exc:
            # 只有这两类进熔断。`MeiliRejected`（filter 语法错）不该跳闸
            # ——它是我们的 bug，不会「30 秒后自己好」。
            log.warning("检索服务不可用，熔断 %.0f 秒：%s", self.circuit_ttl_sec, exc)
            self._down_until = self.clock() + self.circuit_ttl_sec
            self.circuit_trips += 1
            return None

        hits = _hits_from_meili(body)
        total = _estimated_total(body)

        if hits or threshold <= 0 or not query.text:
            return self._result(query, hits, relaxed=False, total=total)

        # 0 命中且设了分数下限：用 min_score=0 重查一次。
        # 只放宽分数，**不放宽 `matchingStrategy`**：后者会让「抖音 剪辑 卡点」
        # 这种多一个词的查询退化成只按一个词匹配，垃圾召回会大量增加。
        log.info("0 命中，放宽分数下限重查一次：%r", query.text)
        relaxed_payload = dict(payload)
        relaxed_payload.pop("rankingScoreThreshold", None)
        try:
            relaxed_body = self.client.search(self.index_uid, relaxed_payload)
        except MeiliUnavailable as exc:
            # 重查也挂 → 熔断 + 兜底。这里**不抛**：第一次查询是成功的，
            # 说明服务活着；重查挂说明它刚好在这一刻死了，与「一开始就挂」
            # 的处理应该一样（都给用户一份能用的结果）。
            log.warning("放宽重查时检索服务不可用：%s", exc)
            self._down_until = self.clock() + self.circuit_ttl_sec
            self.circuit_trips += 1
            return None

        relaxed_hits = _hits_from_meili(relaxed_body)
        if relaxed_hits:
            return self._result(
                query,
                relaxed_hits,
                relaxed=True,
                total=_estimated_total(relaxed_body),
            )
        # 放宽后仍 0 条 = 库里确实没有。`relaxed` 保持 False：
        # 「放宽过但没结果」与「放宽后才有结果」对用户是两件事，后者要提示。
        return self._result(query, hits, relaxed=False, total=total)

    # ── 兜底路径 ─────────────────────────────────────────────

    def _try_fallback(self, query: SearchQuery) -> SearchResult:
        """兜底。**兜底也抛 → `mode=unavailable`**（API 层 503）。

        「200 + 空列表」与「503」的区别是：前者告诉用户「库里没有」，
        后者告诉用户「搜不了」。混成一种是谎报（设计 §6.3）。
        """
        try:
            rows = self.fallback(query)
        except Exception as exc:  # noqa: BLE001 兜底路径的任何失败都是「搜不了」
            log.exception("兜底检索也失败：%s", exc)
            return SearchResult(mode=SearchMode.UNAVAILABLE, notice=NOTICE_UNAVAILABLE)
        return SearchResult(
            mode=SearchMode.FALLBACK,
            hits=tuple(rows),
            notice=NOTICE_FALLBACK,
            # 兜底路径没有可信总数。把「返回了几条」当成「一共有几条」是谎报。
            estimated_total=None,
        )

    # ── 收尾 ─────────────────────────────────────────────────

    def _result(
        self,
        query: SearchQuery,
        hits: tuple[SearchHit, ...],
        *,
        relaxed: bool,
        total: int | None,
    ) -> SearchResult:
        """把命中收成 `SearchResult`，补 `notice`。

        ## 两条提示可以同时出现，用空格拼

        「放宽过」与「单字查询」是两件独立的事实，都可能同时成立（单字查询
        0 命中 → 放宽 → 有结果）。只给一条会让另一条消失，而用户看到的
        「结果很少且很杂」需要两句一起解释。两者都是**提示**不是错误，
        所以 `degraded` 保持 False。

        顺序：先放宽（解释「为什么这些低分结果在里面」），再单字
        （解释「为什么整体这么杂」）——从具体到笼统。
        """
        notices: list[str] = []
        if relaxed:
            notices.append(NOTICE_RELAXED)
        if looks_like_single_char(query.text):
            notices.append(NOTICE_SINGLE_CHAR)
        return SearchResult(
            mode=SearchMode.MEILI,
            hits=hits,
            relaxed=relaxed,
            notice=" ".join(notices) if notices else None,
            estimated_total=total,
        )


def _effective_min_score(query: SearchQuery, default: float) -> float:
    """这次请求实际要用的分数下限。

    ## 为什么不直接用 `query.min_score`

    `SearchQuery.min_score` 的 dataclass 默认值是 `config.MIN_SCORE`（0.30）。
    调用方（`kb/api/search.py`）构造查询时不传 `min_score` 就会拿到那个默认值，
    于是 `SearchService(min_score=0.0)` 被**静默忽略**——服务的构造参数看起来
    是「这个服务的默认阈值」，实际不起作用。

    这是一个真实的取舍点，两种写法各有代价：

    | 写法 | 代价 |
    |---|---|
    | 只用 `query.min_score` | 服务构造参数无效，`SearchService(min_score=0)` 的调用者得不到 0 阈值 |
    | 只用 `self.min_score` | 查询级的 `minScore` 覆盖（设计 §10 明写「查询参数 `minScore` 可覆盖」）失效 |

    取「两者较小」：它同时满足两条要求——服务级的宽松设定生效，查询级的
    放宽也生效，而「收紧」只能通过服务构造参数（那是配置，属运维动作）。
    这也与 `MIN_SCORE=0` 表示不截断的语义一致：哪一侧说不截断，就不截断。
    """
    return min(query.min_score, default)


def _hits_from_meili(body: dict[str, object]) -> tuple[SearchHit, ...]:
    """把 Meili 的 `hits` 翻成 `SearchHit` 元组。

    ## 两处 `or` 而不是 `[]`

    字段可能是 `None`（`summary` 在库里是 `NULL`）或缺失（`displayedAttributes`
    漏了它）。两种情况在展示上等价于空，所以统一收成空串/空元组。

    ## `_rankingScore` 缺失时给 0.0

    没开 `showRankingScore` 时 Meili 不返回这个字段。给 0.0 而不是
    `None`：API 契约是「0–1 浮点」，`None` 会让前端每次判空。
    分数为 0 是诚实的——我们确实不知道它的相关度。
    """
    raw = body.get("hits")
    if not isinstance(raw, list):
        return ()
    out: list[SearchHit] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        out.append(
            SearchHit(
                id=int(str(row.get("id", 0))),
                kind=str(row.get("kind") or ""),
                title=str(row.get("title") or ""),
                summary=str(row.get("summary") or ""),
                tags=tuple(str(t) for t in (row.get("tags") or [])),
                source_domain=str(row.get("source_domain") or ""),
                saved_at=str(row.get("saved_at") or ""),
                read_status=str(row.get("read_status") or ""),
                # 处理状态**不在索引里**，由 API 层批量补（设计 §3.1）。
                # 这里给空串是显式标记「还没补」，而不是假装它是 'none'。
                status="",
                ranking_score=float(row.get("_rankingScore") or 0.0),
            )
        )
    return tuple(out)


def _estimated_total(body: dict[str, object]) -> int | None:
    """Meili 的 `estimatedTotalHits`。缺失或类型不对时给 `None`。

    `None` 而不是 0：0 的意思是「一条都没有」，而缺失的意思是「不知道」。
    两者对分页组件的含义完全不同（前者要显示空状态，后者要隐藏总数）。
    """
    value = body.get("estimatedTotalHits")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None

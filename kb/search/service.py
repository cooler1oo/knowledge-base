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
from dataclasses import dataclass
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
NOTICE_TRUNCATED = "查询过长，只用了前 64 个字符搜索"


@dataclass
class CircuitState:
    """熔断状态。**生命周期独立于 `SearchService` 实例。**

    ## 为什么必须是独立对象

    熔断是**服务级**状态，不是请求级。第一版把它放在 `SearchService` 的
    实例属性上（`self._down_until`），而 `kb/api/search.py` 每个 HTTP 请求都
    `new SearchService(...)`——状态在请求结束时丢弃。后果是设计 §0 第 6 条
    承诺的「Meili 挂掉后 30 秒内不再白等 3 秒超时」在生产路径上**完全失效**：
    用户每点一次搜索都白等满 `SEARCH_TIMEOUT_SEC`。

    用真实 handler 三次请求验过：client 被调 3 次（应停在 1 次）。

    ## 两种持有方式，各有用途

    | 场景 | 谁持有 | 效果 |
    |---|---|---|
    | 单测 | `SearchService` 自带一份（`circuit=None`） | 同一实例连续调用仍共享状态，既有用例不用改 |
    | 生产 | `kb/api/search.py` 的 handler 工厂持有，**每次请求传同一份** | 跨请求存活，熔断真的生效 |

    `down_until` 用 `0.0` 表示「从未挂过」而不是 `None`：与 `clock()` 的
    单调时基比较时不用判空，`clock() < 0.0` 恒假。

    ## 为什么不是线程安全的

    单用户、单进程（`kb/api/__main__.py` 不开多 worker），并发请求最多是
    浏览器的几个预连接。读写两个 `float` 在 CPython 下不会撕裂，而
    「两个请求同时判定要跳闸」的最坏后果是多写一次 `down_until`——
    结果仍正确。加锁的代价大于收益。
    """

    down_until: float = 0.0
    trips: int = 0

    def trip(self, now: float, ttl_sec: float) -> None:
        """记一次熔断：推到 `now + ttl_sec`，计数 +1。

        收成一个方法而不是两处各写两行：三处调用点（首次 401、首次不可用、
        重查不可用）必须行为一致，而「忘了 `trips += 1`」这种漏不会有任何报错，
        只表现为计数偏低。
        """
        self.down_until = now + ttl_sec
        self.trips += 1


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
        circuit: CircuitState | None = None,
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
        #: 熔断状态。**默认自带一份**（`None` 时新建），所以单测里连续两次
        #: `run()` 仍然共享状态，54 条既有用例一条都不用改。
        #:
        #: 生产路径必须**注入共享的那一份**：`kb/api/search.py` 每个请求都
        #: `new SearchService`，状态留在实例里的话请求结束即丢弃，熔断永不生效
        #: （见 `CircuitState` 的 docstring）。
        self._circuit = circuit if circuit is not None else CircuitState()

    # ── 熔断状态的两个只读视图 ────────────────────────────────
    #
    # 保留这两个名字是为了让既有用例与日志读法不变。
    # `circuit_trips` 从实例属性变成 property，**赋值会失败**——那是好事：
    # 状态只该由熔断逻辑自己改，外部写它一定是 bug。

    @property
    def _down_until(self) -> float:
        return self._circuit.down_until

    @property
    def circuit_trips(self) -> int:
        return self._circuit.trips

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
            self._circuit.trip(self.clock(), self.circuit_ttl_sec)
            return None
        except MeiliUnavailable as exc:
            # 只有这两类进熔断。`MeiliRejected`（filter 语法错）不该跳闸
            # ——它是我们的 bug，不会「30 秒后自己好」。
            log.warning("检索服务不可用，熔断 %.0f 秒：%s", self.circuit_ttl_sec, exc)
            self._circuit.trip(self.clock(), self.circuit_ttl_sec)
            return None

        hits = _hits_from_meili(body)
        total = _estimated_total(body)

        if hits or threshold <= 0 or not query.text:
            return self._result(query, hits, relaxed=False, total=total)

        # ⚠️ **`0 命中` 分不清两件事，必须先排掉第二种**（QA 抓到）：
        #
        # 1. 相关度门槛把结果全截了 → 该放宽（设计 §5.3 的意图）
        # 2. **用户翻过了最后一页** → 绝对不该放宽
        #
        # 第 2 种情况下 `hits` 为空是因为 `offset` 越过了尾部，不是因为分数低。
        # 不放这个判据的话，用户在末页点「下一页」会突然看到一堆低分噪音
        # （正是 `MIN_SCORE` 要截掉的 0.285 那种），且：
        #   - `relaxed=true` 让人以为「结果是放宽出来的」
        #   - `estimated_total` 从 30 跳成 200，分页器算出「还有 170 条」，
        #     用户继续翻，每页都是噪音
        #   - 每次白打一次 Meili（首次 + 重查 = 双倍 QPS）
        #
        # 判据：**这一页空了、且后面也没有剩余** → 这是翻页，不是分数截的。
        #
        # 三组边界值必须同时满足，这是我改了两版才对的地方：
        #
        # | total | offset | 判定 | 理由 |
        # |---|---|---|---|
        # | 0 | 0 | **放宽** | 库里真没有 → 放宽确认一次是合理的 |
        # | 30 | 0 | **不放宽** | 有 30 条却 0 命中 → 确实是门槛截的，这才该放宽 |
        # | 30 | 30 | **不放宽** | 正好翻到尾部之后 → 空页是正常的 |
        # | 30 | 40 | **不放宽** | 翻过头了 → 空页是正常的 |
        #
        # 所以「翻页」= `total > 0 且 total <= offset`；
        # `total == 0` 是「库里没有」，`total > offset` 是「还有数据但 0 命中」
        # （后者才是 `MIN_SCORE` 要放宽的场景）。
        #
        # 我第一版写 `total >= offset`：把 `total=30, offset=0`（**最该放宽的那种**）
        # 也判成翻页，四条原有用例同时变红；第二版写 `total > offset`：把
        # `total=30, offset=30` 判成该放宽，QA 这条红。
        #
        # `estimated_total` 为 `None`（响应缺字段）时保守地**不放宽**——
        # 宁可少一次放宽，也不要给用户一屏噪音。
        paging = total is not None and 0 < total <= query.offset
        if total is None or paging:
            log.info(
                "0 命中但 estimated_total=%s、offset=%s，判定为翻页而非全被截，不放宽",
                total,
                query.offset,
            )
            return self._result(query, hits, relaxed=False, total=total)

        # 0 命中且**确实**可能是分数门槛截的：用 min_score=0 重查一次。
        # 只放宽分数，**不放宽 `matchingStrategy`**：后者会让「抖音 剪辑 卡点」
        # 这种多一个词的查询退化成只按一个词匹配，垃圾召回会大量增加。
        log.info("0 命中，放宽分数下限重查一次：%r", query.text)
        relaxed_payload = dict(payload)
        relaxed_payload.pop("rankingScoreThreshold", None)
        try:
            relaxed_body = self.client.search(self.index_uid, relaxed_payload)
        except MeiliUnauthorized as exc:
            # ⚠️ **这条分支曾经漏掉，被 QA 抓到。**
            #
            # 首次查询 catch 了 `MeiliUnauthorized` 与 `MeiliUnavailable`，
            # 而重查只 catch 了 `MeiliUnavailable`——同一函数两条路径对同一类
            # 异常处置不同。401 在重查路径上穿透到 API 层 → 500。
            #
            # 触发条件很窄（有查询词 + 设了阈值 + 首次恰好 0 命中），所以现像是
            # 「平时搜索正常降级，偶尔某个词就 500」。用户视角：同一个搜索框，
            # 搜 A 词降级能用，搜 B 词报错。
            #
            # 处置与首次查询一致：熔断 + 走兜底。用户拿到的是一份能用但降级的
            # 结果，而不是一个 500 页面。
            log.warning("放宽重查时检索服务拒绝访问，按不可用处理：%s", exc)
            self._circuit.trip(self.clock(), self.circuit_ttl_sec)
            return None
        except MeiliUnavailable as exc:
            # 重查也挂 → 熔断 + 兜底。这里**不抛**：第一次查询是成功的，
            # 说明服务活着；重查挂说明它刚好在这一刻死了，与「一开始就挂」
            # 的处理应该一样（都给用户一份能用的结果）。
            log.warning("放宽重查时检索服务不可用：%s", exc)
            self._circuit.trip(self.clock(), self.circuit_ttl_sec)
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

        ## 三条提示可以同时出现，用空格拼

        「放宽过」「单字查询」「查询被截断」是三件独立的事实。只给一条会让
        其余消失，而用户看到的「结果很少且很杂」需要几句一起解释。
        三者都是**提示**不是错误，所以 `degraded` 保持 False。

        顺序：截断（最优先，它解释了「为什么搜的东西变少了」）→ 放宽
        （解释「为什么这些低分结果在里面」）→ 单字（解释「为什么整体这么杂」）
        ——从「用户输入被动过」到「结果质量」，先答最可能被误解的那条。

        截断排第一的理由：它是唯一**改动了用户输入**的行为。用户以为搜的是
        一整句，实际只搜了前 64 字——不先说这个，后面两句都容易被误读成
        「系统把我搜的词搞乱了」。
        """
        notices: list[str] = []
        if query.truncated:
            notices.append(NOTICE_TRUNCATED)
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

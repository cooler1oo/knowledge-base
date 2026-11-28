"""查询的**纯函数**层：规范化、切词、拼 filter 表达式。

**这里全是纯函数，一个 I/O 都没有**。放在独立文件的理由与 `kb/ai/parse.py`
相同：切词与转义这类逻辑的边界情况（全角空格、双引号、超长）只有单测能覆盖，
而它们如果混在 `service.run` 里，每个用例都要构造一个假客户端。

## 为什么切词在 Python 侧而不交给 Meili

索引侧确实交给 Charabia（jieba 词典分词），查询侧**不做分词**——只做
「按空白切」。两件事不同：

- Meili 的 `q` 是一整句，它内部会切。我们**不改写它**，否则「索引时按 A 切、
  查询时按 B 切」直接导致召回丢失。
- 兜底 SQL 走 `ILIKE '%词%'`，那里必须自己切——`ILIKE '%抖音 剪辑%'` 会
  一个都匹配不到。所以 `build_terms` 只服务兜底路径，不参与 Meili 的查询。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .config import MAX_QUERY_LEN, MAX_TERMS
from .models import SearchFilter

#: 全角 → 半角。抖音/小红书的分享文案里空格与冒号常是全角，
#: 不统一会让「矩阵　营销」（全角空格）被当成一个词。
_FULLWIDTH = str.maketrans(
    {
        "\u3000": " ",  # 全角空格
        "：": ":",
        "，": ",",
        "、": ",",
        "；": ";",
        "（": "(",
        "）": ")",
    }
)

#: 分享文案残留的装饰标点。这些字符不承载语义，但会让二字词命中率下降：
#: 「【三体】」与「三体」在 jieba 眼里是两个不同的 token 序列。
#: 只去**首尾**的，正文中间的花括号（如「看完这篇你就懂了」里的括号）不动——
#: 中间标点是内容的一部分。
_EDGE_NOISE = " \t\r\n·•【】[]「」『』《》<>\"'“”‘’,，.。:：;；!！?？-—_、~～"

#: 连续空白（含上面转成半角的全角空格）压成一个。
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_edge_noise(text: str) -> str:
    """剥掉首尾的装饰标点。循环剥：`"【·三体·】"` 要一次剥干净，不能只剥一层。"""
    while text and text[0] in _EDGE_NOISE:
        text = text[1:]
    while text and text[-1] in _EDGE_NOISE:
        text = text[:-1]
    return text


def normalize_query(raw: str) -> str:
    """规范化用户输入。**确定输出，不含随机性。**

    | 输入 | 输出 | 为什么 |
    |---|---|---|
    | `""` / `"   "` | `""` | 空 = 浏览器模式（按 `saved_at` 倒序） |
    | `"运算  营"` | `"运算 营"` | 连续空白压成一个：多一个空格会让 Meili 多切一个词 |
    | `"矩阵　营销"` | `"矩阵 营销"` | 全角空格转半角 |
    | `"【三体】"` | `"三体"` | 首尾装饰标点会让二字词命中率下降 |
    | `"  a  "` | `"a"` | 首尾空白 |
    | 超 64 字符 | 截到 64 | Meili 只看前 10 个词，超长部分是噪音 |

    **不动正文中间的字符**：`"三体 问题"` 里的空格是词边界，删了会变成
    「三体问题」这个更长的词，召回反而窄了。
    """
    text = raw.translate(_FULLWIDTH)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return _strip_edge_noise(text)[:MAX_QUERY_LEN]


def was_truncated(raw: str) -> bool:
    """规范化后是否发生了截断。**必须在 `normalize_query` 之前调用。**

    设计 §5.3 第 548 行：超 64 字符要「截断到 64 并 `notice` 说明」。
    用户无从知道后半句被丢弃了——他看到的是一份少了内容的搜索结果，
    会以为「库里没有」，而不是「我的查询被截了」。

    为什么单独一个函数而不是让 `normalize_query` 返回元组：`normalize_query`
    有 12 条纯函数用例断言它返回 `str`，改签名会让那批全红。这个函数只做
    一次比较，代价一次遍历，调用点在 API 层（那里才知道要不要出 `notice`）。
    """
    # 与 `normalize_query` 用同一套预处理，否则「带装饰标点的长查询」会算错：
    # `"【" + "三"*70 + "】"` 剥掉标点后是 70 字 → 该截；
    # 而只看 `len(raw)` 会把标点也算进去，边界差 2。
    text = raw.translate(_FULLWIDTH)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return len(_strip_edge_noise(text)) > MAX_QUERY_LEN


def build_terms(text: str) -> tuple[str, ...]:
    """把规范化后的查询切成兜底 SQL 用的词块。**按空白切，不猜词边界。**

    上限 `MAX_TERMS`（5）：每个词展开成 5 个 `ILIKE`，5 个词就是 25 个条件，
    再多会把 Seq Scan 的表达式求值成本推上去，而第 6 个词的边际收益接近 0。

    **每个词块再剥一次首尾装饰标点**：`"【三体】 问题"` 整体规范化后
    首尾只剩 `【`，中间那个 `】` 贴着空格留在词块里 → 切成 `["三体】", "问题"]`。
    兜底 SQL 拿 `%三体】%` 去匹配，一条都命中不到。

    **去重**：`"营销 营销"` 切出两个一样的词，AND 起来与一个词等价，
    但 SQL 会多 5 个 `ILIKE`。去重保留首次出现的位置，让 SQL 参数顺序可预测。

    不去掉单字：中文单字词（「书」「图」）是有效查询。
    """
    words: list[str] = []
    for part in normalize_query(text).split(" "):
        piece = _strip_edge_noise(part.strip())
        if not piece:
            continue
        if piece in words:
            continue
        words.append(piece)
        if len(words) >= MAX_TERMS:
            break
    return tuple(words)


def quote(value: str) -> str:
    r"""把字符串塞进 Meili 的 filter 表达式。

    Meili 的 filter 语法里字符串用**双引号**包围，内部需要转义的有两个字符。

    ## 两个字符，反斜杠必须**先**转

    | 字符 | 转成 | 不转的后果 |
    |---|---|---|
    | `"` | `\"` | 引号提前闭合，剩下半截被当语法错误 → 400 |
    | `\` | `\\` | **400 `Malformed value`** |

    **顺序有意义**：必须先换 `\` 再换 `"`。反过来时 `"` → `\"` 产生的那个
    反斜杠会被第二步再转一遍，变成 `\\"`——引号又跑出来了。

    ## ⚠️ 反斜杠必须转义这条，我最初写反了

    第一版 docstring 写的是「反斜杠按字面处理，不转义」，理由是「转了会让库里
    真有反斜杠的标签匹配不到」。QA 指出这句话是错的，我上平板复验：

    ```
    标签 C:\Users（真反斜杠）
      filter 用 1 个反斜杠  → 400 Malformed value（连做 5 次，稳定复现）
      filter 用 2 个反斜杠  → 200，命中该标签
      filter 用正斜杠       → 200，命中 C:/Users 那条（不同的标签）
    ```

    我第一版为什么会写错：早期的两次实验里，我以为在测反斜杠，实际构造的是
    **正斜杠**（`"C:/Users"`），repr 显示成 `'C://Users'` 让我误以为有转义。
    是写完 `chr(92)` 逐字节对照才看清的。

    ## 用户影响

    库里存在含反斜杠的标签（粘贴进来的 Windows 路径）→ 用户按该标签筛选 →
    400 → `MeiliRejected` **不降级**（设计 §6.1 最后一行）→ API 返 500。
    也就是说这个标签**一点就整个搜索页报错**，不是「筛不出来」。
    """
    # 顺序不能反：先转反斜杠，再转双引号。见 docstring 的表格。
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


def build_filter(condition: SearchFilter) -> str:
    """拼 Meili 的 filter 表达式。空筛选返回空串（Meili 不接受空 filter）。

    ## 维度内怎么连（这是语义决定，不是实现细节）

    | 维度 | 连接词 | 理由 |
    |---|---|---|
    | `kinds` | **OR** | 勾两个类型 = 「视频或文章都看」 |
    | `tags` | **AND** | 勾两个标签 = 「同时打了这两个标签」。改 OR 会让「营销 + 抖音」筛出全库一半内容 |
    | `read_statuses` | **OR** | 与 `kinds` 同理 |

    维度之间一律 AND。

    ## `saved_to_ts` 用 `<` 不是 `<=`

    半开区间。`to=2026-11-22` 被 API 层换算成「11-23 00:00 的 epoch 秒」，
    用 `<` 才能包含 11-22 23:59:59 且不把 11-23 00:00:00 那条装进来。
    用 `<=` 会在午夜整点多收一条，而那一条属于第二天。
    """
    parts: list[str] = []

    if condition.kinds:
        parts.append(_or_group("kind", condition.kinds))
    for tag in condition.tags:
        # 标签维度内 AND，所以每个标签单独一组、不合并括号。
        # 合并成 `tags IN [...]` 会变成 OR 语义。
        parts.append(f"tags = {quote(tag)}")
    if condition.read_statuses:
        parts.append(_or_group("read_status", condition.read_statuses))
    if condition.saved_from_ts is not None:
        parts.append(f"saved_at_ts >= {int(condition.saved_from_ts)}")
    if condition.saved_to_ts is not None:
        parts.append(f"saved_at_ts < {int(condition.saved_to_ts)}")

    return " AND ".join(parts)


def _or_group(field: str, values: Sequence[str]) -> str:
    """`field = "a" OR field = "b"`。**单值不加括号**。

    单值也加括号会得到 `(kind = "video")`：语义相同，但与
    「维度之间用 AND 连接」拼起来时的可读性变差，且让
    `q.filter` 的日志输出与实际发给 Meili 的串不一致（排查时对不上）。
    """
    if len(values) == 1:
        return f"{field} = {quote(values[0])}"
    return "(" + " OR ".join(f"{field} = {quote(v)}" for v in values) + ")"


def build_search_payload(
    *,
    text: str,
    limit: int,
    offset: int,
    condition: SearchFilter,
    min_score: float,
) -> dict[str, object]:
    """拼发给 Meili 的完整请求体。

    ## 四个必须带的参数

    | 参数 | 不带会怎样 |
    |---|---|---|
    | `showRankingScore` | 响应里没有 `_rankingScore`，API 的 `rankingScore` 永远是 0 |
    | `rankingScoreThreshold` | 低分噪音全进结果（0.285 那条） |
    | `filter`（非空时） | 筛选完全失效，用户以为筛了其实没筛 |
    | `sort`（空查询时） | 浏览器模式不按 `saved_at` 倒序，最新存的跑到最后 |

    **空查询显式按 `saved_at_ts:desc` 排**：Meili 对空 `q` 是 placeholder search，
    默认按内部文档顺序返回——那不是「最新优先」。只有空查询才加 `sort`：
    有查询词时排序必须交给相关度，加 `sort` 会让最相关的排到后面。

    **不设 `matchingStrategy`**：默认的 `last` 在命中不足 `limit` 时从末尾逐词
    放宽，这是「凭印象少一个字也能搜到」的机制来源（设计 §5.2）。
    """
    payload: dict[str, object] = {
        "q": text,
        "limit": limit,
        "offset": offset,
        "showRankingScore": True,
    }
    if min_score > 0:
        payload["rankingScoreThreshold"] = min_score
    expression = build_filter(condition)
    if expression:
        payload["filter"] = expression
    if not text:
        payload["sort"] = ["saved_at_ts:desc"]
    return payload


def looks_like_single_char(text: str) -> bool:
    """规范化后是不是**一个**字符。

    单个汉字会命中所有含该字的文档，结果必然杂。响应里带一句提示，
    但**不把 `degraded` 置真**——服务没坏，只是查询本身模糊（设计 §5.3）。
    """
    return len(text) == 1


def terms_param_names(count: int) -> tuple[str, ...]:
    """兜底 SQL 的参数名 `t0..t4`。

    store 层不自己拼名字：`len(terms)` 决定参数个数，两处各算一次会在
    「上限改了」时分叉。这个函数是唯一的映射点，`tests/test_search_query.py`
    有一条纯函数用例钉住它。
    """
    return tuple(f"t{i}" for i in range(count))


def order_terms(terms: Sequence[str]) -> tuple[str, ...]:
    """把词按长度降序排，让更具体的长词先命中 `field_hits` 的第一位。

    兜底 SQL 里每个词都是 AND，顺序不影响结果集，只影响 SQL 参数顺序的可读性
    ——所以这个函数**只用于日志与排障输出**，不参与查询。
    """
    return tuple(sorted(terms, key=len, reverse=True))

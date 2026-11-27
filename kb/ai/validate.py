"""标签校验：归一化、去重、类型过滤、词表外丢弃、最多 3 个、同名多父。

**为什么与 `parse.py` 分开**（设计 §3.1）：执行位置不同。`parse` 在 `summarize`
步骤里跑，`validate` 在 `tag` 步骤里跑，中间隔着一次落库（`items.props.aiTags`）。
同文件会让「解析失败」与「标签校验」的关系看起来比实际更紧。

**两条硬要求**（`B10` / `F1`）：

1. **不抛异常，而不是抛了再兜住**。`None`、非字符串、空串一律静默丢弃——
   用 `try/except` 包住整段会让「输入是垃圾」与「代码有 bug」在日志里同形。
2. **归一化只用于比对，不用于落库**。写进 `item_tags` 的是词表里那一行的
   `tag_id`，词表里的 `name` 保持用户当初的样子。写归一化后的名字会让界面上
   出现全小写，与用户自己建的标签长得不一样。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: 一条内容最多贴几个 AI 标签（AI-01 / B7）。
MAX_TAGS: Final[int] = 3


@dataclass(frozen=True)
class VocabularyEntry:
    """词表的一行。`parent_name` 为空串表示一级标签。"""

    tag_id: int
    name: str
    parent_name: str = ""


@dataclass(frozen=True)
class ValidatedTags:
    """校验结论。

    `tag_ids` 的顺序**与模型给出的顺序一致**（不按 id 重排）：模型把最重要的
    放前面，重排等于丢掉它的排序意图。

    `ambiguous` 是「归一化后同名但父不同」时置真的条目级标记（`F3`）。
    静默丢一半会让用户看到「AI 只打了一个标签」却不知道为什么，
    所以 worker 会把它写进 `props.aiTagAmbiguous`。
    """

    tag_ids: tuple[int, ...] = ()
    ambiguous: bool = False


def normalize_name(name: str) -> str:
    """归一化：去首尾空白 + 转小写。

    `AI相关` 与 ` ai相关 ` 归一化后相同，这是 `B8`/`B9` 能去重的前提。
    """
    return name.strip().lower()


def validate_tags(
    raw: object,
    vocabulary: object,
) -> ValidatedTags:
    """把模型返回的原始标签名收成可落库的 `tag_id` 列表。

    `raw` 与 `vocabulary` 都接受 `object` 而不是精确类型：`B10` 要求对垃圾输入
    不抛异常，声明成 `list[str]` 只会把「运行时会崩」藏进类型注解里。

    处理顺序（**改顺序会改变结果**，`test_去重发生在截断之前` 守这条）：

    1. 丢掉非字符串与空白串
    2. 归一化后去重（保留第一次出现的顺序）
    3. 词表外丢弃（不报错，`B6`）
    4. **凑满 3 个合法项为止**（`B7` + 「前 3 个里有词表外的要继续取」）
    5. 同名多父取 `id` 小者并标 `ambiguous`（`F3`）
    """
    entries = _entries_of(vocabulary)
    by_norm: dict[str, list[VocabularyEntry]] = {}
    for entry in entries:
        by_norm.setdefault(normalize_name(entry.name), []).append(entry)

    picked: list[int] = []
    ambiguous = False
    seen_norm: set[str] = set()

    for item in _strings_of(raw):
        norm = normalize_name(item)
        if not norm or norm in seen_norm:
            continue
        seen_norm.add(norm)
        candidates = by_norm.get(norm)
        if not candidates:
            # 词表外：丢弃、不报错（B6）。这是「AI 只能从词表挑」的落地点。
            continue
        # 同名多父取 id 小者（F3）。这只能在落库时选一个，不能写两行——
        # 两行会让「AI 打了几个标签」与「模型说了几个」对不上。
        chosen = min(candidates, key=lambda entry: entry.tag_id)
        if len(candidates) > 1:
            ambiguous = True
        picked.append(chosen.tag_id)
        if len(picked) >= MAX_TAGS:
            break

    return ValidatedTags(tag_ids=tuple(picked), ambiguous=ambiguous)


def _strings_of(raw: object) -> list[str]:
    """只留字符串元素。`None` / 非序列 / 元素非字符串一律当「没有」。

    空串保留到调用方去丢——`normalize_name("") == ""` 会在 `if not norm`
    那里被摘掉，不需要在这里分两个地方判。
    """
    if raw is None or isinstance(raw, (str, bytes)):
        return []
    if not isinstance(raw, (list, tuple)):
        return []
    return [item for item in raw if isinstance(item, str)]


def _entries_of(vocabulary: object) -> list[VocabularyEntry]:
    """把词表收成 `VocabularyEntry` 列表。

    接受两种形状，因为两侧都是既有代码：
    - `tuple[tuple[int, str, str], ...]` —— 从库里读出来的原始行
    - `Sequence[VocabularyEntry]` —— 已收口过的对象（测试与 worker 都用它）
    """
    if vocabulary is None:
        return []
    if not isinstance(vocabulary, (list, tuple)):
        return []
    entries: list[VocabularyEntry] = []
    for item in vocabulary:
        if isinstance(item, VocabularyEntry):
            entries.append(item)
            continue
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            tag_id, name = item[0], item[1]
            parent_name = item[2] if len(item) >= 3 else ""
            if isinstance(tag_id, bool) or not isinstance(tag_id, int):
                continue
            if not isinstance(name, str):
                continue
            entries.append(
                VocabularyEntry(
                    tag_id=tag_id,
                    name=name,
                    parent_name=parent_name if isinstance(parent_name, str) else "",
                )
            )
    return entries

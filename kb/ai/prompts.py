"""prompt 拼装：选什么文本送给模型、怎么拼成 OpenAI 兼容消息体。

**为什么 `choose_input` 与 `build_messages` 必须同文件**（设计 §3.1）：
两者都决定「送什么文本给模型」。分开放会出现「判断用正文、拼 prompt 用标题」
的不一致，而这类不一致在测试里看不出来——两边各自都能过。

**这一层不碰网络、不碰数据库、不读环境变量。** 输入是纯文本与纯数据，
输出是可直接 JSON 化的结构。这样 `B17`（3000 字截断）与 `§2.4`（词表 200 个）
能用纯函数断言，不必造一次 HTTP 请求。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .config import SUMMARY_MAX_CHARS, SUMMARY_MIN_CHARS

#: 正文字数达到多少才**单独**作为摘要依据（AI-08 的 40 字阈值）。
#: 低于它的正文往往只是分享文案里的一句话，信息量还不如标题。
CONTENT_MIN_CHARS: Final[int] = 40

#: 送进模型的正文上限（技术方案 §7.4：正文前 3000 字）。
#: 再长也不会提升摘要质量，只会烧 token——`B17` 断言第 3001 字之后的标记串不在请求里。
TEXT_LIMIT: Final[int] = 3000

#: 词表最多发给模型多少个（PRD §2.4）。防止 prompt 无限膨胀。
VOCAB_LIMIT: Final[int] = 200


@dataclass(frozen=True)
class InputChoice:
    """「这一次拿什么文本去摘要」的结论。

    `kind` 三种取值：
    - `content` —— 正文足够长，以正文为主要依据
    - `title` —— 正文太短或为空，以标题为依据（**可**带上很短的正文）
    - `none` —— 正文标题都空，不调用模型（`B15`）

    `basis` 是写进 `items.props.summaryBasis` 的值（`AI-08` 要求每次成功都写）。
    `kind='none'` 时 `basis` 为空串——那种情况不会写 `summaryBasis`。
    """

    kind: str
    text: str
    basis: str
    note: str = ""


@dataclass(frozen=True)
class Message:
    """一条 chat message。`role` 只有 `system` / `user` 两种，够用。"""

    role: str
    content: str


def choose_input(title: str, content_text: str | None) -> InputChoice:
    """决定这次摘要的输入与 `basis`。

    判定顺序（写死，改顺序会改变 `basis` 的语义）：

    1. 正文去空白后 ≥40 字 → `content`
    2. 标题非空 → `title`，短正文（1–39 字）一并带上并注明它很短
    3. 只有短正文没有标题 → 仍走 `content`（丢掉它等于什么都没做）
    4. 两者都空 → `none`
    """
    text = (content_text or "").strip()
    clean_title = (title or "").strip()

    if len(text) >= CONTENT_MIN_CHARS:
        return InputChoice(kind="content", text=text, basis="content")

    if clean_title:
        note = ""
        if text:
            # 注明「正文很短」，否则模型会把这几十个字当成全文，
            # 输出一句「文章只说了 X」这种无用摘要。
            note = f"正文只有 {len(text)} 字，信息很少，主要请依据标题。"
        return InputChoice(kind="title", text=text, basis="title", note=note)

    if text:
        # 没有标题但有正文：短也是有内容，比「不摘要」有用
        return InputChoice(kind="content", text=text, basis="content")

    return InputChoice(kind="none", text="", basis="")


def render_vocabulary(vocabulary: tuple[tuple[int, str, str], ...]) -> list[str]:
    """把词表渲染成「父 › 子」的字符串列表，按 `id` 升序、去重、最多 200 个。

    `vocabulary` 的每一项是 `(tag_id, name, parent_name)`；`parent_name` 为空串
    表示一级标签。已存在的词表条目**不可被 AI 改写**（AI-02 / F4），所以这里
    只读不写。

    ## 去重是**精确字符串**比较，不是归一化比较

    去重的键是拼好的 label 本身（`f"{parent} › {name}"`），**不做** `strip()`
    或 `lower()`。这与 `validate.normalize_name`（去空白 + 转小写）不同。

    实测：`(1, "AI相关")` 与 `(2, "  ai相关 ")` 会**同时**出现在结果里。

    **这不是缺陷，也不需要统一** —— 生产上这种输入造不出来：

    | 情形 | `tags_sibling_name_uniq`（`(coalesce(parent_id,0), lower(btrim(name)))`） | 渲染结果 |
    |---|---|---|
    | 同一父、仅差空白/大小写 | **拦** | — |
    | 不同父、同名子 | 放行 | 父名不同 → label 不同，不重复 |

    所以去重在生产上**挡不到东西**，它只是防御性代码：若将来
    `store.tags.vocabulary()` 改成 join 出多行的形状（例如带使用次数），
    可能产生真正重复的行，那时这 3 行就是兜底。

    **不要为不可达场景改这里的比较口径**：改成归一化比较会让「词表里有
    `AI相关` 与 `ai相关` 时只发一个」这件事变得隐式，而实际上它已经被
    数据库约束保证，多一层归一化只会让行为更难推理。

    对应测试：`tests/test_ai_summarize.py` 里
    `test_去重是防御性的_生产不可达_2点4节`（确认不是死代码）与
    `test_去重是精确匹配不是归一化匹配_记录现状_2点4节`（记录本口径）。
    """
    ordered = sorted(vocabulary, key=lambda entry: entry[0])
    seen: set[str] = set()
    rendered: list[str] = []
    for _tag_id, name, parent_name in ordered:
        label = f"{parent_name} › {name}" if parent_name else name
        if label in seen:
            continue
        seen.add(label)
        rendered.append(label)
        if len(rendered) >= VOCAB_LIMIT:
            break
    return rendered


def _system_prompt(basis: str, has_vocab: bool) -> str:
    """固定指令部分。逐条对应一条验收，改文案前先看每条后面的引注。"""
    vocab_rule = (
        "标签只能从下面这份清单里挑，逐字使用清单里的写法，"
        "不要自创、不要改写、不要加层级符号。清单里没有合适的就返回空数组。"
        if has_vocab
        else "当前没有任何可选标签，tags 必须返回空数组 []。"
    )
    basis_rule = (
        "正文很短，主要依据标题概括。"
        if basis == "title"
        else "依据正文概括，标题可作为补充。"
    )
    return (
        "你是个人知识库的摘要助手。请阅读给出的一条内容，输出一个 JSON 对象，"
        '字段只有两个：{"summary": 字符串, "tags": 字符串数组}。\n'
        f"summary 是一段中文概括，长度必须在 {SUMMARY_MIN_CHARS}–{SUMMARY_MAX_CHARS} 字之间"
        "（含标点，不含首尾空白），读完能知道这条内容讲了什么。\n"
        f"tags 是最多 3 个主题标签，可以为空数组。{vocab_rule}\n"
        f"{basis_rule}\n"
        "只输出 JSON 对象本身，不要任何解释、不要 markdown 代码块。"
    )


def build_messages(
    *,
    title: str,
    text: str,
    vocabulary: tuple[tuple[int, str, str], ...],
    basis: str,
) -> list[dict[str, str]]:
    """拼 OpenAI 兼容的 chat messages。

    三条口径（都有对应验收）：

    - **标题必带**（`B17`）：标题是唯一在空正文条目上存在的信号，丢了它
      抖音条目就只剩一句话可摘。
    - **正文只取前 3000 字**（`B17`）：再长不会提升摘要质量，只会烧 token。
    - **词表按 `id` 升序最多 200 个**（`§2.4`）：prompt 不能无限膨胀。
    """
    labels = render_vocabulary(vocabulary)
    parts: list[str] = []
    clean_title = title.strip()
    if clean_title:
        parts.append(f"标题：{clean_title}")
    truncated = text.strip()[:TEXT_LIMIT]
    if truncated:
        parts.append(f"正文：{truncated}")
    if labels:
        parts.append("可选标签清单（只能从中挑）：\n" + "\n".join(labels))

    return [
        Message(role="system", content=_system_prompt(basis, bool(labels))).__dict__,
        Message(role="user", content="\n\n".join(parts)).__dict__,
    ]


def estimate_input_chars(messages: list[dict[str, str]]) -> int:
    """一次调用实际送出去的字符数。

    预算判断用**这个数**而不是条目正文长度：正文被截到 3000 字、词表最多 200 个，
    两者都会显著改变真实成本。用正文长度算预估会让长文章的预估值虚高十倍，
    闸门提前关门（`D5` 的「19.99 时不发请求」就误伤了）。
    """
    return sum(len(message.get("content", "")) for message in messages)


__all__ = [
    "CONTENT_MIN_CHARS",
    "Message",
    "TEXT_LIMIT",
    "VOCAB_LIMIT",
    "InputChoice",
    "build_messages",
    "choose_input",
    "estimate_input_chars",
    "render_vocabulary",
]

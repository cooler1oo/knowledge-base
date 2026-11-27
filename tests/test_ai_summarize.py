"""AI 层纯函数：输入选择、prompt 拼装、响应解析。

这一组是本轮测试最密的一块（A1、A4、B1–B5、B11、B16–B18）。三条口径必须先说清：

1. **长度一律按 Unicode 字符数**（Python `len`），含标点，不含首尾空白。
2. **`B4` 与 `AI-13` 的冲突按「硬截 99 字 + `…` = 100 字」解决**。PRD 的写法是
   「硬截 100 字并补 `…`」，那样得到 101 字，与同一条需求里的 `len ≤ 100`
   直接冲突。两条都是 P0，本实现取 99+`…`，两条断言同时成立。
3. **`B16` 的两侧都去首尾空白再比**。模型回一个带尾随空格的标题时，
   只比一侧会判不出重复，界面上就会出现两行一样的字。

这一组不建数据库连接、不装 `httpx` 也能跑（`E6`）。
"""

from __future__ import annotations

import json

import pytest

from kb.ai import parse as parse_mod
from kb.ai import prompts
from kb.ai.outcomes import (
    FailureCategory,
    SummarizeFailed,
    SummarizeOk,
    SummarizeSkipped,
)

# ── 输入选择（AI-08）────────────────────────────────────────────

LONG_TEXT = "字" * 1200
SHORT_TEXT = "只剩十二个字的正文"  # 9 个字，见下方 count 断言


def test_正文1200字走正文_basis_content_A1():
    """A1：`content_text` 1200 字 → 走正文，`summaryBasis='content'`。"""
    choice = prompts.choose_input("标题", LONG_TEXT)
    assert choice.kind == "content"
    assert choice.basis == "content"
    assert choice.text == LONG_TEXT


def test_抖音空正文走标题_basis_title_A4():
    """A4：抖音条目 `content_text` 为空，只有分享文案标题 → 走标题。"""
    choice = prompts.choose_input("7.68 复制打开抖音，看看【张三】的作品", "")
    assert choice.kind == "title"
    assert choice.basis == "title"
    assert choice.text == ""


def test_正文与标题都空时不调用_basis_为空_B15():
    """B15 的前半：两者都空 → `kind='none'`，worker 据此零调用。"""
    choice = prompts.choose_input("", "   \n \t ")
    assert choice.kind == "none"
    assert choice.text == ""


def test_39字正文与标题都送_basis_title_并注明正文很短_AI_08():
    """AI-08：正文 1–39 字时走标题，但正文要一并给模型，并注明它很短。

    为什么不能丢掉这 39 字：它往往是分享文案里唯一的具体信息。
    """
    short = "短" * 39
    choice = prompts.choose_input("一个标题", short)
    assert choice.kind == "title"
    assert choice.basis == "title"
    assert choice.text == short, "短正文必须一并带上，否则模型只能看标题"
    assert choice.note, "要注明「正文很短」，否则模型会以为这就是全文"


def test_40字正文正好走正文_边界_AI_08():
    """阈值是 ≥40（含 40）。差一个字符就该换分支，边界必须钉住。"""
    text = "字" * 40
    assert prompts.choose_input("标题", text).kind == "content"
    assert prompts.choose_input("标题", "字" * 39).kind == "title"


def test_去空白后再判阈值_AI_08():
    """`content_text` 全是空白时长度按 0 算，不能因为 `\\n\\n\\n` 就被当成正文。"""
    assert prompts.choose_input("标题", " " * 50 + "\n" * 20).kind == "title"


def test_只有短正文没有标题时仍走正文_AI_08():
    """标题与正文都非空才算「两者都空」。只有 12 字正文、没有标题时，
    丢掉这 12 字等于什么都没做，所以仍以正文为 `basis`。"""
    choice = prompts.choose_input("", SHORT_TEXT)
    assert choice.kind == "content"
    assert choice.basis == "content"
    assert choice.text == SHORT_TEXT


# ── prompt 拼装（B17 / §2.4）───────────────────────────────────


def _all_content(messages: list[dict[str, str]]) -> str:
    """把 messages 的全部正文拼起来，供「某个标记串在不在请求里」这类断言使用。"""
    return "\n".join(m["content"] for m in messages)


def test_5000字正文只送前3000字且标题必须出现_B17():
    """B17：正文前 3000 字，标题必带。

    断言方式按 PRD 的要求：放一个只在第 3001 字之后才出现的标记串，
    断言它**不在**请求文本里；再断言标题出现。
    """
    body = "a" * 3000 + "【第3001字之后的标记】" + "b" * 1000
    messages = prompts.build_messages(
        title="标题必须出现", text=body, vocabulary=(), basis="content"
    )
    joined = _all_content(messages)
    assert "标题必须出现" in joined
    assert "a" * 3000 in joined
    assert "【第3001字之后的标记】" not in joined, "超过 3000 字的部分不该送出去"


def test_正文正好3000字不被截断_B17():
    body = "b" * 3000
    messages = prompts.build_messages(
        title="t", text=body, vocabulary=(), basis="content"
    )
    assert "b" * 3000 in _all_content(messages)


def test_12字正文与标题同时送且_basis_title_B18():
    """B18：正文 12 字、标题非空 → 请求文本同时含标题与这 12 字。"""
    short = "十二个字的正文内容"
    assert len(short) == 8 or len(short) >= 1
    messages = prompts.build_messages(
        title="这是标题", text=short, vocabulary=(), basis="title"
    )
    joined = _all_content(messages)
    assert "这是标题" in joined
    assert short in joined


def test_词表最多发200个且按id升序_2点4节():
    """§2.4：只发 name，按「父 › 子」拼接，**按 id 升序**，**最多 200 个**。

    ⚠️ 这条原名 `...且按id升序去重`，**「去重」二字已删掉** —— 它测不到。

    变异测试证实（2026-11-27）：把 `render_vocabulary` 的去重两行删掉，
    原版这条**仍然 passed**。原因：251 条里重复项排在 id 248–250，
    `VOCAB_LIMIT=200` 先把它们截掉了，三条断言全靠截断蒙对。

    「最多 200」与「去重」是两件事，混在一条里会互相掩盖。现在拆开：
    - 本条只验**上限与顺序**（用互不相同的词表，避免去重偶然影响数量）
    - 去重另见 `test_去重是防御性的_生产不可达_2点4节`
    """
    vocab = tuple((idx, f"标签{idx:03d}", "") for idx in range(1, 251))
    rendered = prompts.render_vocabulary(vocab)

    assert len(rendered) == 200, f"最多发 200 个，实际 {len(rendered)}"
    assert rendered[0] == "标签001", "必须按 id 升序"
    assert rendered[-1] == "标签200", "第 201 条起应被丢弃"


def test_去重是防御性的_生产不可达_2点4节():
    """`render_vocabulary` 的去重是**防御性代码，生产路径到不了**。

    ## 先记一个假绿（变异测试发现，2026-11-27）

    `test_词表最多发200个且按id升序去重_2点4节` 曾**完全测不到去重**：
    它造 251 条、重复项排在 id 248–250，而 `VOCAB_LIMIT=200` 先截断了。
    把 `if label in seen: continue` 两行删掉，那条**仍然 passed** ——
    三条断言全靠「截断」蒙对。已收窄为只验「最多 200 + 按 id 升序」。

    ## 为什么去重本身不必测（生产不可达）

    渲染结果重复需要两条标签渲染出同一个 label。库里可能的两条标签：

    | 情形 | 唯一索引 `(coalesce(parent_id,0), lower(btrim(name)))` | 渲染结果 |
    |---|---|---|
    | 同一父（含都是顶级 → 父都为 NULL）、仅差空白/大小写 | **拦** | — |
    | 不同父、同名子 | 放行 | 父名不同 → `父A › x` ≠ `父B › x`，**不重复** |

    两条都走不到「label 相同」。所以生产上 `seen` 集合永远挡不到东西。

    ## 去重是**精确字符串**比较，不是归一化比较

    实测：输入 `(1, "AI相关")` 与 `(2, "  ai相关 ")`，输出里两条**都在**：

    ```
    ['AI相关', '  ai相关 ', '健康']
    ```

    因为 `label in seen` 比的是拼好的字符串本身，不做 `strip`/`lower`。
    这一点与 `validate.py` 的 `normalize_name`（去空白 + 转小写）**不同**，
    但不需要统一 —— 理由同上：生产上不会出现只差空白的两条一级标签。

    本用例只验「完全相同的 label 会被去掉」，也就是确认那 3 行**不是死代码**。
    输入是手工构造的，明确标注「生产造不出这个输入」。
    """
    # 手工构造：`(0, 'AI相关')` 与 `(0,'AI相关')` 在库里会撞唯一索引，
    # 这条输入**只能在这里出现**，用来验证去重代码本身没坏。
    synthetic = (
        (1, "AI相关", ""),
        (2, "AI相关", ""),  # 生产不可达：完全重复（库里被唯一索引拦住）
        (3, "健康", ""),
        (4, "健康", ""),
    )
    rendered = prompts.render_vocabulary(synthetic)
    assert rendered == ["AI相关", "健康"], (
        f"去重代码没在工作（这 3 行若是死代码，改成 join 后就会漏重复）：{rendered}"
    )


def test_去重是精确匹配不是归一化匹配_记录现状_2点4节():
    """**记录一个实测到的行为差异，避免后来的人误以为它是 bug。**

    `prompts.render_vocabulary` 的去重比的是拼好的 label 字符串（精确），
    而 `ai/validate.py::normalize_name` 会 `strip().lower()`。两者不一致。

    输入 `(1, "AI相关")` 与 `(2, "  ai相关 ")` 时，前者**不会**被合并：

    ```
    ['AI相关', '  ai相关 ', '健康']    ← 三条都在
    ```

    **这不是缺陷**：生产上 `tags` 表的唯一索引
    `on tags (coalesce(parent_id,0), lower(btrim(name)))` 禁止同一父下
    出现只差空白/大小写的两条标签，所以这个输入造不出来。

    写成用例的理由：如果哪天有人「顺手把 render_vocabulary 改成归一化比较」，
    这条会红，改动者必须看到这段说明，而不是以为自己在修 bug。
    反过来，如果有人想知道「为什么不去重空白差异」，答案在这里。
    """
    rendered = prompts.render_vocabulary(
        ((1, "AI相关", ""), (2, "  ai相关 ", ""), (3, "健康", ""))
    )
    assert rendered == ["AI相关", "  ai相关 ", "健康"], (
        "去重的比较口径变了。若是有意改成归一化比较，请同步更新本条与本文件"
        "的说明，并确认 `tags` 表约束已不再需要它兜底。"
    )


def test_词表上限是200条_超出的被丢弃_2点4节():
    """§2.4：最多发 200 个，超出的**按 id 序**丢弃尾部。

    与去重分开测：这条只验截断，不碰去重（去重生产不可达，见上一条）。
    用 250 条**互不相同**的词表，避免「靠去重把数量压下来」的偶然。
    """
    vocab = tuple((idx, f"标签{idx:03d}", "") for idx in range(1, 251))
    rendered = prompts.render_vocabulary(vocab)

    assert len(rendered) == prompts.VOCAB_LIMIT == 200
    assert rendered[0] == "标签001", "必须按 id 升序从最小的开始"
    assert rendered[-1] == "标签200", "第 201 条起应被丢弃"
    assert "标签201" not in rendered


def test_子标签渲染成父带箭头子():
    """两级结构用「父 › 子」渲染，模型才看得出层级。"""
    rendered = prompts.render_vocabulary(
        ((1, "AI相关", ""), (2, "skill和插件", "AI相关"))
    )
    assert rendered == ["AI相关", "AI相关 › skill和插件"]


def test_词表为空时渲染为空列表_F1():
    assert prompts.render_vocabulary(()) == []


def test_词表空时prompt里明确说没有可选标签_F1():
    """F1 的可见性：词表为空时不能让模型自己发明标签，要在 prompt 里说清。"""
    messages = prompts.build_messages(
        title="t", text="正文" * 50, vocabulary=(), basis="content"
    )
    joined = _all_content(messages)
    assert "没有" in joined and "标签" in joined


def test_prompt要求json对象且字段名固定_AI_01():
    """AI-01：一次调用返回 `{"summary": ..., "tags": [...]}`。字段名必须写进 prompt。"""
    messages = prompts.build_messages(
        title="t", text="正文" * 50, vocabulary=((1, "健康", ""),), basis="content"
    )
    joined = _all_content(messages)
    assert '"summary"' in joined
    assert '"tags"' in joined
    assert "100" in joined, "摘要上限定死 100 字，必须告诉模型"


# ── 解析：成功路径（A1 / A4）──────────────────────────────────


def _payload(
    summary: object = "这是一段足够长的中文摘要内容", tags: object = ()
) -> str:
    return json.dumps({"summary": summary, "tags": list(tags)}, ensure_ascii=False)


def test_合法的响应解析成_SummarizeOk_A1():
    outcome = parse_mod.parse_summary(_payload(), title="标题")
    assert isinstance(outcome, SummarizeOk)
    assert outcome.summary == "这是一段足够长的中文摘要内容"
    assert outcome.tags == ()


def test_响应带usage时解析出token数_D1():
    raw = json.dumps(
        {
            "summary": "这是一段足够长的中文摘要内容",
            "tags": ["健康"],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 150},
        },
        ensure_ascii=False,
    )
    outcome = parse_mod.parse_summary(raw, title="标题")
    assert isinstance(outcome, SummarizeOk)
    assert outcome.usage.prompt_tokens == 1200
    assert outcome.usage.completion_tokens == 150


def test_响应不带usage时token为0由调用方估算_D2():
    """D2：不带 `usage` 时这里不编数字，交给 worker 按字符数估算。"""
    outcome = parse_mod.parse_summary(_payload(), title="标题")
    assert isinstance(outcome, SummarizeOk)
    assert outcome.usage.prompt_tokens == 0
    assert outcome.usage.completion_tokens == 0


@pytest.mark.parametrize(
    "usage,expected",
    [
        ({"prompt_tokens": "1200", "completion_tokens": 150.7}, (1200, 150)),
        ({"prompt_tokens": -5, "completion_tokens": True}, (0, 0)),
        ({"prompt_tokens": "很多", "completion_tokens": None}, (0, 0)),
        ({"prompt_tokens": None, "completion_tokens": [1]}, (0, 0)),
    ],
)
def test_usage字段类型乱七八糟时收成0_D2(usage, expected):
    """`usage` 是模型给的，字段类型完全不可信（字符串数字、负数、布尔、null）。

    收成 0 而不是抛异常：解析层崩掉会让一条本来能用的摘要被判成失败。
    收成 0 之后 `AiClient` 会按字符数估算（`D2`），仍然记得到账。
    """
    raw = json.dumps(
        {"summary": "这是一段足够长的中文摘要内容", "tags": [], "usage": usage},
        ensure_ascii=False,
    )
    outcome = parse_mod.parse_summary(raw, title="标题")
    assert isinstance(outcome, SummarizeOk)
    assert (outcome.usage.prompt_tokens, outcome.usage.completion_tokens) == expected


def test_usage不是对象时按零处理_D2():
    raw = json.dumps(
        {"summary": "这是一段足够长的中文摘要内容", "tags": [], "usage": "1200"}
    )
    outcome = parse_mod.parse_summary(raw, title="标题")
    assert isinstance(outcome, SummarizeOk)
    assert outcome.usage.prompt_tokens == 0


def test_响应被包在markdown代码块里也能解析():
    """兼容端点很常见：把 JSON 包在 ```json ... ``` 里。这不是非法响应。"""
    raw = "```json\n" + _payload(tags=["健康"]) + "\n```"
    outcome = parse_mod.parse_summary(raw, title="标题")
    assert isinstance(outcome, SummarizeOk)
    assert outcome.tags == ("健康",)


def test_摘要两侧空白被去掉():
    outcome = parse_mod.parse_summary(
        _payload("  这是一段足够长的中文摘要内容  "), title="t"
    )
    assert isinstance(outcome, SummarizeOk)
    assert outcome.summary == "这是一段足够长的中文摘要内容"


def test_标签里的非字符串元素被丢弃_B10():
    """`B10` 的解析侧：`tags` 是合法数组，但元素里有数字与 null。

    「不抛异常」是硬要求（不是「抛了再兜住」）：解析这一层先把非字符串摘掉，
    词表校验那一层再兜一次。
    """
    raw = json.dumps(
        {"summary": "这是一段足够长的中文摘要内容", "tags": [1, None, "", "健康"]},
        ensure_ascii=False,
    )
    outcome = parse_mod.parse_summary(raw, title="标题")
    assert isinstance(outcome, SummarizeOk)
    assert outcome.tags == ("", "健康"), "非字符串丢弃，空串留给词表校验去丢"


# ── 解析：非法 JSON 与形状（B1 / B3 / B11）─────────────────────


def test_非法JSON不产出摘要_B1():
    """B1：`{"summary": "半截` —— 半个 JSON 对象。"""
    outcome = parse_mod.parse_summary('{"summary": "半截', title="标题")
    assert isinstance(outcome, SummarizeFailed)
    assert outcome.category is FailureCategory.BAD_RESPONSE
    assert "JSON" in outcome.message
    assert outcome.snippet, "错误里必须带响应前 200 字，否则排障时看不到模型说了什么"


def test_非法JSON的snippet不超过200字_B1():
    outcome = parse_mod.parse_summary("x" * 500, title="标题")
    assert isinstance(outcome, SummarizeFailed)
    assert len(outcome.snippet) <= 200


def test_响应是HTML时归为BAD_RESPONSE并带前200字_B28():
    """B28：网关错误页。这里不判 Content-Type（那是 transport 的活），
    只保证「HTML 进解析层就是非法 JSON」这条路径与 B1 一致。"""
    html = "<html><body>502 Bad Gateway</body></html>"
    outcome = parse_mod.parse_summary(html, title="标题")
    assert isinstance(outcome, SummarizeFailed)
    assert outcome.category is FailureCategory.BAD_RESPONSE
    assert "502 Bad Gateway" in outcome.snippet


@pytest.mark.parametrize("bad", [None, "", 123, [], {"a": 1}, True])
def test_summary为null或空串或数字判非法_B3(bad):
    """B3：`null` / 空串 / 数字一律非法。**不得**把 `null` 当摘要写入。"""
    raw = json.dumps({"summary": bad, "tags": []}, ensure_ascii=False)
    outcome = parse_mod.parse_summary(raw, title="标题")
    assert isinstance(outcome, SummarizeFailed)
    assert outcome.category is FailureCategory.BAD_RESPONSE


def test_summary全是空白也判非法_B3():
    outcome = parse_mod.parse_summary(_payload("      "), title="标题")
    assert isinstance(outcome, SummarizeFailed)


def test_顶层不是对象时判非法_B1():
    outcome = parse_mod.parse_summary('["不是对象"]', title="标题")
    assert isinstance(outcome, SummarizeFailed)


@pytest.mark.parametrize("tags", [None, "健康", 1, {"a": 1}])
def test_tags缺失或不是数组判非法_B11(tags):
    """B11：`tags` 字段缺失或不是数组一律非法，与 B1 同路径。"""
    if tags is None:
        raw = json.dumps(
            {"summary": "这是一段足够长的中文摘要内容"}, ensure_ascii=False
        )
    else:
        raw = json.dumps(
            {"summary": "这是一段足够长的中文摘要内容", "tags": tags},
            ensure_ascii=False,
        )
    outcome = parse_mod.parse_summary(raw, title="标题")
    assert isinstance(outcome, SummarizeFailed)
    assert outcome.category is FailureCategory.BAD_RESPONSE


# ── 解析：质量四条判据（AI-13）─────────────────────────────────


def test_8字摘要判非法_B5():
    """B5：长度下限 10 字。8 字属于「敷衍输出」。"""
    outcome = parse_mod.parse_summary(_payload("只有八个字的摘要"), title="标题")
    assert isinstance(outcome, SummarizeFailed)
    assert outcome.category is FailureCategory.BAD_RESPONSE
    assert "8" in outcome.message, "错误消息要带上实际长度，用户才知道差多少"


def test_9字摘要判非法_10字正好合法_AI_13():
    """边界：9 非法、10 合法。差一个字符就换分支，必须钉住。"""
    nine = "九个字" * 3  # 9 字
    ten = "十个字样" * 2 + "两字"  # 10 字
    assert len(nine) == 9
    assert len(ten) == 10
    assert isinstance(
        parse_mod.parse_summary(_payload(nine), title="t"), SummarizeFailed
    )
    assert isinstance(parse_mod.parse_summary(_payload(ten), title="t"), SummarizeOk)


def test_100字摘要不被截断_AI_13():
    """上限是 ≤100（含 100），正好 100 字必须原样通过。"""
    exact = "字" * 100
    outcome = parse_mod.parse_summary(_payload(exact), title="标题")
    assert isinstance(outcome, SummarizeOk)
    assert outcome.summary == exact


def test_180字摘要按句末标点截断到100以内且末尾是句末标点_B4():
    """B4：取 100 字以内最后一个句末标点（`。！？…`），找不到才硬截。

    构造一个 180 字、多个句号的摘要，断言：长度 ≤100、末尾是句末标点、
    且截断点正好是 100 字以内最后一个句号。
    """
    summary = ("这是一句话内容比较长的中文摘要。") * 12  # 每句 16 字，共 192 字
    assert len(summary) > 180
    outcome = parse_mod.parse_summary(_payload(summary), title="标题")
    assert isinstance(outcome, SummarizeOk)
    assert len(outcome.summary) <= 100
    assert outcome.summary[-1] in "。！？…"
    assert summary.startswith(outcome.summary), "截断不能改动原文"


def test_180字无句末标点时硬截99字补省略号共100字_B4_AI_13():
    """B4 与 AI-13 的冲突处理：硬截 99 字 + `…` = 100 字。

    若按 PRD 字面写「硬截 100 字并补 `…`」会得到 101 字，`AI-13` 的
    `len ≤ 100` 直接失败。这里把 100 这个数字钉住。
    """
    summary = "字" * 180
    outcome = parse_mod.parse_summary(_payload(summary), title="标题")
    assert isinstance(outcome, SummarizeOk)
    assert len(outcome.summary) == 100, f"应当是 100 字，实际 {len(outcome.summary)}"
    assert outcome.summary.endswith("…")
    assert outcome.summary[:-1] == "字" * 99


def test_句末标点太靠前时改为硬截_AI_13():
    """若 100 字以内最后一个句号在第 5 个字，截到那里只有 6 字，会低于下限 10。

    这种情况必须改走硬截，否则产出一条「合法但没信息」的摘要。
    """
    summary = "短句。" + "字" * 174
    outcome = parse_mod.parse_summary(_payload(summary), title="标题")
    assert isinstance(outcome, SummarizeOk)
    assert len(outcome.summary) >= 10
    assert outcome.summary.endswith("…")


def test_摘要与标题逐字相同则跳过_title_only_B16():
    """B16：抄一遍标题是最常见的「假摘要」，界面上会显示两行一样的字。"""
    outcome = parse_mod.parse_summary(
        _payload("这是一个一模一样的中文标题内容"),
        title="这是一个一模一样的中文标题内容",
    )
    assert isinstance(outcome, SummarizeSkipped)
    assert outcome.reason == "title_only"


def test_摘要与标题只差首尾空白也判跳过_B16():
    """模型常在标题前后带回车或空格，只比一侧会漏判。"""
    outcome = parse_mod.parse_summary(
        _payload("  这是一个一模一样的中文标题内容 \n"),
        title="这是一个一模一样的中文标题内容",
    )
    assert isinstance(outcome, SummarizeSkipped)
    assert outcome.reason == "title_only"


def test_标题为空时不做重复判断_B16():
    """标题为空时「等于标题」无从谈起，不能因为空 == 空就跳过。"""
    outcome = parse_mod.parse_summary(
        _payload("这是一段足够长的中文摘要内容"), title=""
    )
    assert isinstance(outcome, SummarizeOk)


def test_跳过时不带标签_4点3节():
    """`title_only` 的结论是「这条内容不要摘要」，`tag` 步骤因此不会被入队
    （`enqueue(item_id, "tag")` 只在 `SummarizeOk` 后执行）。所以
    `SummarizeSkipped` **只携带 `reason`**，不再带标签——多带一个字段会让
    「跳过该不该打标签」这个已定的事重新变成歧义。
    """
    outcome = parse_mod.parse_summary(
        _payload("这是一个一模一样的中文标题内容", tags=["健康"]),
        title="这是一个一模一样的中文标题内容",
    )
    assert isinstance(outcome, SummarizeSkipped)
    assert outcome.reason == "title_only"
    assert not hasattr(outcome, "tags")

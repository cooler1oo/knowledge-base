"""AI 层标签校验：归一化、去重、类型过滤、词表外丢弃、最多 3 个、同名多父。

两条硬要求决定这一组用例的写法：

1. **`B10` 要求「不抛异常」而不是「抛了再兜住」**。所以 `validate_tags` 对
   `None`、非字符串元素、空串一律静默丢弃，不用 try/except 包住整段。
2. **归一化是「去首尾空白 + 转小写」**，用于比对，不用于落库。落库写的是
   词表里那一行的 `name`（`AI相关` 而不是 `ai相关`）——写归一化后的名字会
   在界面上显示全小写，用户看到的词与他自己建的词不一样。

`F3` 的「同名多父取 id 小者」是唯一需要往外传信息的一条：静默丢一半会让
用户看到「AI 只打了一个标签」却不知道为什么，所以 `ambiguous` 单独回传，
worker 写进 `props.aiTagAmbiguous`。
"""

from __future__ import annotations

import pytest

from kb.ai.validate import ValidatedTags, normalize_name, validate_tags

# 一个贴近真实的词表：两级结构，含一个跨父重名的情况（F3）
VOCAB: tuple[tuple[int, str, str], ...] = (
    (1, "AI相关", ""),
    (2, "工作赚钱", ""),
    (3, "学点东西", ""),
    (4, "生活过日子", ""),
    (5, "健康", ""),
    (6, "好看的东西", ""),
    (7, "好玩放松", ""),
    (8, "先存着以后用", ""),
    (9, "skill和插件", "AI相关"),
    (10, "穿搭", "好看的东西"),
    (11, "好物", "好看的东西"),
    (12, "ai相关", "工作赚钱"),  # 与 1 号归一化后同名，不同父（F3）
)


# ── 归一化 ──────────────────────────────────────────────────────


def test_归一化是去首尾空白加小写():
    assert normalize_name("  AI相关 ") == "ai相关"
    assert normalize_name("SkIlL插件") == "skill插件"
    assert normalize_name("\t好物\n") == "好物"


# ── 正常命中 ────────────────────────────────────────────────────


def test_一个合法标签命中对应id():
    result = validate_tags(["健康"], VOCAB)
    assert isinstance(result, ValidatedTags)
    assert result.tag_ids == (5,)
    assert result.ambiguous is False


def test_子标签命中子标签id而不是父id_F2():
    """F2：词表含父「AI相关」与子「skill和插件」，模型返回子标签名 → 写子标签 id。"""
    result = validate_tags(["skill和插件"], VOCAB)
    assert result.tag_ids == (9,), "命中子标签就必须写子标签 id，不能挂到父上"


def test_空标签数组返回空列表不报错_A5():
    """A5：模型返回空数组是合法输出，`tag` 任务照样 done。"""
    result = validate_tags([], VOCAB)
    assert result.tag_ids == ()
    assert result.ambiguous is False


# ── 词表外丢弃（B6 / F1）───────────────────────────────────────


def test_词表外标签丢弃不报错_B6():
    """B6：`["穿搭","词表里没有的","好物"]` → 只写「穿搭」「好物」两行。"""
    result = validate_tags(["穿搭", "词表里没有的", "好物"], VOCAB)
    assert result.tag_ids == (10, 11)


def test_词表为空时返回空列表不抛异常_F1():
    """F1：词表为空时 AI 只出摘要不打标签，这是设计上接受的结果。"""
    result = validate_tags(["健康", "AI相关"], ())
    assert result.tag_ids == ()


def test_全部词表外时返回空列表():
    result = validate_tags(["甲", "乙", "丙"], VOCAB)
    assert result.tag_ids == ()


# ── 去重与归一化匹配（B8 / B9）─────────────────────────────────


def test_完全重复去重为1_B8():
    """B8：`["AI相关","AI相关","ai相关"]` → 去重后 1 行。"""
    result = validate_tags(["AI相关", "AI相关", "ai相关"], VOCAB)
    assert len(result.tag_ids) == 1


def test_空白与大小写差异归一化到同一tag_B9():
    """B9：`["AI相关 ", " ai相关"]` → 归一化后匹配同一个 `tag_id`，写 1 行。"""
    result = validate_tags(["AI相关 ", " ai相关"], VOCAB)
    assert len(result.tag_ids) == 1


def test_同一父下重名不算ambiguous_只有跨父才算_F3():
    """同一个父下面的重名在库里根本不存在（唯一索引拦住了），所以
    `ambiguous` 只在「归一化后同名、父不同」时为真。"""
    single: tuple[tuple[int, str, str], ...] = ((1, "AI相关", ""),)
    assert validate_tags(["AI相关"], single).ambiguous is False


# ── 最多 3 个（B7）────────────────────────────────────────────


def test_5个合法标签只取前3_B7():
    """B7：只写前 3 个，`item_tags` 该条目 `origin='ai'` 行数 = 3。

    「前 3 个」按**模型给出的顺序**，不按词表 id 重排——模型把最重要的放前面，
    重排等于丢掉了它的排序意图。
    """
    tags = ["健康", "穿搭", "好物", "AI相关", "工作赚钱"]
    result = validate_tags(tags, VOCAB)
    assert result.tag_ids == (5, 10, 11), f"应当是前三 3 个，实际 {result.tag_ids}"


def test_前3个里有词表外的要跳过继续取_AI_02():
    """若前 3 个里混了词表外的名字，不能只写 1 个——应当继续往后取，
    直到凑满 3 个或候选耗尽。否则模型偶尔多吐一个词就永久少一个标签。
    """
    tags = ["不存在的", "健康", "也不存在", "穿搭", "好物"]
    result = validate_tags(tags, VOCAB)
    assert result.tag_ids == (5, 10, 11)


def test_去重发生在截断之前_B7_B8():
    """先截断再去重会得到 2 个（`健康`、`健康`被去重），先去重再截断才是 3 个。"""
    tags = ["健康", "健康", "穿搭", "好物", "AI相关"]
    result = validate_tags(tags, VOCAB)
    assert result.tag_ids == (5, 10, 11)


# ── 类型过滤（B10）────────────────────────────────────────────


def test_非字符串元素丢弃_B10():
    """B10：`[1, null, "", "健康"]` → 只写「健康」；**不抛异常**。"""
    raw: list[object] = [1, None, "", "健康"]
    result = validate_tags(raw, VOCAB)
    assert result.tag_ids == (5,)


@pytest.mark.parametrize("bad", [None, 1, True, [], {}, 1.5])
def test_单个非字符串输入不抛异常_B10(bad):
    result = validate_tags([bad], VOCAB)
    assert result.tag_ids == ()


def test_入参是None时按空数组处理_B10():
    """B10 要求「不抛异常而不是抛了再兜住」。`None` 当空数组是这一条的直译。"""
    assert validate_tags(None, VOCAB).tag_ids == ()


def test_词表是None时按空处理_B10():
    assert validate_tags(["健康"], None).tag_ids == ()


def test_词表是字符串时按空处理_B10():
    """传错类型（把词表当字符串传）不能让整条 `tag` 步骤崩掉。"""
    assert validate_tags(["健康"], "健康").tag_ids == ()


def test_词表条目用VocabularyEntry对象也认():
    """worker 侧从库里读的是三元组，这里直接喂 `VocabularyEntry` 也要认。

    两种形状都支持的理由：`store.tags.vocabulary` 返回三元组（不 import `kb.ai`），
    而 `validate_tags` 需要收口成对象才能判「同名多父」。让两侧都自然。
    """
    from kb.ai.validate import VocabularyEntry

    vocab = (VocabularyEntry(5, "健康"), VocabularyEntry(10, "穿搭", "好看的东西"))
    assert validate_tags(["健康", "穿搭"], vocab).tag_ids == (5, 10)


@pytest.mark.parametrize(
    "bad_entry",
    [
        (True, "健康", ""),  # tag_id 是 bool（bool 是 int 的子类，必须显式拦）
        ("5", "健康", ""),  # tag_id 是字符串
        (5, 123, ""),  # name 不是字符串
        (5,),  # 长度不足
        "健康",  # 不是序列
    ],
)
def test_词表里的坏条目被跳过_B10(bad_entry):
    """词表行类型不可信（`tags` 表用户可手改）时跳过坏行，不整条崩掉。"""
    assert validate_tags(["健康"], [bad_entry]).tag_ids == ()


def test_全是空白字符的标签名被丢弃():
    """`"   "` 归一化后是空串，不是任何词表条目。"""
    assert validate_tags(["   ", "\t"], VOCAB).tag_ids == ()


# ── 同名多父（F3）──────────────────────────────────────────────


def test_同名多父取id小者并标ambiguous_F3():
    """F3：词表里「AI相关」与另一父标签下也有「ai相关」→ 取 `id` 较小者，
    并且 `props` 记 `aiTagAmbiguous=true`（同名多父时不静默丢一半）。
    """
    result = validate_tags(["AI相关"], VOCAB)
    assert result.tag_ids == (1,), "同名多父时取 id 小的那一行"
    assert result.ambiguous is True


def test_ambiguous_只标一次不影响其余标签_F3():
    """`ambiguous` 是条目级的一个布尔，不是「每个标签一个」。"""
    result = validate_tags(["AI相关", "健康"], VOCAB)
    assert result.tag_ids == (1, 5)
    assert result.ambiguous is True


def test_无重名时ambiguous为假():
    result = validate_tags(["健康", "穿搭"], VOCAB)
    assert result.ambiguous is False


# ── 落库顺序 ───────────────────────────────────────────────────


def test_返回的id顺序与模型给的顺序一致_AI_02():
    result = validate_tags(["好物", "健康"], VOCAB)
    assert result.tag_ids == (11, 5), "不能按 id 重排，模型给的顺序有意义"

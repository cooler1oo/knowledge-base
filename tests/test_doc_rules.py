"""③ 全量：所有文档都得过规则检查。PR 上也会跑这条（只查改动文件）。"""

from pathlib import Path

from tools.doc_checks import check_banned_words, run_checks


def test_docs_follow_project_rules() -> None:
    violations = run_checks()
    assert not violations, "文档不合规：\n" + "\n".join(str(v) for v in violations)


# ── 2026-11-27：同一类 bug 的第二处 ──────────────────────────
#
# reuse.py 的 mode_for 修好之后，发现 check_banned_words 里藏着同一个错误：
#   if p.parent.name != "tech": return []
# 只认一层目录，docs/tech/2026/x.md 的父目录叫 2026，于是禁用词检查静默失效。
#
# 这类「按父目录名判定」的写法在项目里可能还有别处，所以测试要覆盖到
# 「多层子目录」，而不只是「直系子目录」。


def test_禁用词检查在技术方案直系目录生效():
    text = "这个问题应该这样处理\n"
    assert check_banned_words(Path("docs/tech/a.md"), text), "直系目录没查出禁用词"


def test_禁用词检查在子目录里也要生效():
    """父目录叫 2026 不是逃脱检查的理由。"""
    text = "这个问题应该这样处理\n"
    violations = check_banned_words(Path("docs/tech/2026/a.md"), text)
    assert violations, "子目录里的技术文档漏过了禁用词检查"


def test_禁用词检查在多层子目录里也要生效():
    text = "理论上这样能跑\n"
    violations = check_banned_words(Path("docs/tech/2026/11/a.md"), text)
    assert violations, "多层子目录里的技术文档漏过了禁用词检查"


def test_禁用词检查不该管非技术目录():
    """prd/specs/research 里这些词多为引用用户原话，查了会大量误报。"""
    text = "用户原话是「大概是蓝色」\n"
    assert check_banned_words(Path("docs/prd/a.md"), text) == []
    assert check_banned_words(Path("docs/specs/a.md"), text) == []


def test_引用块里的禁用词不算作者违规():
    """引用块（> 开头）是别人的话，不是作者判断。

    规则本意是「作者不要用模糊词下结论」。把「模型转写原文」或「用户原话」
    判成违规，会逼着人改写引用——那就毁掉了原文可比对的价值。
    这类误报是规则被关掉的典型起点。
    """
    text = "下面这句是原文：\n\n> 到页面渲染完成，通常只有一两百毫秒。\n\n作者自己的判断在这里。\n"
    assert check_banned_words(Path("docs/tech/a.md"), text) == []


def test_引用块之外的禁用词照样要报():
    """跳过引用不等于放过正文。"""
    text = "> 这是引用，含通常。\n\n这个方案通常能跑通。\n"
    violations = check_banned_words(Path("docs/tech/a.md"), text)
    assert len(violations) == 1, f"只该报正文那一行：{violations}"

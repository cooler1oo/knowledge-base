"""reuse 规则验收探针：逐项确认「该拦的拦住、不该拦的不误报」。

这不是单元测试的替代品，而是交付前的人工验收脚本——按 AGENTS.md 8.1 的
每条规则口径逐项实测，输出可读的对照表。

跑法：python scripts/verify_reuse_rules.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.doc_checks import check_banned_words, docs_files  # noqa: E402
from tools.doc_checks.reuse import audit_text, mode_for  # noqa: E402

HEADER = "| 组件 | 候选 | 星数 | 许可 | 结论 | 运行环境 | 理由 |"
SEP = "| --- | --- | --- | --- | --- | --- | --- |"


def doc(*rows: str) -> str:
    return "\n".join(["# 文档", "", "## 复用调研", "", HEADER, SEP, *rows, ""])


def ok_row(comp: str = "A", repo: str = "owner/repo") -> str:
    return f"| {comp} | {repo} | 1000 | MIT | 复用 | proot | 检索过同类项目无现成 |"


CASES: list[tuple[str, str, str, bool]] = [
    # (名称, 文档内容, 期望命中的关键词, 是否该报)
    ("缺章节", "# 文档\n\n没有调研\n", "缺「## 复用调研」", True),
    ("合规文档", doc(ok_row()), "", False),
    (
        "候选不是 owner/repo",
        doc("| A | 某开源库 | 100 | MIT | 复用 | proot | 检索过同类项目无现成 |"),
        "owner/repo",
        True,
    ),
    (
        "星数不是数字",
        doc("| A | owner/a | 很多 | MIT | 复用 | proot | 检索过同类项目无现成 |"),
        "星数",
        True,
    ),
    (
        "结论是自己搞",
        doc("| A | owner/a | 100 | MIT | 自己搞 | proot | 检索过同类项目无现成 |"),
        "结论",
        True,
    ),
    (
        "理由太短",
        doc("| A | owner/a | 100 | MIT | 复用 | proot | 简单 |"),
        "理由",
        True,
    ),
    (
        "复用缺运行环境",
        doc("| A | owner/a | 100 | MIT | 复用 | - | 检索过同类项目无现成 |"),
        "运行环境",
        True,
    ),
    (
        "理由含敷衍词",
        doc("| A | owner/a | 100 | MIT | 复用 | proot | 感觉不合适所以不用 |"),
        "敷衍词",
        True,
    ),
    (
        "列数不符（竖线未转义）",
        doc("| A | owner/a | 100 | MIT | 复用 | proot | 理由含 A|B |"),
        "列",
        True,
    ),
    (
        "参考没说取什么弃什么",
        doc("| A | owner/a | 100 | MIT | 参考 | proot | 检索过同类项目可以参考 |"),
        "参考",
        True,
    ),
    (
        "全自写无检索痕迹",
        doc(
            "| A | owner/a | 100 | MIT | 自写 | proot | 自己实现更快 |",
            "| B | owner/b | 100 | MIT | 自写 | proot | 自己实现更省 |",
        ),
        "检索",
        True,
    ),
]


def run_reuse_cases() -> tuple[int, int]:
    passed = failed = 0
    print("=" * 68)
    print("一、复用调研规则（audit_text / docs/tech 走 strict）")
    print("=" * 68)
    for name, text, keyword, should_report in CASES:
        violations = audit_text(text, "docs/tech/x.md")
        hit = any(keyword in str(v) for v in violations) if keyword else False
        good = hit if should_report else (not violations)
        mark = "通过" if good else "失败"
        detail = f"{len(violations)} 条违规"
        if violations and should_report is False:
            detail += f"：{violations[0]}"
        print(f"  [{mark}] {name:22} {detail}")
        passed, failed = (passed + 1, failed) if good else (passed, failed + 1)
    return passed, failed


def run_path_cases() -> tuple[int, int]:
    passed = failed = 0
    print()
    print("=" * 68)
    print("二、目录判定（子目录必须与顶层同待遇）")
    print("=" * 68)
    cases = [
        ("docs/tech/a.md", "strict"),
        ("docs/tech/2026/a.md", "strict"),
        ("docs/tech/2026/11/a.md", "strict"),
        ("docs/prd/a.md", "lite"),
        ("docs/prd/2026/a.md", "lite"),
        ("docs/research/x/y/a.md", "lite"),
        ("docs/other/a.md", None),
    ]
    for path, want in cases:
        got = mode_for(path)
        good = got == want
        print(f"  [{'通过' if good else '失败'}] {path:28} → {got}（期望 {want}）")
        passed, failed = (passed + 1, failed) if good else (passed, failed + 1)
    return passed, failed


def run_banned_cases() -> tuple[int, int]:
    passed = failed = 0
    print()
    print("=" * 68)
    print("三、禁用词（含子目录与引用块）")
    print("=" * 68)
    cases = [
        ("docs/tech/a.md", "这个方案应该这样\n", True, "顶层正文"),
        ("docs/tech/2026/a.md", "这个方案应该这样\n", True, "子目录正文"),
        ("docs/tech/2026/11/a.md", "理论上能跑\n", True, "多层子目录正文"),
        ("docs/prd/a.md", "用户说大概是蓝色\n", False, "非技术目录不查"),
        ("docs/tech/a.md", "> 原文含通常二字\n", False, "引用块跳过"),
        (
            "docs/tech/a.md",
            "> 引用含通常\n\n正文也含通常\n",
            True,
            "引用跳过但正文照查",
        ),
    ]
    for path, text, want_report, desc in cases:
        violations = check_banned_words(Path(path), text)
        good = bool(violations) == want_report
        print(f"  [{'通过' if good else '失败'}] {desc:20} {len(violations)} 条")
        passed, failed = (passed + 1, failed) if good else (passed, failed + 1)
    return passed, failed


def run_scan_case() -> tuple[int, int]:
    print()
    print("=" * 68)
    print("四、文件发现（递归到子目录）")
    print("=" * 68)
    with tempfile.TemporaryDirectory() as td:
        fake = Path(td) / "docs"
        (fake / "tech" / "2026" / "11").mkdir(parents=True)
        (fake / "tech" / "top.md").write_text("# t\n", encoding="utf-8")
        (fake / "tech" / "2026" / "d.md").write_text("# d\n", encoding="utf-8")
        (fake / "tech" / "2026" / "11" / "x.md").write_text("# x\n", encoding="utf-8")

        import tools.doc_checks as dc

        old = dc.DOC_ROOT
        dc.DOC_ROOT = fake
        try:
            names = {p.name for p in docs_files()}
        finally:
            dc.DOC_ROOT = old

    want = {"top.md", "d.md", "x.md"}
    good = want <= names
    print(
        f"  [{'通过' if good else '失败'}] 找到 {sorted(names)}，期望含 {sorted(want)}"
    )
    return (1, 0) if good else (0, 1)


def main() -> int:
    total_p = total_f = 0
    for fn in (run_reuse_cases, run_path_cases, run_banned_cases, run_scan_case):
        p, f = fn()
        total_p += p
        total_f += f

    print()
    print("=" * 68)
    print(f"合计：通过 {total_p} 项，失败 {total_f} 项")
    print("=" * 68)

    print()
    print("五、真实仓库现状")
    files = docs_files()
    print(f"  扫描文档数：{len(files)}")
    strict = sum(1 for p in files if mode_for(p) == "strict")
    print(f"  其中 strict：{strict}，lite：{len(files) - strict}")

    return 0 if total_f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

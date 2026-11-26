"""复用调研检查：把「先找开源、不重复造轮子」变成可执行校验。

背景：这条规则原本只在会话里口头要求，靠 agent 自觉。结果是抓取器被手写了
120 行，而一个 MIT 库三行就能替代。能机器查的部分不该靠自觉。

可机器判断的（本模块负责）：
- 技术方案必须有「复用调研」章节，且是固定列数的表格
- 结论必须落在枚举内（复用/参考/不采用/自写）
- 结论为「自写」时必须给理由，且理由不能是敷衍词
- 选「复用」的候选，许可证必须在宽松白名单内
- 星数低于阈值的候选，必须在理由里标出风险

不可机器判断、留给人工（本模块不假装能查）：
- 调研是否穷尽（只搜到一个冷门项目也算"查过"）
- 复用与自写的取舍是否正确
- 上游项目的质量与可持续性
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import Violation, _rel

SECTION_KEYWORD = "复用调研"
COLUMNS = ("组件", "候选", "星数", "许可", "结论", "理由")
CONCLUSIONS = {"复用", "参考", "不采用", "自写"}

# 宽松许可：可商用、可闭源修改
PERMISSIVE = {
    "MIT",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
    "0BSD",
    "Unlicense",
    "MPL-2.0",
    "PSF-2.0",
    "Zlib",
}
# 传染性许可：个人自用可接受，但必须写进理由
COPYLEFT = {"AGPL-3.0", "GPL-3.0", "GPL-2.0", "LGPL-3.0", "LGPL-2.1"}

# 「自写」的理由里出现这些词，等于没写理由
FILLER = ("简单", "没必要", "顺手", "方便", "很快", "自己写", "小功能", "工作量小")

MIN_REASON_LEN = 8
LOW_STAR_THRESHOLD = 100
LOW_STAR_MARKS = ("⚠", "小众", "低星", "停更")


@dataclass
class ReuseRow:
    component: str
    candidate: str
    stars: str
    license: str
    conclusion: str
    reason: str
    line: int


@dataclass
class ReuseAudit:
    found_section: bool = False
    rows: list[ReuseRow] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)


def _split_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c)


def _section_lines(text: str) -> tuple[bool, list[tuple[int, str]]]:
    """取出「复用调研」二级章节的行，带原始行号。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and SECTION_KEYWORD in line:
            start = i
            break
    if start is None:
        return False, []

    level = len(lines[start]) - len(lines[start].lstrip("#"))
    out: list[tuple[int, str]] = []
    in_fence = False
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if line.startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("#"):
            nxt = len(line) - len(line.lstrip("#"))
            if nxt <= level:
                break
        out.append((j + 1, line))
    return True, out


def parse_audit(text: str) -> ReuseAudit:
    audit = ReuseAudit()
    found, body = _section_lines(text)
    audit.found_section = found
    if not found:
        return audit

    header: list[str] | None = None
    for lineno, line in body:
        if not line.startswith("|"):
            continue
        cells = _split_cells(line)
        if _is_separator(cells):
            continue
        if header is None:
            header = cells
            continue
        if len(cells) != len(COLUMNS):
            continue
        audit.rows.append(
            ReuseRow(
                component=cells[0],
                candidate=cells[1],
                stars=cells[2],
                license=cells[3],
                conclusion=cells[4],
                reason=cells[5],
                line=lineno,
            )
        )
    return audit


def audit_text(text: str, path: str) -> list[Violation]:
    out: list[Violation] = []
    audit = parse_audit(text)

    if not audit.found_section:
        out.append(
            Violation(
                path,
                1,
                "复用调研",
                f"缺「## {SECTION_KEYWORD}」章节：技术选型必须有复用对照表",
            )
        )
        return out

    if not audit.rows:
        out.append(Violation(path, 1, "复用调研", "章节存在但没有数据行"))
        return out

    for row in audit.rows:
        out.extend(_check_row(row, path))

    # 全部「自写」且理由里没有检索痕迹，等于没调研。
    # 注意：单条「自写」不需要这条（上面已单独校验理由长度与敷衍词）。
    if len(audit.rows) >= 2 and all(r.conclusion == "自写" for r in audit.rows):
        has_evidence = any(
            any(k in r.reason for k in ("检索", "搜索", "查过", "搜过"))
            for r in audit.rows
        )
        if not has_evidence:
            out.append(
                Violation(
                    path,
                    audit.rows[0].line,
                    "复用调研",
                    "全部结论都是「自写」且理由里没有检索痕迹：须说明搜过什么关键词",
                )
            )

    return out


def _check_row(row: ReuseRow, path: str) -> list[Violation]:
    out: list[Violation] = []
    ln = row.line

    if row.conclusion not in CONCLUSIONS:
        out.append(
            Violation(
                path,
                ln,
                "复用调研",
                f"结论「{row.conclusion}」不在 {'/'.join(sorted(CONCLUSIONS))} 内",
            )
        )
        return out

    if not re.fullmatch(r"\d+", row.stars.replace(",", "")):
        out.append(Violation(path, ln, "复用调研", f"星数「{row.stars}」不是数字"))
    elif row.conclusion != "自写":
        # 「自写」行的星数指候选项目的星数，0 表示没有候选，不该要求标 ⚠
        stars = int(row.stars.replace(",", ""))
        if stars < LOW_STAR_THRESHOLD and not any(
            m in row.reason for m in LOW_STAR_MARKS
        ):
            out.append(
                Violation(
                    path,
                    ln,
                    "复用调研",
                    f"星数 {stars} 低于 {LOW_STAR_THRESHOLD}，理由里须标 ⚠ 说明风险",
                )
            )

    if row.conclusion in {"复用", "参考"}:
        if row.license not in PERMISSIVE | COPYLEFT:
            out.append(
                Violation(
                    path, ln, "复用调研", f"许可「{row.license}」不在已知白名单内"
                )
            )
        elif row.license in COPYLEFT and row.license not in row.reason:
            out.append(
                Violation(
                    path,
                    ln,
                    "复用调研",
                    f"选用了 {row.license}，理由里须写明传染性许可的影响",
                )
            )

    if row.conclusion == "自写":
        reason = row.reason.strip()
        if len(reason) < MIN_REASON_LEN:
            out.append(
                Violation(path, ln, "复用调研", "结论为「自写」但理由过短（少于 8 字）")
            )
        hit = [w for w in FILLER if w in reason]
        if hit:
            out.append(
                Violation(
                    path,
                    ln,
                    "复用调研",
                    f"「自写」的理由含敷衍词「{hit[0]}」，须说明搜过什么、为什么不能用",
                )
            )

    return out


def check_file(p: Path) -> list[Violation]:
    return audit_text(p.read_text(encoding="utf-8"), _rel(p))

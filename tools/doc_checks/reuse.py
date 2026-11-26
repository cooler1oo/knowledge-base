"""复用调研检查：把「先找现成的、不重复造轮子」变成可执行校验。

适用范围是**所有设计场景**，不只是技术方案：
- tech：技术选型，调研对象是代码库（要星数、要许可）
- prd：需求设计，调研对象是现成产品与竞品（星数许可写 `-`）
- specs：信息架构，调研对象是同类产品的组织方式

两种严格度共用同一张六列表，区别只在 lite 模式不校验星数与许可。

可机器判断的：
- 章节存在、表格列数正确
- 结论在枚举内
- 任何一种结论都必须给理由，且理由不能是敷衍词
- strict 模式下候选名须为 `owner/repo`（可查证）、星数为数字、许可在白名单
- 星数低于阈值须标 ⚠
- 全部「自写/无参考」须留下检索痕迹

不可机器判断、留给人工：
- 调研是否穷尽（搜到一个冷门项目也算查过）
- 取舍是否正确
- 上游项目的质量与可持续性
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import Violation, _rel

SECTION_KEYWORD = "复用调研"
COLUMNS = ("组件", "候选", "星数", "许可", "结论", "理由")
CONCLUSIONS = {"复用", "参考", "不采用", "自写"}

# 每个目录对应一种严格度
STRICT_DIRS = {"tech"}
LITE_DIRS = {"prd", "specs", "research"}

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
COPYLEFT = {"AGPL-3.0", "GPL-3.0", "GPL-2.0", "LGPL-3.0", "LGPL-2.1"}
# 源码可得但有限制（商用需授权/限时转开源）。自用可接受，但必须写明限制。
SOURCE_AVAILABLE = {"BUSL-1.1", "SSPL-1.0", "Elastic-2.0", "BSL-1.1"}

FILLER = (
    "简单",
    "没必要",
    "顺手",
    "方便",
    "很快",
    "自己写",
    "小功能",
    "工作量小",
    "不好用",
    "不合适",
    "不满足",
)

MIN_REASON_LEN = 8
LOW_STAR_THRESHOLD = 100
LOW_STAR_MARKS = ("⚠", "小众", "低星", "停更")

REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
EVIDENCE_WORDS = ("检索", "搜索", "查过", "搜过", "调研", "对比")
LITE_PLACEHOLDER = {"-", "—", "n/a", "N/A", "无"}

# 历史文档豁免：规则上线前写的文档没有这张表，要求补写不现实。
# 但豁免必须显式写在文档里，不能默默跳过——否则等于规则失效。
EXEMPT_MARK = "<!-- reuse-audit: exempt -->"
EXEMPT_REASON_HINT = "复用调研豁免"


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


def mode_for(path: str | Path) -> str | None:
    """按目录决定严格度。返回 strict / lite / None（不校验）。"""
    p = Path(path)
    name = p.parent.name if p.suffix else p.name
    if name in STRICT_DIRS:
        return "strict"
    if name in LITE_DIRS:
        return "lite"
    return None


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


def audit_text(text: str, path: str, mode: str = "strict") -> list[Violation]:
    out: list[Violation] = []
    audit = parse_audit(text)

    if not audit.found_section:
        # 显式豁免：历史文档可以不走这张表，但必须在正文里写明为什么。
        if EXEMPT_MARK in text:
            return []
        out.append(
            Violation(
                path,
                1,
                "复用调研",
                f"缺「## {SECTION_KEYWORD}」章节：设计决策必须列出现成方案对照"
                f"（历史文档可加 {EXEMPT_MARK} 豁免）",
            )
        )
        return out

    if not audit.rows:
        out.append(Violation(path, 1, "复用调研", "章节存在但没有数据行"))
        return out

    for row in audit.rows:
        out.extend(_check_row(row, path, mode))

    # 全部「自写」且理由里没有检索痕迹，等于没调研。
    # 单条不识（上面已单独校验理由长度与敷衍词）。
    if len(audit.rows) >= 2 and all(r.conclusion == "自写" for r in audit.rows):
        if not any(any(k in r.reason for k in EVIDENCE_WORDS) for r in audit.rows):
            out.append(
                Violation(
                    path,
                    audit.rows[0].line,
                    "复用调研",
                    "全部结论都是「自写」且理由里没有检索痕迹：须说明搜过什么关键词",
                )
            )

    return out


def _is_placeholder(v: str) -> bool:
    return v.strip() in LITE_PLACEHOLDER


def _check_row(row: ReuseRow, path: str, mode: str) -> list[Violation]:
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

    # 理由：任何结论都要给，且不能敷衍。两项独立检查，不要互斥——
    # 「感觉不合适」既短又敷衍，两个问题都该报出来。
    reason = row.reason.strip()
    if len(reason) < MIN_REASON_LEN:
        out.append(
            Violation(
                path,
                ln,
                "复用调研",
                f"结论为「{row.conclusion}」但理由过短（少于 {MIN_REASON_LEN} 字）",
            )
        )
    hit = [w for w in FILLER if w in reason]
    if hit:
        out.append(
            Violation(
                path,
                ln,
                "复用调研",
                f"理由含敷衍词「{hit[0]}」，须说明查过什么、具体差在哪",
            )
        )

    # 「参考」必须说清参考了什么，否则等于免死金牌
    if row.conclusion == "参考" and not any(
        k in reason for k in ("取", "借", "学", "模", "用其", "沿用")
    ):
        out.append(
            Violation(
                path,
                ln,
                "复用调研",
                "结论为「参考」但没说参考了哪一部分（应写明取其什么、弃其什么）",
            )
        )

    if mode == "lite":
        return out

    # ── 以下仅 strict（tech）模式 ──
    # 「自写」行的候选栏语义是「查过什么、没有能用的」，不强求 owner/repo
    if row.conclusion != "自写" and not REPO_RE.match(row.candidate):
        out.append(
            Violation(
                path,
                ln,
                "复用调研",
                f"候选「{row.candidate}」不是 owner/repo 形式，无法查证",
            )
        )

    if not re.fullmatch(r"\d+", row.stars.replace(",", "")):
        out.append(Violation(path, ln, "复用调研", f"星数「{row.stars}」不是数字"))
    elif row.conclusion != "自写":
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
        lic = row.license
        # 复合许可（如 "MIT AND BUSL-1.1"）按其中最强的限制判定
        parts = {x.strip() for x in re.split(r"\s+AND\s+|\s+OR\s+|\s*/\s*", lic)}
        has_source_available = bool(parts & SOURCE_AVAILABLE)
        has_copyleft = bool(parts & COPYLEFT)

        if not (parts <= (PERMISSIVE | COPYLEFT | SOURCE_AVAILABLE)):
            out.append(
                Violation(path, ln, "复用调研", f"许可「{lic}」不在已知白名单内")
            )
        elif has_copyleft and not any(x in row.reason for x in parts & COPYLEFT):
            out.append(
                Violation(
                    path,
                    ln,
                    "复用调研",
                    f"选用了 {lic}，理由里须写明传染性许可的影响",
                )
            )
        elif (
            has_source_available
            and not any(x in row.reason for x in parts & SOURCE_AVAILABLE)
            and not any(k in row.reason for k in ("商用", "授权", "限制", "自用"))
        ):
            out.append(
                Violation(
                    path,
                    ln,
                    "复用调研",
                    f"选用了 {lic}（源码可得但有商用限制），理由里须写明该限制",
                )
            )

    return out


def check_file(p: Path, mode: str | None = None) -> list[Violation]:
    mode = mode or mode_for(p)
    if mode is None:
        return []
    return audit_text(p.read_text(encoding="utf-8"), _rel(p), mode)


# ── 联网核对（可选，用于 ③ main 的慢检查） ─────────────────


@dataclass
class RemoteCheck:
    candidate: str
    claimed_stars: int | None
    actual_stars: int | None
    claimed_license: str
    actual_license: str
    error: str | None = None

    @property
    def stars_mismatch(self) -> bool:
        if self.actual_stars is None or self.claimed_stars is None:
            return False
        # 星数会自然增长，只报「声称的比实际多」这种造假
        return self.claimed_stars > self.actual_stars * 1.2

    @property
    def license_mismatch(self) -> bool:
        if not self.actual_license:
            return False
        # GitHub 对复合许可（如 MIT AND BUSL-1.1）一律返回 NOASSERTION，
        # 这种情况无法比对，只能交由人工确认，不算文档写错。
        if self.actual_license == "NOASSERTION":
            return False
        return self.claimed_license != self.actual_license


def fetch_repo(candidate: str, timeout: int = 15) -> tuple[int | None, str, str | None]:
    """返回 (星数, 许可, 错误)。"""
    url = f"https://api.github.com/repos/{candidate}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "kb-doc-checks",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        return None, "", f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return None, "", type(e).__name__

    stars = data.get("stargazers_count")
    lic = (data.get("license") or {}).get("spdx_id") or ""
    return stars, lic, None


def verify_remote(text: str, path: str, mode: str = "strict") -> list[Violation]:
    """联网核对候选的星数与许可，只报「声称多于实际」与许可不符。"""
    if mode != "strict":
        return []
    audit = parse_audit(text)
    out: list[Violation] = []
    seen: dict[str, tuple[int | None, str]] = {}

    for row in audit.rows:
        if row.conclusion == "自写" or not REPO_RE.match(row.candidate):
            continue
        if row.candidate in seen:
            actual_stars, actual_lic = seen[row.candidate]
        else:
            actual_stars, actual_lic = fetch_repo(row.candidate)[:2]
            seen[row.candidate] = (actual_stars, actual_lic)
        if actual_stars is None:
            continue

        claimed = (
            int(row.stars.replace(",", ""))
            if row.stars.replace(",", "").isdigit()
            else None
        )
        if claimed is not None and claimed > actual_stars * 1.2:
            out.append(
                Violation(
                    path,
                    row.line,
                    "复用调研",
                    f"{row.candidate} 声称 {claimed} 星，实际 {actual_stars} 星（虚报）",
                )
            )
        if actual_lic and actual_lic != "NOASSERTION" and row.license != actual_lic:
            out.append(
                Violation(
                    path,
                    row.line,
                    "复用调研",
                    f"{row.candidate} 许可写 {row.license}，实际 {actual_lic}",
                )
            )
    return out

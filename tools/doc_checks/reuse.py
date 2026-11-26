"""复用调研检查：把「先找现成的、不重复造轮子」变成可执行校验。

适用范围是**所有设计场景**，不只是技术方案：
- tech：技术选型，调研对象是代码库（要星数、要许可、要运行环境）
- prd：需求设计，调研对象是现成产品与竞品（星数许可可写 `-`）
- specs：信息架构，调研对象是同类产品的组织方式

**本项目前提：个人非商用、单机自托管。** 因此校验重点不是「许可能不能商用」，
而是「能不能在目标环境跑起来」与「会不会产生持续付费」。
商用许可（GPL/AGPL/BUSL）在自用场景不触发义务，只要求写清即可，不做拦截。

可机器判断的：
- 章节存在、表格列数正确
- 结论在枚举内
- 任何结论都必须给理由，且理由不能是敷衍词
- strict 模式：候选名须 `owner/repo`、星数为数字、许可在白名单
- strict 模式：**复用项必须写明运行环境**（Termux/proot/Docker/二进制）
- strict 模式：**复用项必须写明是否有付费依赖**

不可机器判断、留给人工：
- 调研是否穷尽（搜到一个冷门项目也算查过）
- 取舍是否正确
- 上游项目的质量与可持续性
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import Violation, _rel

SECTION_KEYWORD = "复用调研"
# 七列：比商用场景多一列「运行环境」，因为本项目最常被否决的原因是跑不起来。
COLUMNS = ("组件", "候选", "星数", "许可", "结论", "运行环境", "理由")
LEGACY_COLUMNS_6 = ("组件", "候选", "星数", "许可", "结论", "理由")
CONCLUSIONS = {"复用", "参考", "不采用", "自写"}

# 运行环境标记：必须能看出它靠什么跑、是否需要编译
RUNTIME_MARKS = (
    "Termux",
    "proot",
    "Docker",
    "静态二进制",
    "纯Python",
    "纯 Python",
    "需编译",
    "aarch64",
    "ARM",
    "浏览器",
    "云托管",
    "不支持",
)
FREE_MARKS = ("免费", "免费额度", "无付费", "自托管免费", "开源免费", "Ollama")
PAID_MARKS = ("付费", "订阅", "按量", "API 计费", "需绑卡", "商用授权", "订阅制")
# 写了具体金额就算交代清楚，放行。匹配「月 5 元」「每条 0.02 元」「¥10/月」
COST_RE = re.compile(r"(?:[¥￥]\s*\d+|\d+(?:\.\d+)?\s*元)(?:/|\s*每|\s*月|\s*年)?")

# 每个目录对应一种严格度
STRICT_DIRS = {"tech"}
LITE_DIRS = {"prd", "specs", "research"}

PERMISSIVE = {
    "MIT",
    "MIT-CMU",
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
    runtime: str
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
    """解析六列（旧）或七列（新）表格。

    只认**表头匹配**的表格：靠列数判断会把任何七列表（如「项|内容」）都吃进来。
    旧格式没有「运行环境」列，映射时置空而不是丢弃——
    否则旧文档会静默变成「零行」，看起来像通过。
    """
    audit = ReuseAudit()
    found, body = _section_lines(text)
    audit.found_section = found
    if not found:
        return audit

    current_kind: str | None = None
    seen_header = False
    in_fence = False
    for lineno, line in body:
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line.startswith("|"):
            # 空行或非表格行表示表格结束，允许后面出现新表
            if not line.strip():
                current_kind = None
                seen_header = False
            continue
        cells = _split_cells(line)
        if _is_separator(cells):
            continue

        # 只在每张表的**第一行**判断表头。若第一行就是标准表头，跳过它；
        # 否则视为数据行（有些文档省略表头，或表头被改写过）。
        if not seen_header:
            seen_header = True
            if tuple(cells) == COLUMNS:
                current_kind = "seven"
                continue
            if tuple(cells) == LEGACY_COLUMNS_6:
                current_kind = "six"
                continue
            # 没识别到标准表头，按列数推断，并把这一行当数据处理
            if len(cells) == len(COLUMNS):
                current_kind = "seven"
            elif len(cells) == len(LEGACY_COLUMNS_6):
                current_kind = "six"
            else:
                current_kind = "other"
                continue

        if current_kind == "other":
            continue

        if current_kind == "seven":
            if len(cells) != len(COLUMNS):
                continue
            runtime, reason = cells[5], cells[6]
        else:
            if len(cells) != len(LEGACY_COLUMNS_6):
                continue
            runtime, reason = "", cells[5]
        audit.rows.append(
            ReuseRow(
                component=cells[0],
                candidate=cells[1],
                stars=cells[2],
                license=cells[3],
                conclusion=cells[4],
                runtime=runtime,
                reason=reason,
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

    # 建表语句是设计已落地的信号，必须已被候选对比覆盖。
    # 放在最后：缺章节/无行的情形上面已 return，不重复报。
    out.extend(check_design_coverage(text, path))

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
    # 关键词覆盖通顺写法：「取其X」「借用X」「沿用X」「只采用X」「参考X的做法」
    REFER_VERBS = ("取", "借", "学", "模", "沿用", "采用", "参考")
    if row.conclusion == "参考" and not any(k in reason for k in REFER_VERBS):
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
        # 复合许可（如 "MIT AND BUSL-1.1"）拆开逐项识别
        parts = {x.strip() for x in re.split(r"\s+AND\s+|\s+OR\s+|\s*/\s*", lic)}
        has_source_available = bool(parts & SOURCE_AVAILABLE)
        has_copyleft = bool(parts & COPYLEFT)

        if not (parts <= (PERMISSIVE | COPYLEFT | SOURCE_AVAILABLE)):
            out.append(
                Violation(path, ln, "复用调研", f"许可「{lic}」不在已知白名单内")
            )
        elif has_copyleft or has_source_available:
            # 个人非商用场景：GPL/AGPL/BUSL 都不触发对外义务，不构成拦截。
            # 但仍要求写一句「为什么不影响」，避免有人照抄却真去开公司。
            restrict = (parts & COPYLEFT) | (parts & SOURCE_AVAILABLE)
            if not any(k in row.reason for k in ("自用", "不商用", "不分发", "不对外")):
                out.append(
                    Violation(
                        path,
                        ln,
                        "复用调研",
                        f"选用了 {'/'.join(sorted(restrict))}，理由里须说明"
                        "「个人自用不触发其义务」",
                    )
                )

    # ── 运行环境：本项目最常被否决的原因，必须写 ──
    if row.conclusion == "复用":
        rt = row.runtime.strip()
        if not rt or rt in LITE_PLACEHOLDER:
            out.append(
                Violation(
                    path,
                    ln,
                    "复用调研",
                    "复用项必须填运行环境（Termux/proot/Docker/静态二进制等），"
                    "否则无法判断能否在平板上跑",
                )
            )
        elif not any(m in rt for m in RUNTIME_MARKS):
            out.append(
                Violation(
                    path,
                    ln,
                    "复用调研",
                    f"运行环境「{rt}」看不出运行方式，须含 "
                    f"{'/'.join(RUNTIME_MARKS[:5])} 之一",
                )
            )

    # ── 付费依赖：个人使用不接受「说不清多少钱」的开销 ──
    # 规则本意不是禁止付费，是禁止「按量计费」这种事后才知道账单的模糊表述。
    # 写出具体金额或明确免费，就放行。
    if row.conclusion in {"复用", "参考"}:
        cell = f"{row.runtime} {row.reason}"
        mentions_paid = any(k in cell for k in PAID_MARKS)
        states_cost = any(k in cell for k in FREE_MARKS) or bool(COST_RE.search(cell))
        if mentions_paid and not states_cost:
            out.append(
                Violation(
                    path,
                    ln,
                    "复用调研",
                    "该方案含付费/订阅/按量计费，须写出具体金额（如「月 5 元」）"
                    "或明确免费，否则账单不可预期",
                )
            )

    return out


# 建表语句是「设计已落地」的最强信号：它一旦写进方案就是要照着建。
# 只认 ```sql 围栏内的，因为正文与行内代码里的同名文本是叙述而非设计。
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_]\w*)",
    re.IGNORECASE,
)


def extract_design_anchors(text: str) -> list[tuple[int, str]]:
    """取出正文里落地的设计锚点：[(行号, 表名), ...]。"""
    out: list[tuple[int, str]] = []
    in_sql = False
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            # 只有 ```sql / ```SQL 开围栏才算；其它语言块跳过
            if not in_sql and stripped.lower().startswith("```sql"):
                in_sql = True
            elif in_sql:
                in_sql = False
            continue
        if not in_sql:
            continue
        for m in _CREATE_TABLE_RE.finditer(line):
            out.append((i, m.group(1)))
    return out


def check_design_coverage(text: str, path: str) -> list[Violation]:
    """建表语句必须有对应的复用调研行，否则说明该设计没做过候选对比。

    只对 strict 目录（docs/tech）生效：research/prd 里出现的 DDL 多是引用
    别人的 schema，不是本项目的设计决策。豁免文档整篇跳过。
    """
    if EXEMPT_MARK in text:
        return []
    if mode_for(path) != "strict":
        return []

    anchors = extract_design_anchors(text)
    if not anchors:
        return []

    audit = parse_audit(text)
    if not audit.found_section or not audit.rows:
        # 缺章节/无数据行由 audit_text 单独报，这里不重复
        return []

    corpus = "\n".join(f"{r.component} {r.reason}" for r in audit.rows)
    out: list[Violation] = []
    for lineno, name in anchors:
        if name in corpus:
            continue
        out.append(
            Violation(
                path,
                lineno,
                "设计覆盖",
                f"建了表「{name}」但复用调研表没有任何一行提到它："
                "该表的结构设计缺少候选对比（AGENTS.md 8.1）",
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


# 顺序很重要：本机实际使用的是 GITHUB_PERSONAL_ACCESS_TOKEN（实测 5000/小时），
# 早先只认 GITHUB_TOKEN，导致一直走匿名 60/小时 的通道并被打满。
TOKEN_ENVS = (
    "GITHUB_PERSONAL_ACCESS_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)


def github_token() -> str | None:
    for name in TOKEN_ENVS:
        v = os.environ.get(name)
        if v:
            return v
    return None


# 限流与网络故障不等于「文档写错」，要能区分开，否则 CI 会被误报刷屏。
TRANSIENT_ERRORS = ("HTTP 403", "HTTP 429", "URLError", "TimeoutError", "HTTP 5")


def fetch_repo(candidate: str, timeout: int = 15) -> tuple[int | None, str, str | None]:
    """返回 (星数, 许可, 错误)。"""
    url = f"https://api.github.com/repos/{candidate}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "kb-doc-checks",
    }
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
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


def is_transient(err: str | None) -> bool:
    """限流、超时、5xx 属于环境问题，不该判文档有错。"""
    if not err:
        return False
    return any(tag in err for tag in TRANSIENT_ERRORS)


def verify_remote(text: str, path: str, mode: str = "strict") -> list[Violation]:
    """联网核对候选的星数与许可，只报「声称多于实际」与许可不符。

    环境故障（限流/超时/5xx）抛 RemoteUnavailable，由调用方决定跳过，
    不要混进 Violation —— 否则 CI 每天都会因为打满配额而红。
    """
    if mode != "strict":
        return []
    audit = parse_audit(text)
    out: list[Violation] = []
    seen: dict[str, tuple[int | None, str]] = {}
    transient_hit = False
    checked = 0

    for row in audit.rows:
        if row.conclusion == "自写" or not REPO_RE.match(row.candidate):
            continue
        if row.candidate in seen:
            actual_stars, actual_lic = seen[row.candidate]
        else:
            actual_stars, actual_lic, err = fetch_repo(row.candidate)
            seen[row.candidate] = (actual_stars, actual_lic)
            if is_transient(err):
                transient_hit = True
        if actual_stars is None:
            continue
        checked += 1

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

    if checked == 0 and transient_hit:
        hint = (
            "已检测到 token，可能是 token 失效或权限不足"
            if github_token()
            else f"未检测到 token，设置 {' / '.join(TOKEN_ENVS)} 之一可把配额从 60/小时提到 5000/小时"
        )
        raise RemoteUnavailable(f"GitHub API 不可用或已限流，无法核对；{hint}")
    return out


class RemoteUnavailable(RuntimeError):
    """联网核对因环境原因无法进行，与文档是否正确无关。"""

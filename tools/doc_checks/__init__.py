"""文档规则检查。

这些规则原本写在 AGENTS.md 里，靠人记、靠 agent 自觉。能机器查的就不该靠自觉，
所以统一挪到这里：① 提交前钩子、② PR、③ main 三处跑同一套代码。

检查只覆盖"确定对错"的东西（禁用词、命名、结构完整性、跨文档编号覆盖）。
"事实有没有来源""数值口径对不对"这类需要判断的，留给人工。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC_ROOT = ROOT / "docs"
DOC_DIRS = ("prd", "specs", "research", "tech")

BANNED_WORDS = ("应该", "大概", "理论上", "通常", "后续考虑", "酌情")
# 这些词出现在"禁令本身"的句子里是合法的，例如"禁用「应该」"
BANNED_ESCAPE = ("禁用", "禁止", "不许", "不用「")

PLACEHOLDERS = ("<!-- PART", "TODO(生成)", "待补")

FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-.+\.md$")
REQ_ID_RE = re.compile(r"^M\d{1,2}-\d{1,2}$")
DOC_REF_RE = re.compile(r"`(docs/[^`\s]+\.md)`")


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}"


def _rel(p: Path) -> str:
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def docs_files() -> list[Path]:
    out: list[Path] = []
    for d in DOC_DIRS:
        base = DOC_ROOT / d
        if base.is_dir():
            out.extend(sorted(base.glob("*.md")))
    return out


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def check_banned_words(p: Path, text: str) -> list[Violation]:
    """禁用词只查 docs/tech。

    其他文档里这些词多为引用用户原话（「大概是蓝色」）或调研记录中的判断，
    在那里做关键词拦截会大量误报，最后的结果是整个检查被关掉。
    """
    if p.parent.name != "tech":
        return []
    out: list[Violation] = []
    for i, line in enumerate(text.splitlines(), start=1):
        if any(esc in line for esc in BANNED_ESCAPE):
            continue
        for w in BANNED_WORDS:
            if w in line:
                out.append(
                    Violation(_rel(p), i, "禁用词", f"出现「{w}」，改成具体判断")
                )
    return out


def check_filename(p: Path) -> list[Violation]:
    if FILENAME_RE.match(p.name):
        return []
    return [
        Violation(
            _rel(p), 1, "命名", "文件名须为 YYYY-MM-DD-主题.md（见 AGENTS.md 第四节）"
        )
    ]


def check_code_fences(p: Path, text: str) -> list[Violation]:
    fences = [
        i for i, line in enumerate(text.splitlines(), start=1) if line.startswith("```")
    ]
    if len(fences) % 2 == 0:
        return []
    return [Violation(_rel(p), fences[-1], "代码块", "``` 未闭合（数量为奇数）")]


def check_table_rows(p: Path, text: str) -> list[Violation]:
    out: list[Violation] = []
    in_fence = False
    for i, line in enumerate(text.splitlines(), start=1):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line.startswith("|"):
            continue
        if not line.rstrip().endswith("|"):
            out.append(Violation(_rel(p), i, "表格", "表格行未以 | 结尾"))
    return out


def check_empty_sections(p: Path, text: str) -> list[Violation]:
    """标题下面只有子标题是正常结构；只有"同级或更高级标题紧接其后"才算空章节。"""
    out: list[Violation] = []
    lines = text.splitlines()
    in_fence = False
    for i, line in enumerate(lines):
        if line.startswith("```"):
            in_fence = not in_fence
        if in_fence or not line.startswith("#"):
            continue
        level = len(line) - len(line.lstrip("#"))
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines) or not lines[j].startswith("#"):
            continue
        next_level = len(lines[j]) - len(lines[j].lstrip("#"))
        if next_level <= level:
            out.append(
                Violation(_rel(p), i + 1, "空章节", "标题下面没有内容，接了同级标题")
            )
    return out


def check_placeholders(p: Path, text: str) -> list[Violation]:
    out: list[Violation] = []
    for token in PLACEHOLDERS:
        for m in re.finditer(re.escape(token), text):
            out.append(
                Violation(
                    _rel(p), _line_of(text, m.start()), "占位符", f"残留「{token}」"
                )
            )
    return out


def _prd_priorities() -> dict[str, str]:
    """从 PRD 里读出每个需求编号的优先级。"""
    found: dict[str, str] = {}
    prd = DOC_ROOT / "prd"
    if not prd.is_dir():
        return found
    for f in prd.glob("*.md"):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 3 and REQ_ID_RE.match(cells[0]):
                if re.fullmatch(r"P[0-3]", cells[2]):
                    found[cells[0]] = cells[2]
    return found


def check_p0_coverage() -> list[Violation]:
    """AGENTS.md 第七节第 1 条：PRD 每条 P0 必须在方案里出现。"""
    prio = _prd_priorities()
    p0 = sorted(k for k, v in prio.items() if v == "P0")
    if not p0:
        return [Violation("docs/prd", 1, "P0 覆盖", "没从 PRD 里解析出任何 P0 需求")]
    tech = DOC_ROOT / "tech"
    if not tech.is_dir():
        return [Violation("docs/tech", 1, "P0 覆盖", "技术方案目录不存在")]
    blob = "\n".join(f.read_text(encoding="utf-8") for f in tech.glob("*.md"))
    missing = [x for x in p0 if x not in blob]
    return [
        Violation("docs/tech", 1, "P0 覆盖", f"PRD 的 {x} 在技术方案里没有落点")
        for x in missing
    ]


def check_referenced_paths(paths: list[Path]) -> list[Violation]:
    """文档里写成 `docs/xxx.md` 的引用必须真实存在（只在 ③ 跑，需要全库视角）。"""
    out: list[Violation] = []
    for p in paths:
        text = p.read_text(encoding="utf-8")
        for m in DOC_REF_RE.finditer(text):
            target = m.group(1)
            if not (ROOT / target).exists():
                out.append(
                    Violation(
                        _rel(p),
                        _line_of(text, m.start()),
                        "引用失效",
                        f"指向的 {target} 不存在",
                    )
                )
    return out


def check_file(p: Path) -> list[Violation]:
    text = p.read_text(encoding="utf-8")
    return [
        *check_filename(p),
        *check_banned_words(p, text),
        *check_code_fences(p, text),
        *check_table_rows(p, text),
        *check_empty_sections(p, text),
        *check_placeholders(p, text),
    ]


def run_checks(
    paths: list[Path] | None = None,
    *,
    cross_refs: bool = False,
) -> list[Violation]:
    targets = paths if paths is not None else docs_files()
    out: list[Violation] = []
    for p in targets:
        out.extend(check_file(p))
    if paths is None:
        out.extend(check_p0_coverage())
    if cross_refs:
        out.extend(check_referenced_paths(targets))
    return out


def _git_names(args: list[str]) -> list[str]:
    """取 git 给出的文件列表。

    必须用 -z：默认输出会把中文路径转成 "\\351\\222\\251" 这种八进制转义，
    再按路径找文件就全落空 —— 表现是"没有文档改动"，钩子永远是绿的。
    """
    try:
        raw = subprocess.run(
            ["git", *args, "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
    except subprocess.CalledProcessError as exc:  # pragma: no cover - 环境问题
        msg = exc.stderr.decode("utf-8", "replace").strip()
        raise SystemExit(f"git {' '.join(args)} 失败：{msg}") from exc
    return [n for n in raw.decode("utf-8", "replace").split("\0") if n]


def changed_files(base: str) -> list[Path]:
    """相对 base 分支改动过的文档。"""
    return _to_docs(
        _git_names(
            ["diff", "--name-only", "--diff-filter=ACM", f"{base}...HEAD"],
        )
    )


def staged_files() -> list[Path]:
    return _to_docs(
        _git_names(["diff", "--cached", "--name-only", "--diff-filter=ACM"])
    )


def _to_docs(names: list[str]) -> list[Path]:
    out: list[Path] = []
    for name in names:
        p = ROOT / name
        if p.suffix == ".md" and p.exists() and DOC_ROOT in p.parents:
            out.append(p)
    return out

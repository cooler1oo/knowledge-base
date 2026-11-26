"""提交纪律的机器检查。

三类规则，共同点都是「原本写在文档里靠自觉」：
1. 提交信息要有内容（主题格式 + 正文 + 必须写验证）
2. 单次提交不能是大杂烩（文件数/行数有上限，超了要显式放行）
3. 推送前工作区必须干净；交付前不许有未推送的提交

为什么第 3 条用 pre-push 而不是 commit 钩子：能机器判的只有「推之前有没有漏提交」，
「每次改动就提交」本身无法在提交那一刻判定（那是在判断未来）。
所以口径定为：**只要推送，就必须先把改动提交干净**。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

SUBJECT_RE = re.compile(
    r"^(feat|fix|docs|test|refactor|chore|ci|perf|build|revert)(\([\w.\-/]+\))?: \S.{2,}$"
)
SUBJECT_MAX = 72
BODY_MIN_LINES = 2

MAX_FILES = 25
MAX_LINES = 800
BYPASS_ENV = "KB_ALLOW_LARGE_COMMIT"


@dataclass(frozen=True)
class Problem:
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.detail}"


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout


def check_commit_message(message: str) -> list[Problem]:
    """检查一条完整提交信息（首行主题 + 空行 + 正文）。"""
    text = message.strip("\n")
    if not text.strip():
        return [Problem("提交信息为空", "至少要写做了什么")]
    lines = text.splitlines()
    subject = lines[0].strip()
    body = [line for line in lines[1:] if line.strip()]

    problems: list[Problem] = []
    if not SUBJECT_RE.match(subject):
        problems.append(
            Problem(
                "主题格式",
                f"「{subject}」不符合 type(scope): 描述；type 取 "
                "feat/fix/docs/test/refactor/chore/ci/perf/build/revert",
            )
        )
    if len(subject) > SUBJECT_MAX:
        problems.append(Problem("主题过长", f"{len(subject)} 字符，上限 {SUBJECT_MAX}"))
    if len(body) < BODY_MIN_LINES:
        problems.append(
            Problem("缺正文", f"正文至少 {BODY_MIN_LINES} 行：做了什么、为什么")
        )
    if "验证" not in text:
        problems.append(
            Problem(
                "缺验证", "正文里必须写「验证：」并附命令或输出，没有验证等于没做完"
            )
        )
    return problems


def staged_stats() -> tuple[int, int]:
    """返回 (暂存文件数, 增删行数合计)。"""
    out = _git("diff", "--cached", "--numstat")
    files = 0
    lines = 0
    for row in out.splitlines():
        parts = row.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        added, deleted = parts[0], parts[1]
        lines += int(added) if added.isdigit() else 0
        lines += int(deleted) if deleted.isdigit() else 0
    return files, lines


def check_commit_size(files: int, lines: int) -> list[Problem]:
    """一次提交塞太多东西，说明把多次改动攒成了一坨。"""
    if os.environ.get(BYPASS_ENV) == "1":
        return []
    problems: list[Problem] = []
    if files > MAX_FILES:
        problems.append(
            Problem(
                "提交过大",
                f"{files} 个文件，上限 {MAX_FILES}（确需批量：设 {BYPASS_ENV}=1）",
            )
        )
    if lines > MAX_LINES:
        problems.append(
            Problem(
                "提交过大",
                f"{lines} 行改动，上限 {MAX_LINES}（确需批量：设 {BYPASS_ENV}=1）",
            )
        )
    return problems


def check_worktree_clean() -> list[Problem]:
    """推送前必须干净：有未提交改动就推，等于把改动留在本地没备份。"""
    dirty = [ln for ln in _git("status", "--porcelain").splitlines() if ln.strip()]
    if not dirty:
        return []
    preview = "；".join(dirty[:5])
    more = f" 等 {len(dirty)} 项" if len(dirty) > 5 else ""
    return [Problem("工作区不干净", f"先提交再推送：{preview}{more}")]


def check_pushed() -> list[Problem]:
    """交付前不许有未推送的提交。没有上游就直说。"""
    try:
        ahead = int(_git("rev-list", "--count", "@{u}..HEAD").strip() or 0)
    except RuntimeError:
        return [
            Problem("未设置上游", "main 还没跟踪 origin/main：git push -u origin main")
        ]
    if ahead:
        return [Problem("有未推送提交", f"{ahead} 个提交只在本地，git push")]
    return []

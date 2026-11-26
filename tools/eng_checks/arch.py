"""架构边界检查（技术方案 §7.5 的三条，逐条可执行）。

为什么要它：规则写在文档里没人执行，「api 层直接写库」这类腐化是慢慢发生的，
等发现时已经散落各处。AST 检查能在提交那一刻拦住。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

# 只认真正的写库语句形态，避免把错误提示里提到 DELETE 误判
WRITE_SQL_RE = re.compile(
    r"\b(insert\s+into|update\s+[a-z_]+\s+set|delete\s+from|create\s+table|drop\s+table)\b",
    re.IGNORECASE,
)

STORE_PREFIX = "kb/store/"
LAYER_PREFIXES = (("kb/extract/", "抓取层"), ("kb/ai/", "AI 层"))


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} [{self.rule}] {self.detail}"


def _imports(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
    return found


def _strings(tree: ast.AST) -> list[tuple[int, str]]:
    return [
        (n.lineno, n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def check_file(rel_path: str, source: str) -> list[Violation]:
    """检查单个文件。rel_path 用 / 分隔、相对仓库根。"""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Violation(rel_path, exc.lineno or 0, "语法", f"无法解析：{exc.msg}")]

    out: list[Violation] = []

    if not rel_path.startswith(STORE_PREFIX):
        for lineno, text in _strings(tree):
            if WRITE_SQL_RE.search(text):
                out.append(
                    Violation(
                        rel_path,
                        lineno,
                        "只有 store 能写库",
                        "出现写库 SQL；写库只能经 kb/store 的函数",
                    )
                )

    for prefix, layer in LAYER_PREFIXES:
        if not rel_path.startswith(prefix):
            continue
        for lineno, module in _imports(tree):
            if module == "kb.store" or module.startswith("kb.store."):
                out.append(
                    Violation(
                        rel_path,
                        lineno,
                        f"{layer}不依赖 store",
                        f"import 了 {module}；该层只处理文本，不碰数据库",
                    )
                )
    return out


def scan(root: Path) -> list[Violation]:
    """扫 root/kb 下全部 .py。没有 kb 目录时返回空：规则暂时没有对象，不算失败。"""
    kb_dir = root / "kb"
    if not kb_dir.is_dir():
        return []
    out: list[Violation] = []
    for path in sorted(kb_dir.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        out.extend(check_file(rel, path.read_text(encoding="utf-8")))
    return out

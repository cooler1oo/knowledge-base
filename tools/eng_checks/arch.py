"""架构边界检查：机制通用，规则声明式。

规则不再硬编码在代码里，而是 `layer_rules.toml` 的几行声明——换项目只改配置，
执行器不动。这也是它适合从本项目抽成通用工具的原因。

为什么要它：规则写在文档里没人执行，「api 层直接写库」这类腐化是慢慢发生的，
等发现时已经散落各处。AST 检查能在提交那一刻拦住。
"""

from __future__ import annotations

import ast
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

# 只认真正的写库语句形态，避免把错误提示里提到 DELETE 误判
WRITE_SQL_RE = re.compile(
    r"\b(insert\s+into|update\s+[a-z_]+\s+set|delete\s+from|create\s+table|drop\s+table)\b",
    re.IGNORECASE,
)

RULES_FILE = "layer_rules.toml"

# 配置文件缺失时的兜底：保持本项目当前的分层口径
DEFAULT_RULES: dict[str, object] = {
    "dirs": ["kb"],
    "write_sql_allowed_prefixes": ["kb/store/"],
    "import_deny": [
        {"path_prefix": "kb/extract/", "layer": "抓取层", "modules": ["kb.store"]},
        {"path_prefix": "kb/ai/", "layer": "AI 层", "modules": ["kb.store"]},
    ],
}


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} [{self.rule}] {self.detail}"


class RuleConfigError(RuntimeError):
    """规则文件存在但不可用。

    不能静默退回默认口径：那样规则没生效却照样显示「通过」，比没有检查更危险。
    """


def _require_str_list(data: dict[str, object], key: str, where: str) -> None:
    value = data.get(key)
    if value is None:
        return
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise RuleConfigError(f"{where}: {key} 必须是字符串数组")


def load_rules(path: Path) -> dict[str, object]:
    """读声明式规则。

    - 文件不存在 → 用默认口径（项目可以不配）
    - 文件存在但读不出/写错 → **报错**，不退回默认
    """
    if not path.is_file():
        return DEFAULT_RULES
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuleConfigError(f"{path.name} 读不出来：{exc}") from exc

    where = path.name
    _require_str_list(data, "dirs", where)
    _require_str_list(data, "write_sql_allowed_prefixes", where)
    deny = data.get("import_deny", [])
    if not isinstance(deny, list):
        raise RuleConfigError(f"{where}: import_deny 必须是数组")
    for idx, rule in enumerate(deny, 1):
        if not isinstance(rule, dict):
            raise RuleConfigError(f"{where}: import_deny 第 {idx} 项必须是表")
        if not isinstance(rule.get("path_prefix"), str):
            raise RuleConfigError(f"{where}: import_deny 第 {idx} 项缺 path_prefix")
        modules = rule.get("modules")
        if (
            not isinstance(modules, list)
            or not modules
            or not all(isinstance(m, str) for m in modules)
        ):
            raise RuleConfigError(
                f"{where}: import_deny 第 {idx} 项的 modules 必须是非空字符串数组"
            )

    merged = dict(DEFAULT_RULES)
    merged.update({k: v for k, v in data.items() if v})
    return merged


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


def _matches(rel_path: str, prefixes: object) -> bool:
    return isinstance(prefixes, list) and any(
        rel_path.startswith(str(p)) for p in prefixes
    )


def check_file(
    rel_path: str, source: str, rules: dict[str, object] | None = None
) -> list[Violation]:
    """检查单个文件。rel_path 用 / 分隔、相对仓库根。"""
    active = rules or DEFAULT_RULES
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Violation(rel_path, exc.lineno or 0, "语法", f"无法解析：{exc.msg}")]

    out: list[Violation] = []

    allowed = active.get("write_sql_allowed_prefixes", [])
    if not _matches(rel_path, allowed):
        for lineno, text in _strings(tree):
            if WRITE_SQL_RE.search(text):
                out.append(
                    Violation(
                        rel_path,
                        lineno,
                        "只有 store 能写库",
                        "出现写库 SQL；写库只能经 store 层的函数",
                    )
                )

    deny = active.get("import_deny", [])
    if isinstance(deny, list):
        for rule in deny:
            if not isinstance(rule, dict):
                continue
            prefix = str(rule.get("path_prefix", ""))
            if not prefix or not rel_path.startswith(prefix):
                continue
            layer = str(rule.get("layer", "该层"))
            modules = [str(m) for m in rule.get("modules", [])]
            for lineno, module in _imports(tree):
                if any(module == m or module.startswith(m + ".") for m in modules):
                    out.append(
                        Violation(
                            rel_path,
                            lineno,
                            f"{layer}不依赖 store",
                            f"import 了 {module}；该层只处理文本，不碰数据库",
                        )
                    )
    drivers = active.get("driver_deny", [])
    if isinstance(drivers, list):
        for rule in drivers:
            if not isinstance(rule, dict):
                continue
            if _matches(rel_path, rule.get("allowed_prefixes")):
                continue
            modules = [str(m) for m in rule.get("modules", [])]
            for lineno, module in _imports(tree):
                if any(module == m or module.startswith(m + ".") for m in modules):
                    out.append(
                        Violation(
                            rel_path,
                            lineno,
                            "不得直连数据库驱动",
                            f"import 了 {module}；数据库访问只能经 store 层",
                        )
                    )
    return out


def scan(root: Path, rules: dict[str, object] | None = None) -> list[Violation]:
    """按配置扫指定目录。目录不存在时跳过：规则暂时没有对象，不算失败。"""
    if rules is None:
        try:
            active = load_rules(root / RULES_FILE)
        except RuleConfigError as exc:
            # 把配置错误当成一条违规报出去，退出码非 0——不许静默降级
            return [Violation(RULES_FILE, 0, "规则文件", str(exc))]
    else:
        active = rules
    dirs = active.get("dirs", ["kb"])
    dirs = [str(d) for d in dirs] if isinstance(dirs, list) else ["kb"]

    out: list[Violation] = []
    for name in dirs:
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            out.extend(check_file(rel, path.read_text(encoding="utf-8"), active))
    return out

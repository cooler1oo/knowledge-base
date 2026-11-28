"""架构约束的 AST 断言：`kb/vision/` 的顶层 import 不许出现外部视频库。

为什么用 AST 而不用 grep：`import cv2` 出现在**方法体内**是合法的（设计如此），
出现在模块顶层就要拦——而 grep 分不清两者。AST 能精确判断 `Import` /
`ImportFrom` 节点挂在哪一层。

两条规则，都对应一个具体的坏结果：

1. **顶层不 import `cv2` / `scenedetect` / `numpy`**：顶层 import 会让
   `tests/test_vision_plan.py`（纯逻辑，不装这些库也该能跑）在收集阶段就失败。
   本机与 CI 装的东西并不一致，这个风险是真会发生的。
2. **没有文件 import `kb.store`**：视觉层出的是帧与时间区间，落库由调用方经
   store 做。这条同时也由 `layer_rules.toml` + `tools.eng_checks --arch` 强制，
   在这里再断言一次是因为架构检查是**可选命令**，而 pytest 是必跑的。
"""

from __future__ import annotations

import ast
from pathlib import Path

VISION_DIR = Path(__file__).resolve().parents[1] / "kb" / "vision"

# 只允许在方法体内 import 的外部库。都是重依赖或需要编译产物的库。
LAZY_ONLY_MODULES = ("cv2", "scenedetect", "numpy")

# 视觉层不得依赖的层。
FORBIDDEN_MODULES = ("kb.store",)


def vision_files() -> list[Path]:
    files = sorted(VISION_DIR.glob("*.py"))
    assert files, f"没在 {VISION_DIR} 找到任何 .py 文件，检查路径对不对"
    return files


def top_level_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """模块顶层的 import，**不含**函数与类体内的。

    只看 `tree.body` 里的节点，不 `ast.walk`——walk 会钻进函数体，那样
    「方法内 import cv2」这个正确写法也会被判违规。
    """
    found: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
    return found


def all_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """全部 import，含函数体内的。用来查「任何层的依赖」。"""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
    return found


def matches(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == p or module.startswith(p + ".") for p in prefixes)


# ── 规则一：顶层不 import 外部视频库 ────────────────────────


def test_视觉层文件都被扫到() -> None:
    """防止「一个文件都没扫到」这种静默通过——那年 docs 检查踩过同一个坑。"""
    names = {p.name for p in vision_files()}

    assert names >= {
        "__init__.py",
        "models.py",
        "shot_plan.py",
        "cut.py",
        "grab.py",
        "frames.py",
    }, f"缺文件：{names}"


def test_顶层不import外部视频库() -> None:
    """顶层 import 会让纯逻辑单测在没装这些库的环境里收集失败。"""
    offenders: list[str] = []
    for path in vision_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, module in top_level_imports(tree):
            if matches(module, LAZY_ONLY_MODULES):
                offenders.append(f"{path.name}:{lineno} import {module}")

    assert not offenders, "顶层不许出现外部视频库（放方法体内）：" + "；".join(
        offenders
    )


def test_切镜头与抽帧确实在方法体内import() -> None:
    """反向断言：合法的延迟 import 必须真的存在。

    少了这条，把 `detect` 写成空实现也能让上一条通过——「没有违规」和
    「没有代码」在上一条里长得一样。
    """
    for name, module in (("cut.py", "scenedetect"), ("grab.py", "cv2")):
        tree = ast.parse((VISION_DIR / name).read_text(encoding="utf-8"), filename=name)
        inside = [line for line, mod in all_imports(tree) if mod == module]
        assert inside, f"{name} 里找不到 import {module}，延迟 import 写了吗？"
        top = [line for line, mod in top_level_imports(tree) if mod == module]
        assert not top, f"{name} 的 {module} 出现在顶层（第 {top} 行）"


# ── 规则二：不依赖 store 层 ────────────────────────────────


def test_没有文件import_store层() -> None:
    """视觉层出帧与时间区间，落库由调用方做。这条同时由 layer_rules.toml 强制，
    在这里再断一次是因为 eng_checks 是可选命令而 pytest 必跑。"""
    offenders: list[str] = []
    for path in vision_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, module in all_imports(tree):
            if matches(module, FORBIDDEN_MODULES):
                offenders.append(f"{path.name}:{lineno} import {module}")

    assert not offenders, "视觉层不许 import store：" + "；".join(offenders)


def test_没有文件出现写库SQL() -> None:
    """放宽到「字符串里出现写库关键字」：错误提示里提到 DELETE 不算，
    但真写了 SQL 会命中。与 eng_checks 的正则口径一致。"""
    import re

    pattern = re.compile(
        r"\b(insert\s+into|update\s+[a-z_]+\s+set|delete\s+from|create\s+table|"
        r"drop\s+table)\b",
        re.IGNORECASE,
    )
    offenders: list[str] = []
    for path in vision_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if pattern.search(node.value):
                    offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, "视觉层不许出现写库 SQL：" + "；".join(offenders)


def test_没有文件import数据库驱动() -> None:
    """与 layer_rules.toml 的 driver_deny 同口径。"""
    offenders: list[str] = []
    for path in vision_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, module in all_imports(tree):
            if matches(module, ("pg8000", "psycopg", "psycopg2", "sqlite3")):
                offenders.append(f"{path.name}:{lineno} import {module}")

    assert not offenders, "视觉层不许直连数据库驱动：" + "；".join(offenders)


# ── 规则三：不许 print ─────────────────────────────────────


def test_没有文件调用print() -> None:
    """产品代码用 logging，print 只会进 stdout 没人收。ruff 的 T20 也会拦，
    但那条规则依赖 ruff 被跑过；这里是必跑的。"""
    offenders: list[str] = []
    for path in vision_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, "kb/ 下不许 print（用 log）：" + "；".join(offenders)


# ── 规则四：能真的 import ──────────────────────────────────


def test_导入顶层模块不依赖外部库() -> None:
    """端到端的同一件事：只 import 本层，不去碰 cv2 / scenedetect。

    用一个干净子进程跑，避免当前进程已经 import 过这些库造成假通过。
    """
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import kb.vision.frames\n"
        "banned = [m for m in ('cv2', 'scenedetect') if m in sys.modules]\n"
        "assert not banned, banned\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=VISION_DIR.parents[1],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout

"""媒体层的架构约束：模块导入必须是纯的，不许依赖环境、不许 import store。

**为什么单独一个文件**：这些断言守的是「一个不该出现的东西存在」，不是功能。
放在 `tests/test_media_layout.py` 里会被看成「layout 的用例」，而它们约束的是
**整个 `kb/media/` 包**——包括将来新增的文件。

## 三条约束，每条都对应一次真实故障

1. **模块级不许有环境依赖**（`Path.home()` / `os.environ` 取值 / 建目录）。
   `kb/media/config.py` 曾在模块级写 `DEFAULT_MEDIA_ROOT = Path.home() / "media"`，
   结果 `python -m kb.worker` 在 **import 阶段**就抛
   `RuntimeError: Could not determine home directory.`，抢在
   `ensure_ai_available()` 前面——B27 那条用例要验的「缺 httpx 时给友好提示」
   永远执行不到。真机与 HEAD 对比验证过（HEAD 绿、工作区红）。

2. **顶层不 import `httpx` / `PIL`**。这两个库放在方法体内，纯逻辑单测才能
   在没装它们的环境跑。

3. **不 import `kb.store`**。落库由 worker 经 `kb.store.attachments` 做。
   `layer_rules.toml` 也登记了这条，但 `eng_checks` 是可选命令，pytest 必跑。

## 这一组都**不加** `pg` 标记

它们不需要数据库。加了标记会在本机无库时 skip，防线就等于不存在
（`tests/test_worker_loop.py` 的 F2 那条记着同一个坑）。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

MEDIA_DIR = Path(__file__).resolve().parents[1] / "kb" / "media"
ROOT = MEDIA_DIR.parents[1]

#: 只允许在方法体内 import 的外部库。
LAZY_ONLY = ("httpx", "PIL")

#: 媒体层不得依赖的层。
FORBIDDEN = ("kb.store",)

#: 数据库驱动。任何层都不许直接摸（`layer_rules.toml` 的 driver_deny 同口径）。
DRIVERS = ("pg8000", "psycopg", "psycopg2", "sqlite3")


def media_files() -> list[Path]:
    files = sorted(MEDIA_DIR.glob("*.py"))
    assert files, f"没在 {MEDIA_DIR} 找到任何 .py 文件，检查路径对不对"
    return files


def top_level_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """模块顶层的 import，**不含**函数与类体内的。

    只看 `tree.body`，不 `ast.walk`——walk 会钻进函数体，那样「方法内 import」
    这个正确写法也会被判违规。
    """
    found: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
        elif isinstance(node, ast.If):
            # `if TYPE_CHECKING:` 块里的 import 不算运行时依赖，但它也是
            # 顶层节点——真要出现外部库，`from __future__ import annotations`
            # 会让它只在类型检查时生效，运行时仍然安全。这里仍然拦，
            # 因为「类型检查时才 import」与「方法体内 import」对单测的影响
            # 虽然不同，但两者都没有必要引入外部库的顶层引用。
            for child in node.body:
                if isinstance(child, ast.Import):
                    found += [(child.lineno, alias.name) for alias in child.names]
                elif (
                    isinstance(child, ast.ImportFrom)
                    and child.module
                    and child.level == 0
                ):
                    found.append((child.lineno, child.module))
    return found


def all_imports(tree: ast.Module) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
    return found


def matches(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def module_level_nodes(tree: ast.Module) -> list[ast.AST]:
    """模块级语句里**除了函数与类定义体**的所有节点。

    ## 为什么不能用 `ast.walk(node)` 直接走 `tree.body`

    `FunctionDef` 本身是 `tree.body` 的成员，而 `ast.walk(FunctionDef)` 会
    钻进它的 `body`——于是「函数内调 `Path.home()`」这个**正确**写法会被
    判违规。第一版就是这么错的：`persist.py` 的 `_ensure_root` 里几个
    `mkdir` 全被报了出来，而它们都在函数里。

    正确的做法是：只遍历模块级语句，遇到 `FunctionDef` / `ClassDef` /
    `AsyncFunctionDef` 就停（连它的装饰器与默认参数都不查——那些在 import
    时求值，但本项目的模块级函数定义没有这种写法，查了只会产生误报）。
    """
    out: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        out.append(node)
    return out


def _walk_module_level(tree: ast.Module) -> list[ast.AST]:
    """模块级语句的全部后代节点（`ast.walk` 每个模块级语句）。"""
    found: list[ast.AST] = []
    for node in module_level_nodes(tree):
        found.extend(ast.walk(node))
    return found


# ── 文件清单（防止「一个文件都没扫到」的静默通过）──────────────


def test_媒体层文件都被扫到() -> None:
    """那年 docs 检查踩过「一个文件都没扫到就报通过」的坑。"""
    names = {p.name for p in media_files()}
    assert names >= {
        "__init__.py",
        "config.py",
        "models.py",
        "layout.py",
        "sniff.py",
        "plan.py",
        "guard.py",
        "fetch.py",
        "encode.py",
        "persist.py",
    }, f"缺文件：{names}"


# ── 约束一：模块级无环境依赖 ──────────────────────────────────


def test_模块级不调用Path_home() -> None:
    """`Path.home()` 在 HOME 未设时抛 `RuntimeError`，模块级抛会让导入方崩。

    这条是 `kb/media/config.py` 那次回归的直接守门用例。
    """
    offenders: list[str] = []
    for path in media_files():
        for child in _walk_module_level(_parse(path)):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "home"
                and isinstance(func.value, ast.Name)
                and func.value.id == "Path"
            ):
                offenders.append(f"{path.name}:{child.lineno} Path.home()")
    assert not offenders, (
        "模块级不许调 Path.home()（HOME 未设时抛 RuntimeError，"
        "会让 import 阶段就崩）：" + "；".join(offenders)
    )


def test_模块级不读环境变量() -> None:
    """`os.environ[...]` / `os.getenv(...)` 在模块级求值同样让导入依赖环境。

    `media_root_from_env()` 里的读取是合法的——它在函数内。
    """
    offenders: list[str] = []
    for path in media_files():
        for child in _walk_module_level(_parse(path)):
            if isinstance(child, ast.Subscript):
                value = child.value
                if (
                    isinstance(value, ast.Attribute)
                    and value.attr == "environ"
                    and isinstance(value.value, ast.Name)
                    and value.value.id == "os"
                ):
                    offenders.append(f"{path.name}:{child.lineno} os.environ[...]")
            elif isinstance(child, ast.Call):
                func = child.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in {"getenv", "get"}
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "environ"
                ):
                    offenders.append(
                        f"{path.name}:{child.lineno} os.environ.{func.attr}()"
                    )
    assert not offenders, "模块级不许读环境变量：" + "；".join(offenders)


def test_模块级不建目录或写文件() -> None:
    """`mkdir` / `open(...,'w')` 在模块级执行会让 `import kb.media` 有副作用。

    后果比崩更隐蔽：单测 import 一下就在仓库里建了目录，而 CI 上权限不同
    时行为不同。
    """
    offenders: list[str] = []
    for path in media_files():
        for child in _walk_module_level(_parse(path)):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            if isinstance(func, ast.Attribute) and func.attr in {
                "mkdir",
                "makedirs",
                "rmdir",
                "unlink",
                "remove",
                "rmtree",
            }:
                offenders.append(f"{path.name}:{child.lineno} .{func.attr}()")
    assert not offenders, "模块级不许做文件系统操作：" + "；".join(offenders)


def test_这些模块级断言真的能抓到违规() -> None:
    """**反向验证：这三条断言不是恒真。**

    少了这条，把检测逻辑写成空循环也能让上面三条全绿——「没有违规」与
    「没有检测」在断言眼里长得一样。这与我写 `test_httpx与PIL确实在方法体内
    import` 的理由相同。

    用 `ast.parse` 造一段**故意违规**的源码，逐条跑检测函数。
    """
    bad_home = ast.parse('from pathlib import Path\nX = Path.home() / "media"\n')
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "home"
        for node in _walk_module_level(bad_home)
    ), "Path.home() 检测失效——上面的用例守不住任何东西"

    bad_env = ast.parse('import os\nX = os.environ["HOME"]\n')
    assert any(
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "environ"
        for node in _walk_module_level(bad_env)
    ), "os.environ 检测失效"

    bad_mkdir = ast.parse('from pathlib import Path\nPath("x").mkdir()\n')
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "mkdir"
        for node in _walk_module_level(bad_mkdir)
    ), "mkdir 检测失效"

    # 反向：函数体内的同类调用**不该**被报（这是第一版误报的根因）
    good = ast.parse(
        "from pathlib import Path\n"
        "def f() -> None:\n"
        "    Path.home()\n"
        "    Path('x').mkdir()\n"
    )
    inner = _walk_module_level(good)
    assert not [n for n in inner if isinstance(n, ast.Call)], (
        "函数体内的调用被当成模块级违规了——`module_level_nodes` 没跳过函数体"
    )


def test_子进程在无HOME环境里能导完整个媒体层() -> None:
    """端到端：清掉 HOME/USERPROFILE 后 import 每一个媒体模块。

    AST 断言只看得到「有没有写 `Path.home()`」，看不到 `platform` /
    `tempfile` / 第三方库在 import 期读环境这种间接依赖。子进程这条能兜住。
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")
    }
    env["PYTHONIOENCODING"] = "utf-8"
    modules = ", ".join(
        f"kb.media.{path.stem}" for path in media_files() if path.stem != "__init__"
    )
    code = f"import {modules}; print('全部模块导入 OK')"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert "全部模块导入 OK" in result.stdout, (
        f"媒体层模块级依赖了环境：{(result.stderr or '')[-500:]}"
    )


# ── 约束二：顶层不 import 外部库 ────────────────────────────


def test_顶层不import_httpx与PIL() -> None:
    """顶层 import 会让纯逻辑单测在没装这些库的环境收集阶段就失败。"""
    offenders: list[str] = []
    for path in media_files():
        for lineno, module in top_level_imports(_parse(path)):
            if matches(module, LAZY_ONLY):
                offenders.append(f"{path.name}:{lineno} import {module}")
    assert not offenders, "顶层不许出现 httpx / PIL（放方法体内）：" + "；".join(
        offenders
    )


def test_httpx与PIL确实在方法体内import() -> None:
    """反向断言：合法的延迟 import 必须真的存在。

    少了这条，把 `HttpxDownloader.get` 写成空实现也能让上一条通过
    ——「没有违规」与「没有代码」在上一条里长得一样。
    """
    for name, module in (("fetch.py", "httpx"), ("encode.py", "PIL")):
        tree = _parse(MEDIA_DIR / name)
        inside = [line for line, mod in all_imports(tree) if mod == module]
        assert inside, f"{name} 里找不到 import {module}，延迟 import 写了吗？"
        top = [line for line, mod in top_level_imports(tree) if mod == module]
        assert not top, f"{name} 的 {module} 出现在顶层（第 {top} 行）"


# ── 约束三：不依赖 store / 不摸驱动 ────────────────────────


def test_没有文件import_store层() -> None:
    """落库由 worker 经 `kb.store.attachments` 做。

    这条同时由 `layer_rules.toml` + `eng_checks --arch` 强制，
    在这里再断言一次是因为 `eng_checks` 是**可选命令**，而 pytest 必跑。
    """
    offenders: list[str] = []
    for path in media_files():
        for lineno, module in all_imports(_parse(path)):
            if matches(module, FORBIDDEN):
                offenders.append(f"{path.name}:{lineno} import {module}")
    assert not offenders, "媒体层不许 import store：" + "；".join(offenders)


def test_没有文件import数据库驱动() -> None:
    offenders: list[str] = []
    for path in media_files():
        for lineno, module in all_imports(_parse(path)):
            if matches(module, DRIVERS):
                offenders.append(f"{path.name}:{lineno} import {module}")
    assert not offenders, "媒体层不许直连数据库驱动：" + "；".join(offenders)


def test_没有文件出现写库SQL() -> None:
    """放宽到「字符串里出现写库关键字」：错误提示里提到 DELETE 不算，
    但真写了 SQL 会命中。与 `eng_checks` 的正则口径一致。

    注意本文件自己含 `delete from attachments` 字样（在 `attachments.py` 里），
    所以只查 `kb/media/`，不查 `kb/store/`。
    """
    import re

    pattern = re.compile(
        r"\b(insert\s+into|update\s+[a-z_]+\s+set|delete\s+from|create\s+table|"
        r"drop\s+table)\b",
        re.IGNORECASE,
    )
    offenders: list[str] = []
    for path in media_files():
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if pattern.search(node.value):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, "媒体层不许出现写库 SQL：" + "；".join(offenders)


# ── 约束四：不许 print ────────────────────────────────────


def test_没有文件调用print() -> None:
    """产品代码用 logging。ruff 的 T20 也会拦，但那条依赖 ruff 被跑过。"""
    offenders: list[str] = []
    for path in media_files():
        for node in ast.walk(_parse(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, "kb/ 下不许 print（用 log）：" + "；".join(offenders)

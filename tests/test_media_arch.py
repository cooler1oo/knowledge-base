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
import re
import subprocess
import sys
from pathlib import Path

from kb.store import attachments as attachments_store

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


def docstring_nodes(tree: ast.Module) -> set[int]:
    """模块 / 类 / 函数 docstring 那个 `Constant` 节点的 `id()`。

    ## 为什么必须排除 docstring

    docstring 在 AST 里就是一个 `ast.Constant(str)` 节点，与真正的 SQL 字符串
    常量**长得一模一样**。不排除的后果是：一句**说明「本层不写库」**的注释
    反而被判违规——

    ```python
    # 一句说明「本层不写库」的 docstring：
    #   本层不执行 delete from attachments（写库在 store 层）
    #            ^^^^^^^^^^^^^^^ 命中正则 → 误报
    ```

    实测过：在 `kb/media/layout.py` 里加这一句，这条用例立刻红。
    而这与 `test_replace_for_item不写on_conflict` 那个 bug 是**同一类**
    ——「把注释/文档当代码」。那一处是恒红（`getsource` 含注释），
    这里是误报。两者的危害相同：恒红的断言会被人当噪声关掉。

    ## 为什么仍然要查其他字符串常量

    真正要拦的是 `conn.run("delete from items")` 这种**可执行**的字符串。
    它也是 `ast.Constant`，但不是 docstring，所以会被保留在检查范围内。

    判据用「节点身份」而不是「是否在文件开头」：用 `ast.get_docstring` 拿不到
    节点对象，只能拿字符串值，而值可能重复（两处写同一句话）。按 `id()` 排除
    才不会误杀一个恰好与 docstring 同文的真实 SQL 常量。
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


#: 写库 SQL 的判据。**只有一份**，被 `test_没有文件出现写库SQL` 与它的自检
#: 共用 —— 两处各写一份的话，自检改不了被测逻辑，测的就是个副本（实测踩过：
#: 把真实检查里的 docstring 过滤删掉，自检照样绿）。
_WRITE_SQL_RE = re.compile(
    r"\b(insert\s+into|update\s+[a-z_]+\s+set|delete\s+from|create\s+table|"
    r"drop\s+table)\b",
    re.IGNORECASE,
)


def write_sql_strings(tree: ast.Module) -> list[tuple[int, str]]:
    """源码里**可执行**的、命中写库关键字的字符串常量 `(行号, 内容)`。

    docstring 被排除：它是说明文字，不是 SQL。判据见 `docstring_nodes`。

    ## ⚠️ 必须按 `id()` 排除，不能按内容匹配

    后来人最容易「优化」成按内容排除（`if node.value not in docstring_texts`），
    那样会**把真实的写库 SQL 一起放过**。

    反例：同一个函数里 docstring 与真实 SQL 写的是同一句话——

    ```python
    def f(conn):
        \"\"\"delete from items\"\"\"      # docstring，不该报
        conn.run("delete from items")  # 真实 SQL，必须报
    ```

    按内容排除：两条的内容都是 `delete from items`，于是**两条都被放过**
    ——安全检查在最该报警的地方失明。按节点身份（`id()`）排除只影响 docstring
    那一个节点，函数体里的同文常量仍会被抓。

    注意 `id()` 只在单次 `ast.parse` 的生命周期内有效：`tree` 活着，节点就不会
    被回收，所以同一个 `tree` 内部比对是安全的。**不要跨 tree 复用 `id()`。**
    """
    skip = docstring_nodes(tree)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
        and _WRITE_SQL_RE.search(node.value)
    ]


def test_没有文件出现写库SQL() -> None:
    """媒体层不许出现**可执行**的写库 SQL 字符串。

    与 `eng_checks` 的正则口径一致，但**排除 docstring**：docstring 里写
    「本层不执行 delete from …」是说明文字，不是 SQL。不排除会误报——
    详见 `docstring_nodes` 的 docstring 与
    `test_写库SQL检测不把docstring当违规` 那条自检。

    判据在 `write_sql_strings` 里**只有一份**，自检直接调它。这样自检才能
    真的守住这个检查——只测副本的话，把这里的过滤删掉自检照样绿（实测过）。

    只查 `kb/media/`：`kb/store/attachments.py` 里当然有 `delete from`。
    """
    offenders: list[str] = []
    for path in media_files():
        for lineno, _text in write_sql_strings(_parse(path)):
            offenders.append(f"{path.name}:{lineno}")
    assert not offenders, "媒体层不许出现写库 SQL：" + "；".join(offenders)


def test_写库SQL检测不把docstring当违规() -> None:
    """**反向自检：这条断言不能抓注释，也不能漏掉真 SQL。**

    `test_replace_for_item不写on_conflict` 踩过的坑是「断言抓到注释 → 恒红」。
    这里防的是对偶问题：「断言抓到 docstring → 误报」。两者危害相同：
    恒红/误报的断言会被人当噪声关掉，之后真违规也没人看见。

    ## 为什么必须调 `write_sql_strings` 而不是就地写一份逻辑

    第一版这条自检自己写了一遍过滤（`[n for n in walk if id(n) not in skip]`），
    结果是**它测的是副本**：把 `test_没有文件出现写库SQL` 里的过滤删掉，
    这条自检照样绿（已实测）。那是「自检与被测逻辑各自演化」的典型失败。
    现在只有一份实现，改坏它这条必红。

    ## 为什么五个方向都要验

    - ①②③ docstring（模块/函数/类）提到 `delete from` → 不该报
    - ④ 真正的 `conn.run("delete from …")` → **必须**报
    - ⑤ docstring 与真实 SQL 同文 → 只报真实那条

    只验前半会漏掉「过滤过头把真 SQL 也滤了」——那样最危险：检查形同虚设
    却全绿。⑤ 专门验「按内容去重」这种错误实现。
    """

    def offenders(src: str) -> list[str]:
        return [text for _line, text in write_sql_strings(ast.parse(src))]

    # ① 模块 docstring 提到它 → 不报
    assert offenders('"""本层不执行 delete from items。"""\n') == []
    # ② 函数 docstring 提到它 → 不报
    assert (
        offenders('def f() -> None:\n    """不写 delete from items。"""\n    pass\n')
        == []
    )
    # ③ 类 docstring → 不报
    assert offenders('class C:\n    """不写 delete from items。"""\n') == []
    # ④ 真正可执行的 SQL 字符串 → **必须**报
    assert offenders('def f(conn):\n    conn.run("delete from items")\n') == [
        "delete from items"
    ]
    # ⑤ docstring 与真实 SQL 同文时，仍只报后者
    mixed = '"""delete from items"""\ndef f(conn):\n    conn.run("delete from items")\n'
    assert offenders(mixed) == ["delete from items"], (
        "按内容去重会误杀：docstring 与真实 SQL 同文时，必须只报真实的那条"
    )


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


# ── 约束五：`_REPLACE_SQL` 的执行依赖锚点 ────────────────────
#
# 这两条本是 `tests/test_store_attachments_pg.py` 里的用例，2026-11-28 移到这里。
#
# **为什么移**：那个文件后来加了模块级 `pytestmark = pytest.mark.pg`（为了让
# `-m pg` 能选中它），于是文件里所有用例都带上 pg 标记。而这两条**不需要数据库**
# ——它们的全部意义就是「在没有真库的环境里也守住锚点」。带标记之后，
# 任何 `-m "not pg"` 的 CI 配置会静默跳过它们，防线就没了。
#
# 实测：`pytest tests/test_store_attachments_pg.py -m "not pg" --collect-only`
# → `30 tests collected` 变 `no tests collected`。
#
# 这个教训与 F2/F3 是同一个：**静态断言不许挂在需要真库的文件上**。


def _sql_without_comments(sql: str) -> str:
    """剥掉 SQL 里的 `--` 行注释。

    ## 为什么断言前必须先剥注释

    `_REPLACE_SQL` 的锚点上方有一大段解释「为什么要引用 `gone`」的注释，
    而注释里**反复出现 `gone` 这个词**。所以不剥注释时：

    ```python
    assert "gone" in ins          # 恒真 —— 注释里就有
    ```

    实测过：把真正的锚点 `where (select count(*) from gone) >= 0` 换成
    `where true`，这条断言照样绿，因为注释里还留着十几个 `gone`。
    **断言被自己的注释架空了。**

    这与 `test_replace_for_item不写on_conflict` 是同一类错的第三种形态：
    - 那次是 `getsource` 把注释当代码 → 恒红
    - `test_没有文件出现写库SQL` 是把 docstring 当代码 → 误报
    - 这里是**注释让断言恒真** → 恒绿，最隐蔽（恒绿没人会去看）

    注释里的引号内可能含 `--`（比如 `'-'`），所以只在**未被引号包住**时剥。
    本项目的 SQL 常量里没有字符串字面量含 `--`，简化为按行首次出现 `--` 截断。
    """
    return "\n".join(line.split("--")[0] for line in sql.splitlines())


def test_CTE的ins必须依赖gone() -> None:
    """`_REPLACE_SQL` 的 `ins` 必须引用 `gone`。

    ## 它是次要防线，不是主要防线

    主要防线是 `tests/test_store_attachments_pg.py::test_同key连续替换两次不撞唯一索引`
    （断言行为，需真库）。这条断言字符串，作用是**在没有真库的环境里也能挡住
    「把那一行当冗余删掉」** ——因为 pg 用例在本机全部 skip，只留行为用例等于
    没有防线。

    代价是重构 SQL（换写法但行为不变）时可能误报。这个代价可以接受：
    `_REPLACE_SQL` 是安全敏感的原子性载体，改它应当被盯着看。

    ## PG 的依据

    文档 §7.8.2：WITH 子语句「以同一快照并发执行，实际顺序不可预测」。
    外层只读 `ins` 时，`gone`（DELETE）可能被推迟到 INSERT **之后** →
    旧行还在堆上 → 撞 `attachments_key_uniq`。

    真机 A/B 对照（平板 PG）已确认：保留锚点 → 成功；去掉 → `duplicate key`。
    """
    ins = _sql_without_comments(attachments_store._REPLACE_SQL)
    ins = ins[ins.find("), ins as (") :]
    assert ins, "找不到 `), ins as (` 这一段——SQL 结构变了，这条断言要跟着改"
    assert "gone" in ins, (
        "ins 必须引用 gone，否则 DELETE 可能排在 INSERT 之后，"
        "重跑同一条内容时撞 attachments_key_uniq"
    )


def test_CTE依赖条件用大于等于0而不是大于0() -> None:
    """`>= 0` 与 `> 0` 的差别在**首次** fetch 上。

    首次 fetch 时表里没有旧附件，`gone` 一行都没删，`count(*) = 0`。
    写成 `> 0` 会让第一次写入一条都插不进去——把「重跑会撞」换成
    「首次全丢」，更糟。

    这条同样用字符串断言（不能靠行为验：`> 0` 的行为失败是「首次写入 0 行」，
    而首次写入的用例 `test_首次写入返回写入行数` 也能抓到——两条互补）。
    """
    sql = _sql_without_comments(attachments_store._REPLACE_SQL)
    assert ">= 0" in sql, "依赖条件必须是 >= 0，> 0 会让首次 fetch 插不进任何行"

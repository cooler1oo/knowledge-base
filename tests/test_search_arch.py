"""T01 的三条架构边界，全部靠**读源码**验，不依赖 pg、不联网。

这三条的共同点：它们的违反都不会在运行时报错，只会让将来的某次排查走错方向。

| 规则 | 违反了会怎样 | 本文件怎么守 |
|---|---|---|
| `kb/search/` 不得 import `kb.store` | 「纯逻辑层不碰数据库」这句话失去可执行性；`Indexer` 的用例变成必须连库 | 扫每个文件的 AST |
| 索引 uid 只在 `config.py` 出现一次 | 改名时漏一处，文档写进另一个索引，搜索页永远少几条 | 数每个文件里的字面量 |
| `IndexDoc` 字段集合与设计表一致 | 「索引多一个字段」或「少一个可搜字段」都不报错，只表现为搜不到 | 断言有序字段名元组 |

**为什么这些不做成注释里的规矩**（`AGENTS.md` §八）：注释不会拦住任何人。
`python -m tools.eng_checks --arch` 也会独立检查第一条——两处都查不是冗余，
`layer_rules.toml` 是声明式规则（能被别的项目复用），本文件是「这条规则是否
真的覆盖到了 `kb/search/`」的守门（规则文件写错目录名时 `--arch` 会静默通过）。
"""

from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path

import pytest

from kb.search import config, models

SEARCH_DIR = Path(__file__).resolve().parents[1] / "kb" / "search"


def _py_files() -> list[Path]:
    files = sorted(SEARCH_DIR.glob("*.py"))
    assert files, f"没在 {SEARCH_DIR} 找到任何 .py；目录名写错了？"
    return files


def _imported_modules(path: Path) -> list[str]:
    """文件里所有 `import x` / `from x import y` 的模块名（只认绝对导入）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append(node.module)
    return found


# ── 规则一：kb/search/ 不得 import kb.store ─────────────────────


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: p.name)
def test_检索层不import_store层(path: Path) -> None:
    """`kb/search/` 不知道 `kb.store` 存在（设计 §1.3）。

    三种写法都要拦住：`import kb.store`、`from kb.store import x`、
    `from kb import store`。第三种最容易漏——`_imported_modules` 收的是
    `"kb"`，而 `"kb".startswith("kb.store")` 是 False。所以判据里要显式列出
    `from kb import store` 这一形态。

    为什么这条边界值得一条测试：把兜底检索写进 `kb/search/` 是最省事的实现，
    而它的代价（`Indexer` 的用例从「一个假 HTTP + 一个 dict 列表」变成
    「要么连库、要么把 store 一起打桩」）在写的那一刻完全看不出来。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "kb.store" or alias.name.startswith("kb.store."):
                    offenders.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:  # 相对导入，与本条无关
                continue
            if module == "kb.store" or module.startswith("kb.store."):
                offenders.append(f"from {module} import ...")
            elif module == "kb":
                names = [alias.name for alias in node.names]
                if "store" in names:
                    offenders.append("from kb import store")

    assert not offenders, f"{path.name} 违规：{offenders}"


def test_检索层也不import数据库驱动() -> None:
    """`pg8000` 等驱动同样不得出现。

    `layer_rules.toml` 的 `driver_deny` 只放行 `kb/store/`，所以这条对
    `kb/search/` 本来就已经生效。本用例的价值是**在没跑 `--arch` 的单测里
    也能立刻看到**——一次 `pytest` 就暴露，不必等提交钩子。
    """
    drivers = ("pg8000", "psycopg", "psycopg2", "sqlite3")
    for path in _py_files():
        for module in _imported_modules(path):
            assert not any(
                module == d or module.startswith(d + ".") for d in drivers
            ), f"{path.name} 直接 import 了数据库驱动 {module}"


# ── 规则二：索引 uid 只在 config.py 出现一次 ────────────────────


def test_索引uid只在config出现一次() -> None:
    """设计 §10 硬约定。`config.py` 命中 1 次，其余文件 0 次。

    只数源码字符串，不含注释：注释里写「本包写 `kb_items` 索引」不是违规
    （它是解释，不是第二份真源）。所以判据是 AST 里的常量字符串。
    """
    hits: dict[str, int] = {}
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        count = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == config.INDEX_UID:
                count += 1
        if count:
            hits[path.name] = count

    assert hits == {"config.py": 1}, (
        f"索引 uid {config.INDEX_UID!r} 应只在 config.py 出现 1 次，实际 {hits}"
    )


def test_indexer_用的是config里的uid而不是自己的字面量() -> None:
    """`Indexer` 的构造默认值必须**等于** `config.INDEX_UID`。

    这条与上一条互补：上一条拦「源码里出现第二个 `kb_items` 字面量」，
    这条拦「默认值被改成了别的字符串」——那种写法不带 `kb_items` 字面量，
    所以前一条抓不到，但它会让 worker 把文档写进一个没人搜的索引。

    断言的是**运行时取值**而不是源码里的 AST 形状：`from .config import INDEX_UID`
    与 `import ... as config; config.INDEX_UID` 两种写法都正确，前者更简洁，
    用 AST 判形状会把正确写法判成违规（本用例第一版就是这么红的）。
    """
    import inspect

    from kb.search import indexer

    default = (
        inspect.signature(indexer.Indexer.__init__).parameters["index_uid"].default
    )
    assert default == config.INDEX_UID, (
        f"Indexer 的 index_uid 默认值是 {default!r}，应为 config.INDEX_UID"
        f"（{config.INDEX_UID!r}）"
    )


# ── 规则三：IndexDoc 字段集合逐字一致 ───────────────────────────


def test_IndexDoc字段集合与设计表逐字一致() -> None:
    """设计 §3.1 的 12 个字段，**有序**。

    断言有序元组而不是集合：集合会让「字段顺序被改了」这件事静默通过，
    而字段顺序在本项目里是有语义的（`searchableAttributes` 的权重序与它同名，
    改索引文档的顺序虽不影响 Meili，但会让人以为权重序也改了）。
    """
    assert tuple(f.name for f in dataclasses.fields(models.IndexDoc)) == (
        "id",
        "title",
        "summary",
        "tags",
        "ocr_text",
        "content_text",
        "author",
        "kind",
        "read_status",
        "saved_at",
        "saved_at_ts",
        "source_domain",
    )


def test_正文单独命中分与标签分都高于阈值() -> None:
    """M6-01 要求的五路可搜字段必须全在 `IndexDoc` 里（判据原文）。

    少了任何一个都不是「少个字段」这么轻：M6-01 明写检索要覆盖
    标题、正文、摘要、标签、OCR 五路，缺一路就是功能级缺口。
    """
    names = {f.name for f in dataclasses.fields(models.IndexDoc)}
    assert {"title", "summary", "content_text", "ocr_text", "tags"} <= names


def test_IndexDoc的tags出列表而不是元组() -> None:
    """`json.dumps` 不认元组（会抛 TypeError）。

    这条拦的是「`to_meili_dict` 里忘了 `list(...)`」——PyPI 的 Meili 客户端
    有 pydantic 兜着，自写薄客户端没有，错误会以
    `Object of type tuple is not JSON serializable` 出现在第一次写索引时。
    """
    doc = models.IndexDoc(
        id=1,
        title="t",
        summary="",
        tags=("a", "b"),
        ocr_text="",
        content_text="",
        author="",
        kind="text",
        read_status="unread",
        saved_at="2026-11-22T10:00:00+08:00",
        saved_at_ts=1,
        source_domain="",
    )
    payload = doc.to_meili_dict()
    assert isinstance(payload["tags"], list)
    assert payload["tags"] == ["a", "b"]


# ── 常量与打包 ─────────────────────────────────────────────────


def test_阈值常量在config里且不为零() -> None:
    """`MIN_SCORE` 不许被改成 0（设计 §5.4 明确「不许取消截断」）。

    取消截断会把 0.285 那条垃圾召回放回结果里，等于用「多几条噪音」
    换「不漏正文」，方向反了。
    """
    assert config.MIN_SCORE > 0
    assert 0.285 < config.MIN_SCORE < 0.352, (
        f"阈值 {config.MIN_SCORE} 必须落在 (0.285, 0.352) 开区间内："
        "低于 0.285 会放回垃圾召回，高于 0.352 会截掉正文单独命中"
    )


def test_时区常量是上海且只在config出现一次() -> None:
    """`DISPLAY_TZ` 是「按天切」的唯一口径（设计 §3.9）。

    判据两条：值必须是 `Asia/Shanghai`（不是 UTC），且 `kb/search/` 里
    不允许出现第二个 `ZoneInfo(...)` 调用——那意味着有人自己挑了一个时区。
    """
    assert config.DISPLAY_TZ == "Asia/Shanghai"

    offenders: list[str] = []
    for path in _py_files():
        if path.name == "config.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "ZoneInfo":
                    offenders.append(path.name)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"timezone", "ZoneInfo"}:
                    offenders.append(path.name)
    assert not offenders, f"除 config.py 外不得自己造时区：{sorted(set(offenders))}"


def test_pyproject登记了kb_search() -> None:
    """不登记时 `pip install -e .` 装出来的包缺这个目录。

    测试靠 `pythonpath = ["."]` 照跑得通，只有部署到平板后 `import kb.search`
    才失败——`kb.vision` 踩过同一个坑，`pyproject.toml` 的注释里留着记录。
    """
    root = Path(__file__).resolve().parents[1]
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'"kb\.search"', text), (
        "pyproject.toml 的 packages 里没有 kb.search"
    )


def test_layer_rules登记了检索层() -> None:
    """`layer_rules.toml` 少了这条时，`--arch` 对 `kb/search/` 只剩两条规则生效，
    「import kb.store」会静默漏检（`kb/vision/` 与 `kb/media/` 的注释记着同一件事）。
    """
    root = Path(__file__).resolve().parents[1]
    text = (root / "layer_rules.toml").read_text(encoding="utf-8")
    assert 'path_prefix = "kb/search/"' in text
    assert "检索层" in text

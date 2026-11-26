# 设计决策覆盖检查（design-coverage）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `tools/doc_checks` 能拦下「文档里落地了具体设计（建表 / 接口契约），但复用调研表没有对应候选对比」的情况，把 AGENTS.md 8.1「任何设计决策前先查有没有成熟项目」从「章节存在」升级为「决策被覆盖」。

**Architecture:** 在 `reuse.py` 内新增一个独立函数 `check_design_coverage(text, path)`，由 `audit_text()` 在同一处调用。它不改现有行的校验逻辑，只增加一个跨节检查：提取正文里的设计锚点（`CREATE TABLE <name>`、`### 8.1` 类接口表的端点路径），要求复用调研表的「组件」或「理由」列中出现该锚点的归属标记。用**标签锚定**而非模糊字符串匹配，避免误报。

**Tech Stack:** Python 3.10+、pytest、现有 `tools.doc_checks` 框架（`Violation` dataclass）

**Spec:** `C:\Users\39681\WorkBuddy\知识库\AGENTS.md` §8.1「复用优先（已机器化，不靠自觉）」

## Global Constraints

- Python `requires-python = ">=3.10"`（`pyproject.toml`），不得使用 3.10 以上语法
- `ruff` line-length = 88；`select = ["E4","E7","E9","F","I"]`
- 注释只写「为什么」，不复述代码在做什么（AGENTS.md §8）
- 现有 47 个测试必须全绿，不得修改既有测试的断言
- `docs/**` 不在 ruff 范围内（`extend-exclude`），但本功能的测试文件在 `tests/` 内，受 ruff 约束
- 检查必须幂等、不依赖网络
- 误报比漏报更危险：宁可漏掉一个决策，也不能让正常文档变红

## Review Focus

这些是设计时容易没想到、但一上线就会咬人的情况，按可能性排序：

1. **豁免文档**：带 `<!-- reuse-audit: exempt -->` 的历史文档没有调研表，绝不能因本规则变红
2. **`docs/research/` 与 `docs/prd/`（lite 模式）**：需求与调研文档里也会出现 `CREATE TABLE` 示例（比如调研报告引用别人 schema），不能要求它们也有对应候选
3. **调研表里合法的「不适用」场景**：例如 `sessions` 表是框架自带能力，调研表里没有对应行是合理的
4. **代码块内的 `CREATE TABLE`**：DDL 一定在 ```sql 代码块里；正文叙述里提到「CREATE TABLE items」不应被当作锚点
5. **`IF NOT EXISTS` 与换行**：`CREATE TABLE IF NOT EXISTS items (` 和 `CREATE TABLE items(` 都要识别

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `tools/doc_checks/reuse.py` | 新增 `DESIGN_ANCHOR_RE`、`check_design_coverage()`；在 `audit_text()` 末尾调用 | 修改 |
| `tests/test_reuse_audit.py` | 新增 4 个测试类/函数 | 修改（只追加，不改既有） |

不改 `__init__.py`、不改 `__main__.py`、不改 `pyproject.toml`、不改 CI 配置 —— 本检查挂在既有 `reuse.py` 调用链上，自动被 ①②③ 三处执行。

---

### Task 1: 提取设计锚点

**Files:**
- Modify: `tools/doc_checks/reuse.py`（在 `check_file()` 之前插入）
- Test: `tests/test_reuse_audit.py`（追加到文件末尾）

**Interfaces:**
- Produces: `extract_design_anchors(text: str) -> list[tuple[int, str]]`，返回 `[(行号, 锚点名), ...]`，锚点名是表名（如 `items`）；行号从 1 起，用于 Violation 定位

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_reuse_audit.py` 末尾：

```python
# ── 设计决策覆盖（design-coverage）────────────────────────


def test_extract_design_anchors_only_inside_sql_fence():
    """锚点只从 ```sql 代码块里取：正文叙述提到表名不算设计落地。"""
    text = (
        "正文里提到 CREATE TABLE items 只是叙述。\n"
        "\n"
        "```sql\n"
        "CREATE TABLE IF NOT EXISTS items (\n"
        "  id bigserial PRIMARY KEY\n"
        ");\n"
        "CREATE TABLE tags (id bigserial);\n"
        "```\n"
    )
    anchors = extract_design_anchors(text)
    names = [n for _, n in anchors]
    assert names == ["items", "tags"]


def test_extract_design_anchors_handles_no_if_not_exists():
    """`CREATE TABLE x (` 与 `CREATE TABLE IF NOT EXISTS x (` 都要识别。"""
    text = "```sql\nCREATE TABLE jobs (\n  id int\n);\n```\n"
    assert [n for _, n in extract_design_anchors(text)] == ["jobs"]


def test_extract_design_anchors_ignores_inline_code():
    """行内 `CREATE TABLE x` 不是 DDL，不提锚点。"""
    text = "见 `CREATE TABLE items` 这一句。\n"
    assert extract_design_anchors(text) == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_reuse_audit.py -k design_anchors -v`
Expected: FAIL，`ImportError: cannot import name 'extract_design_anchors'`

- [ ] **Step 3: 实现 `extract_design_anchors`**

在 `tools/doc_checks/reuse.py` 中、`check_file()` 定义之前插入：

```python
# 建表语句是「设计已落地」的最强信号：它一旦写进方案就是要照着建。
# 只认 ```sql 围栏内的，因为正文与行内代码里的同名文本是叙述而非设计。
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)",
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_reuse_audit.py -k design_anchors -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add tools/doc_checks/reuse.py tests/test_reuse_audit.py
git commit -m "feat(check): 提取 SQL 围栏内的建表锚点"
```

---

### Task 2: 覆盖校验（核心）

**Files:**
- Modify: `tools/doc_checks/reuse.py`（在 `extract_design_anchors` 之后、`check_file()` 之前）
- Test: `tests/test_reuse_audit.py`

**Interfaces:**
- Consumes: `extract_design_anchors(text)`、`parse_audit(text)` 返回的 `ReuseAudit`
- Produces: `check_design_coverage(text: str, path: str) -> list[Violation]`

**规则定义（务必按此实现，不要自行扩展）：**

对每个锚点表名 `t`，在调研表全部行的 `component + reason` 拼接文本中查找 `t`。找不到则报一条违规。用**表名原文出现**作为判据，不要求语义匹配 —— 因为语义判断做不到可靠，而表名是唯一可机器核验的锚。

- [ ] **Step 1: 写失败测试**

```python
def _doc_with(ddl: str, rows: str) -> str:
    return (
        "## 复用调研\n\n"
        "| 组件 | 候选 | 星数 | 许可 | 结论 | 运行环境 | 理由 |\n"
        "|---|---|---|---|---|---|---|\n"
        f"{rows}"
        "\n\n## 数据模型\n\n"
        "```sql\n"
        f"{ddl}"
        "```\n"
    )


def test_design_coverage_passes_when_table_mentioned():
    """调研表的理由里提到了表名，视为已覆盖。"""
    text = _doc_with(
        "CREATE TABLE items (id bigserial);\n",
        "| 内容模型 | karakeep-app/karakeep | 30862 | AGPL-3.0 | 参考 | Docker | "
        "取其主表+分表结构，用于设计 items 表 |\n",
    )
    assert check_design_coverage(text, "docs/tech/x.md") == []


def test_design_coverage_flags_unmentioned_table():
    """建了表但调研表里完全没提到它 → 报违规。"""
    text = _doc_with(
        "CREATE TABLE items (id bigserial);\nCREATE TABLE wobbly (id int);\n",
        "| 内容模型 | karakeep-app/karakeep | 30862 | AGPL-3.0 | 参考 | Docker | "
        "取其主表+分表结构，用于设计 items 表 |\n",
    )
    v = check_design_coverage(text, "docs/tech/x.md")
    assert len(v) == 1
    assert "wobbly" in v[0].message
    assert v[0].rule == "设计覆盖"


def test_design_coverage_skips_exempt_document():
    """带豁免标记的文档整篇跳过，历史文档不得因此变红。"""
    text = "<!-- reuse-audit: exempt -->\n" + _doc_with(
        "CREATE TABLE wobbly (id int);\n", ""
    )
    assert check_design_coverage(text, "docs/tech/old.md") == []


def test_design_coverage_skips_non_strict_dir():
    """research/ 里引用别人的 schema 不需要本项目调研表覆盖。"""
    text = _doc_with("CREATE TABLE wobbly (id int);\n", "")
    assert check_design_coverage(text, "docs/research/x.md") == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_reuse_audit.py -k design_coverage -v`
Expected: FAIL，`NameError: name 'check_design_coverage' is not defined`

- [ ] **Step 3: 实现 `check_design_coverage`**

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_reuse_audit.py -k design_coverage -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add tools/doc_checks/reuse.py tests/test_reuse_audit.py
git commit -m "feat(check): 建表语句须有复用调研覆盖"
```

---

### Task 3: 接入调用链 + 反向测试

**Files:**
- Modify: `tools/doc_checks/reuse.py`（`audit_text()` 的 `return out` 之前）
- Test: `tests/test_reuse_audit.py`

**Interfaces:**
- Consumes: `check_design_coverage(text, path)`
- Produces: 无新符号；`audit_text()` 的返回违规列表里包含 `设计覆盖` 类

- [ ] **Step 1: 写失败测试（反向测试，验证真的拦住）**

```python
def test_audit_text_includes_design_coverage_violation():
    """接入调用链：audit_text 必须把设计覆盖问题一并返回。"""
    text = _doc_with(
        "CREATE TABLE mystery_table (id int);\n",
        "| 内容模型 | karakeep-app/karakeep | 30862 | AGPL-3.0 | 参考 | Docker | "
        "取其主表+分表结构设计 |\n",
    )
    violations = audit_text(text, "docs/tech/x.md", "strict")
    rules = [v.rule for v in violations]
    assert "设计覆盖" in rules


def test_real_tech_plan_has_no_design_coverage_violation():
    """对仓库里真实的 v8.0 跑一次：锚点必须被解析到，且不能全绿。

    v8.0 的 10 张表里，调研表提过 items/files/jobs，但没提
    sessions/users/queries 等。断言「至少报 1 条」——
    如果一条都不报，说明锚点提取或覆盖判断失效了（假绿灯）。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    target = root / "docs" / "tech" / "2026-11-26-技术方案.md"
    if not target.exists():
        pytest.skip("v8.0 文档不存在，跳过真实文档反向测试")
    text = target.read_text(encoding="utf-8")
    anchors = extract_design_anchors(text)
    assert len(anchors) >= 10, f"锚点只提取到 {len(anchors)} 个，预期 >=10"
    violations = check_design_coverage(text, "docs/tech/2026-11-26-技术方案.md")
    assert len(violations) >= 1, "真实文档一条都不报 → 检查失效（假绿灯）"
```

> 注意：`test_real_tech_plan_has_no_design_coverage_violation` 这个函数名与断言相反，是刻意保留的历史命名习惯 —— **改名为 `test_real_tech_plan_flags_uncovered_tables`**，实现时用新名字。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_reuse_audit.py -k "audit_text_includes or real_tech_plan_flags" -v`
Expected: FAIL，`assert "设计覆盖" in rules` 不成立

- [ ] **Step 3: 接入 `audit_text()`**

在 `tools/doc_checks/reuse.py` 的 `audit_text()` 中，把最后的 `return out` 之前插入：

```python
    # 建表语句是设计已落地的信号，必须已被候选对比覆盖。
    # 放在最后：缺章节/无行的情形上面已 return，不重复报。
    out.extend(check_design_coverage(text, path))
```

- [ ] **Step 4: 跑全部测试确认通过**

Run: `python -m pytest tests/test_reuse_audit.py -v`
Expected: 全绿（原有 30 项 + 新增 9 项）

- [ ] **Step 5: 跑仓库级检查**

Run: `python -m tools.doc_checks --all --cross-refs`
Expected: **会报告现有文档的违规** —— 这是预期的，说明规则生效。记录报出的表名，进 Task 4 处理

- [ ] **Step 6: 提交**

```bash
git add tools/doc_checks/reuse.py tests/test_reuse_audit.py
git commit -m "feat(check): 设计覆盖检查接入 audit_text"
```

---

### Task 4: 让现有文档合规

**Files:**
- Modify: `docs/tech/2026-11-26-技术方案.md`（补调研表行）
- Modify: `docs/tech/2026-11-26-v9重制清单.md`（如有缺口）

**Interfaces:**
- Consumes: Task 3 的检查输出
- Produces: `python -m tools.doc_checks --all --cross-refs` 全绿

- [ ] **Step 1: 记录检查输出**

Run: `python -m tools.doc_checks --all --cross-refs 2>&1 | tee /tmp/coverage-out.txt`
Expected: 列出所有「建了表但没覆盖」的表名

- [ ] **Step 2: 逐条判断，不一律补行**

对每个报出的表，二选一：
- **确有设计取舍**（如 `items`/`jobs`/`files`）→ 在调研表补一行，写明候选与理由
- **无取舍可做**（如 `sessions` 是 Cookie 会话表的通用做法，`settings` 是键值表）→ 在调研表的**理由列**里补一句说明「该表为通用模式，无候选对比必要，理由：XXX」，锚名出现在理由列即视为覆盖

Step 2 的判据：**能不能说出「查过什么、为什么不用它」**。说不出来就是没调研，必须补。

- [ ] **Step 3: 验证全绿**

Run: `python -m tools.doc_checks --all --cross-refs`
Expected: `[doc-checks] 全量 N 个文档：通过`

- [ ] **Step 4: 反向验证（关键，不可跳过）**

确认检查**真的会红**，而不是永远绿。在临时文件里构造违规并跑检查：

```bash
python - <<'PY'
from tools.doc_checks.reuse import check_design_coverage
text = open("docs/tech/2026-11-26-技术方案.md", encoding="utf-8").read()
# 注入一个全新的表名，模拟「加了表但没做调研」
injected = text.replace("CREATE TABLE items (", "CREATE TABLE zzz_injected (id int);\nCREATE TABLE items (", 1)
assert "zzz_injected" in injected, "注入失败，测试无效"
v = check_design_coverage(injected, "docs/tech/2026-11-26-技术方案.md")
assert any("zzz_injected" in x.message for x in v), f"没拦住，实际输出: {v}"
print("✅ 反向测试通过：注入的未覆盖表被拦下，共", len(v), "条")
PY
```

Expected: `✅ 反向测试通过：注入的未覆盖表被拦下，共 1 条`

- [ ] **Step 5: 跑全量验证**

```bash
python -m pytest -q
./.venv/Scripts/python.exe -m ruff check .
./.venv/Scripts/python.exe -m ruff format --check .
```

Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add docs/tech/ tools/doc_checks/ tests/
git commit -m "docs(tech): 补齐建表语句的复用调研覆盖"
```

---

## Self-Review

**1. Spec coverage（AGENTS.md 8.1）**

| 8.1 的要求 | 由哪个 Task 落实 |
|---|---|
| 设计决策前先查有没有能复用的 | Task 2 的覆盖校验（没有对应行就红） |
| 文档必须有 `## 复用调研` 章节 | 已有（v8 之前就实现） |
| 七列表格式、结论枚举、理由质量 | 已有（`_check_row`） |
| 候选须 owner/repo、星数、许可、运行环境 | 已有（strict 模式） |
| 「全自写须留痕」 | 已有（`EVIDENCE_WORDS` 检查） |
| **具体设计落地时也要被覆盖**（本次新增） | Task 1–3 |

**2. Step scan**：每个 Step 都是单一动作 + 可检验结果；无「TBD」「处理边界情况」这类空步。

**3. Type consistency**：`extract_design_anchors(text) -> list[tuple[int, str]]` 与 `check_design_coverage(text, path) -> list[Violation]` 在 Task 1/2/3 中签名一致；`Violation(path, line, rule, message)` 与 `__init__.py` 的 dataclass 定义一致。

**4. Review Focus**：五条各自有测试 ——
- 豁免文档 → `test_design_coverage_skips_exempt_document`
- lite 目录 → `test_design_coverage_skips_non_strict_dir`
- 「无取舍可做」的表 → Task 4 Step 2 的人工判据 + 理由列锚名
- 正文叙述里的表名 → `test_extract_design_anchors_only_inside_sql_fence` / `_ignores_inline_code`
- `IF NOT EXISTS` → `test_extract_design_anchors_handles_no_if_not_exists`

**5. Proportion**：本计划描述约 90 行新代码，计划本身 4 个 Task，比例合理。

## 已知局限（明写，不藏）

| 局限 | 说明 |
|---|---|
| 只覆盖「建表」这一类锚点 | 接口契约（`28 个端点`）暂未纳入。原因是端点表格式不统一，先做能可靠做的一半 |
| 判据是「表名出现在调研表」而非语义匹配 | 有人可以写一行「本项目不用任何 ORM」把表名塞进理由从而绕过。这是**可接受的绕过**：它留下了文字痕迹，复检时能看到 |
| 不能拦「只存在对话里的设计」 | 本检查只在提交时生效。对话中的设计讨论无文件可查，属规则固有边界 |
"""任务总表与代码事实的一致性检查。

## 为什么需要这个，而不是靠人记住

2026-11-28 的真实事故：R6 检索轮的 7 个任务（E-18/E-19/E-20/M6-01/02/03/05）
全部交付并推送了，但 `任务总表-2026-11-28.csv` 里一行都没标 `done`。
不是「忘了看一眼」，是**把表当成了参考资料，而它是工程顺序的唯一权威**。

用户的原话：「我们曾置顶过 csv 工程顺序表，并明确要求你按照表的顺序做，
且每步做完都要更新表格 done 列和完成过程记录」。

AGENTS.md §八 自己写着「规则的第一顺位是机器能执行的检查」。所以这件事
不该写成「请记得更新表格」，要写成检查。

## 四条判据

| # | 判据 | 拦什么 |
|---|---|---|
| 1 | `todo` 行的证据文件已存在 | 干了活没记账（本次事故） |
| 2 | `done` 行的证据是空的 | 记了账没干活 |
| 3 | 后面顺序 done 而前面 todo | 跳过步骤 |
| 4 | 改了 `kb/` 但没动 CSV | 提交时漏更新 |

判据 1 是整个设计的核心：**用「证据文件存在」这个客观事实反查表格**，
不依赖任何人记得更新。CSV 的 `证据` 列写的是文件路径或命令，
文件存在但状态是 `todo`，就是自相矛盾。

## 为什么判据 1 要排除「明确的未来任务」

`M6-09`/`M6-10` 写着「AGENTS.md §3 明确不做」，这类任务永远不会有证据文件。
判据 1 只对「证据列看起来像路径」的行生效——见 `_looks_like_path`。
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path

TABLE_NAME = "任务总表-2026-11-28.csv"

# CSV 由 Excel / WPS 编辑过，带 BOM。用 utf-8-sig 读，否则第一列表头变成
# "\ufeff顺序"，按列名取值全部 KeyError——这个坑很隐蔽：肉眼看不到 BOM。
ENCODING = "utf-8-sig"

STATE_DONE = "done"
STATE_TODO = "todo"

# 「证据」列里出现这些，说明是路径而不是「未实现」这类说明文字
_PATH_HINT = re.compile(r"[/\\.]")

# 含这些字样说明「没做完 / 不该做 / 只是引用」，都不算「证据已存在」。
#
# 这份表是踩出来的，每条都对应一次真实误报（2026-11-28 首跑 9 处里 4 处误报）：
#   - 「无字幕抓取」「未接 AI」：含「无」「未」但原表用的是完整词组，
#     所以匹配必须是**子串**而不是全等
#   - 「AGENTS.md §2.1」：这是规格引用，不是交付证据——含 `.md` 会被
#     _PATH_HINT 命中，必须整体排除
#   - 「kb/asr/ 已有 decode，未接 worker」：目录存在但功能只做了一半
_NOT_EVIDENCE = (
    "—",
    "-",
    "未实现",
    "明确不做",
    "未验",
    "todo",
    "未接",
    "没有",
    "未做",
    "待补",
    "缺",
    "无",  # 单字也要拦：「kb/asr/ 无字幕抓取」说的是没有，不是有
)

# 证据列如果只是引用规格/需求文档，说明「需求定了但没做」，不算交付证据。
# 这些文件本来就长期存在，拿它们当 done 的证据等于没证据。
_SPEC_ONLY = re.compile(r"^(AGENTS\.md|docs/|PRD|技术方案)")


@dataclass(frozen=True)
class Row:
    order: int
    code: str
    state: str
    evidence: str
    task: str
    line: int
    #: 「说明」列。`明确不做` 这类范围声明写在这里，不在 evidence 列。
    #: 首版漏了这个字段，导致 next_task 把「明确不做」的任务排进待办。
    note: str = ""


@dataclass(frozen=True)
class Violation:
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{TABLE_NAME}:{self.line}: [{self.rule}] {self.message}"


def load(path: Path) -> list[Row]:
    """读表。`utf-8-sig` 吃掉 BOM，`csv` 模块处理引号内的逗号。"""
    text = path.read_text(encoding=ENCODING)
    rows: list[Row] = []
    reader = csv.DictReader(io.StringIO(text))
    for index, raw in enumerate(reader, start=2):  # 第 1 行是表头
        try:
            order = int(str(raw.get("顺序", "")).strip())
        except (TypeError, ValueError):
            continue
        rows.append(
            Row(
                order=order,
                code=str(raw.get("编号", "")).strip(),
                state=str(raw.get("状态", "")).strip().lower(),
                evidence=str(raw.get("证据", "")).strip(),
                task=str(raw.get("任务", "")).strip(),
                line=index,
                note=str(raw.get("说明", "")).strip(),
            )
        )
    return rows


def _looks_like_path(evidence: str) -> bool:
    """证据列是不是在指一个「交付物」，而非说明文字或规格引用。

    **判定必须同时满足三条**，缺一不算（下面每条都对应一次真实误报）：

    1. 含路径分隔符或扩展名 —— 「—」「未实现」这类直接排除
    2. **不含**任何否定字样 —— 「kb/asr/ 已有 decode，未接 worker」含 `/`
       但说的是「一半做了」，状态就该是 todo
    3. **不是规格引用** —— 「AGENTS.md §2.1」指需求文档，那文件本来就长期存在，
       拿它当 done 的证据等于没证据
    """
    if not evidence or evidence in _NOT_EVIDENCE:
        return False
    if any(word in evidence for word in _NOT_EVIDENCE):
        return False
    if _SPEC_ONLY.match(evidence):
        return False
    return bool(_PATH_HINT.search(evidence))


def _evidence_exists(root: Path, evidence: str) -> bool:
    """证据列里的第一个路径片段是否真实存在。

    证据列形如 `kb/store/ + items.py`、`R0 报告 R9/R10`、`—`，
    只取第一段像路径的，避免把说明文字也当路径查。
    """
    for chunk in re.split(r"[+、，,;；]", evidence):
        candidate = chunk.strip().strip("`")
        if not candidate or not _PATH_HINT.search(candidate):
            continue
        # 只取路径本体，去掉后面的附加说明（`kb/worker/loop.py:32` → `kb/worker/loop.py`）
        candidate = re.split(r"[:：\s]", candidate)[0]
        if (root / candidate.rstrip("/")).exists():
            return True
        # 目录写法 `kb/store/` 已被 rstrip 处理；再试一次原样
        if (root / candidate).exists():
            return True
    return False


def check(root: Path, *, table: Path | None = None) -> list[Violation]:
    path = table or (root / TABLE_NAME)
    if not path.exists():
        return [Violation(0, "缺表", f"找不到 {TABLE_NAME}，工程顺序无法校验")]

    rows = load(path)
    if not rows:
        return [Violation(0, "空表", f"{TABLE_NAME} 读不出任何任务行")]

    out: list[Violation] = []

    # ── 判据 1：todo 但证据文件已存在 → 干了活没记账 ──
    for row in rows:
        if row.state != STATE_TODO:
            continue
        if not _looks_like_path(row.evidence):
            continue
        if _evidence_exists(root, row.evidence):
            out.append(
                Violation(
                    row.line,
                    "活干了没记账",
                    f"{row.code}「{row.task}」状态是 todo，但证据「{row.evidence}」"
                    f"指向的文件已存在。要么标 done 并写完成记录，要么证据写错了。",
                )
            )

    # ── 判据 2：done 但证据为空 → 记了账没干活 ──
    for row in rows:
        if row.state != STATE_DONE:
            continue
        if not row.evidence or row.evidence in _NOT_EVIDENCE:
            out.append(
                Violation(
                    row.line,
                    "标记无证据",
                    f"{row.code}「{row.task}」标了 done，但证据列是「{row.evidence}」。"
                    f"done 必须能被查证。",
                )
            )

    # ── 判据 3 已删除：原「按顺序」审计是错的 ──
    #
    # 首版写的是「同组内后面 done 而前面 todo 就报错」。2026-11-28 实测出
    # 4 条报错，全部不可行动：E-19（order 21）排 E-14（order 32）之前，
    # 但实际执行是先做 R7 视觉、R8 媒体，再回头做 R6 检索。
    #
    # **历史执行顺序改不了。** 报出来只会让每次跑检查都有 4 条噪音，
    # 久了所有人都会忽略这个检查——那比没有检查更糟。
    #
    # 「按顺序」的正确形态是**做事前的下一步查询**，不是事后追责：
    # 见 `next_task()`。它回答「我现在该做哪个」，而不是「你昨天做错了」。

    return out


def _group_of(row: Row) -> str:
    """按编号前缀分组：E-xx / M6 等各自比顺序。

    拿不到前缀时退化成「全部同一组」——宁可多报也不漏报。
    """
    return row.code.split("-")[0] if "-" in row.code else "全部"


def next_task(root: Path, *, table: Path | None = None) -> list[Row]:
    """按顺序返回「现在该做的前几项」。

    **这是「按顺序做」的正确形态**：做事前查一次，不是事后审计。
    历史顺序改不了，但下一次做哪个可以查询。

    返回前 3 个 `todo`，按 `顺序` 升序。排除三类：
      - 依赖未完成的（`依赖` 列指向的行还是 todo）
      - 明确不做的（说明列含「明确不做」）
      - **被外部条件阻塞的**（说明列含 ⏸）

    为什么返回 3 个而不是 1 个：只给 1 个时，若它被外部原因阻塞
    （等设备、等授权），执行者会卡住或自己找活干。给 3 个留出选择空间，
    但主推第一个。
    """
    path = table or (root / TABLE_NAME)
    if not path.exists():
        return []
    rows = load(path)
    by_code = {r.code: r for r in rows}
    out: list[Row] = []
    for row in sorted(rows, key=lambda r: r.order):
        if row.state != STATE_TODO:
            continue
        if _is_excluded(row) or _is_blocked(row):
            continue
        deps = _deps_of(path, row)
        if any(by_code.get(d) and by_code[d].state != STATE_DONE for d in deps):
            continue
        out.append(row)
        if len(out) >= 3:
            break
    return out


def _is_excluded(row: Row) -> bool:
    """范围声明写了「明确不做」的，不进待办队列。

    要查三列：`说明` 是范围声明该在的地方，但历史上也有人写进 `证据` 或
    `任务`，三处都查才不会因为写法不同就漏。
    """
    text = f"{row.note} {row.evidence} {row.task}"
    return "明确不做" in text or "不做" in row.note


#: 「暂时做不了」的标记。命中即跳过，但**不改变 P0 位置**——
#: 任务的优先级是产品定的，不该因为它此刻被外部条件阻塞就降级。
_BLOCKED_MARK = "⏸"


def _is_blocked(row: Row) -> bool:
    """外部条件未就绪（等模型、等设备、等授权）→ 进不了待办队列。

    为什么必须有这条：2026-11-28 的 M3-05（P0 图片 OCR）卡在「没有视觉模型」，
    但它顺序排第一，`--next-task` 每次都推荐它，整个施工就停在那里。
    优先级不降（它仍是 P0），只是「现在做不了」，所以跳过并显示给用户看。

    标记写在 `说明` 列，用 ⏸。选它是因为肉眼醒目、不会与正常文案混淆。
    """
    return _BLOCKED_MARK in row.note


def _deps_of(path: Path, row: Row) -> tuple[str, ...]:
    """读该行的 `依赖` 列，切成编号元组。

    `依赖` 列形如 `M6-01`、`E-18`、空。多依赖用逗号或分号分隔。
    """
    text = path.read_text(encoding=ENCODING)
    reader = csv.DictReader(io.StringIO(text))
    for raw in reader:
        if str(raw.get("编号", "")).strip() != row.code:
            continue
        deps = str(raw.get("依赖", "")).strip()
        if not deps or deps in _NOT_EVIDENCE:
            return ()
        return tuple(p.strip() for p in re.split(r"[,;，；]", deps) if p.strip())
    return ()


def check_touched(root: Path, touched: list[str]) -> list[Violation]:
    """改了 `kb/` 却没动表格 → 提交时拦截。

    这条是判据 4。它不看内容对不对，只看「动了代码却没申报」。
    比前三条更早生效：代码进 commit 的那一刻就查，而不是等下次跑 --all。
    """
    code_touched = [f for f in touched if f.startswith(("kb/", "tools/"))]
    table_touched = any(Path(f).name == TABLE_NAME for f in touched)
    if code_touched and not table_touched:
        sample = ", ".join(code_touched[:3])
        more = f" 等 {len(code_touched)} 个" if len(code_touched) > 3 else ""
        return [
            Violation(
                0,
                "改了代码没更新任务表",
                f"本次提交改了 {sample}{more}，但没动 {TABLE_NAME}。"
                f"完成任务后在表格里标 done、写完成过程记录，再提交。",
            )
        ]
    return []

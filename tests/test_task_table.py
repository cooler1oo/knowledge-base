"""任务总表一致性检查的规格说明。

这些用例守的不是「代码能跑」，而是**判据本身对不对**。首版实现出了
9 处报错、其中 4 处误报（2026-11-28 实测），全部因为判据写宽了。
误报比漏报更危险：报错多了执行者就学会忽略这个检查。

所以这里每种误报场景都有一条用例守。
"""

from __future__ import annotations

from pathlib import Path

from tools.eng_checks import task_table

HEADER = "顺序,编号,类别,阶段,任务,优先级,状态,依赖,说明,证据\n"


def _write(tmp_path: Path, body: str) -> Path:
    """造一张最小可用的任务表。带 BOM，与真实文件一致。"""
    path = tmp_path / task_table.TABLE_NAME
    path.write_text("\ufeff" + HEADER + body, encoding="utf-8")
    return path


# ── 判据 1：todo 但证据文件已存在 ──────────────────────────────


def test_todo_but_evidence_exists_is_flagged(tmp_path: Path) -> None:
    """核心场景：干了活没记账。

    2026-11-28 的真实事故——R6 七个任务全交付了，表里一行没标 done。
    """
    (tmp_path / "kb").mkdir()
    (tmp_path / "kb" / "done.py").write_text("x = 1\n", encoding="utf-8")
    table = _write(
        tmp_path,
        "1,E-01,工程,R1,做了但没记,工程,todo,,说明,kb/done.py\n",
    )

    problems = task_table.check(tmp_path, table=table)

    assert [p.rule for p in problems] == ["活干了没记账"]
    assert "E-01" in problems[0].message


def test_todo_with_missing_evidence_is_fine(tmp_path: Path) -> None:
    """真没做：证据指向的文件不存在，不该报。"""
    table = _write(tmp_path, "1,E-01,工程,R1,还没做,工程,todo,,说明,kb/ghost.py\n")
    assert task_table.check(tmp_path, table=table) == []


def test_no_evidence_marker_is_fine(tmp_path: Path) -> None:
    """证据写 `—` 表示没做，不查文件。"""
    table = _write(tmp_path, "1,E-01,工程,R1,还没做,工程,todo,,说明,—\n")
    assert task_table.check(tmp_path, table=table) == []


# ── 误报防线：这四条对应首版真实误报 ───────────────────────────


def test_spec_reference_is_not_evidence(tmp_path: Path) -> None:
    """`AGENTS.md §2.1` 是规格引用，不是交付证据。

    这些文件长期存在，拿它们当证据等于没证据。首版把它报成「干了活没记账」。
    """
    table = _write(
        tmp_path,
        "1,E-01,工程,R1,需求定了没做,工程,todo,,说明,AGENTS.md §2.1\n",
    )
    assert task_table.check(tmp_path, table=table) == []


def test_negative_word_in_evidence_is_not_evidence(tmp_path: Path) -> None:
    """含「未接」「无」这类否定字样，说明一半做了，不算交付。

    首版漏了单字「无」，把「kb/asr/ 无字幕抓取」报成已交付。
    """
    (tmp_path / "kb").mkdir()
    (tmp_path / "kb" / "asr").mkdir()
    for evidence in ("kb/asr/ 无字幕抓取", "kb/asr/ 有 decode，未接 worker"):
        table = _write(
            tmp_path,
            f"1,E-01,工程,R1,一半做了,工程,todo,,说明,{evidence}\n",
        )
        assert task_table.check(tmp_path, table=table) == [], evidence


def test_evidence_with_colon_suffix_resolves(tmp_path: Path) -> None:
    """`kb/worker/loop.py:32` 的行号后缀不该影响文件判定。"""
    (tmp_path / "kb" / "worker").mkdir(parents=True)
    (tmp_path / "kb" / "worker" / "loop.py").write_text("x = 1\n", encoding="utf-8")
    table = _write(
        tmp_path, "1,E-01,工程,R1,做了没记,工程,todo,,说明,kb/worker/loop.py:32\n"
    )
    assert [p.rule for p in task_table.check(tmp_path, table=table)] == ["活干了没记账"]


# ── 判据 2：done 但无证据 ─────────────────────────────────────


def test_done_without_evidence_is_flagged(tmp_path: Path) -> None:
    """标了 done 必须有能查证的证据。"""
    table = _write(tmp_path, "1,E-01,工程,R1,标了但空,工程,done,,说明,—\n")
    problems = task_table.check(tmp_path, table=table)
    assert [p.rule for p in problems] == ["标记无证据"]


def test_done_with_evidence_is_fine(tmp_path: Path) -> None:
    (tmp_path / "kb").mkdir()
    (tmp_path / "kb" / "ok.py").write_text("x = 1\n", encoding="utf-8")
    table = _write(tmp_path, "1,E-01,工程,R1,做完了,工程,done,,说明,kb/ok.py\n")
    assert task_table.check(tmp_path, table=table) == []


# ── 判据 4：改了代码没动表 ─────────────────────────────────────


def test_code_change_without_table_update_is_flagged() -> None:
    """这条是提交前钩子的拦截点。

    2026-11-28 的事故就是这条缺失：代码提交了，表没动。
    """
    problems = task_table.check_touched(
        Path("."), ["kb/search/service.py", "kb/api/search.py"]
    )
    assert [p.rule for p in problems] == ["改了代码没更新任务表"]
    assert "kb/search/service.py" in problems[0].message


def test_code_change_with_table_update_is_fine() -> None:
    """同一提交里带上表格，就不拦。"""
    problems = task_table.check_touched(
        Path("."), ["kb/search/service.py", task_table.TABLE_NAME]
    )
    assert problems == []


def test_doc_only_change_does_not_require_table() -> None:
    """只改文档不要求动表格——否则每个文档提交都被拦，很快会被绕过。"""
    problems = task_table.check_touched(Path("."), ["docs/tech/方案.md", "README.md"])
    assert problems == []


def test_chinese_table_name_matches_after_git_escaping() -> None:
    """中文文件名不该因为 git 转义而匹配失败。

    真实事故：任务表文件名是中文，`git diff --name-only` 输出八进制转义
    `"\\344\\273\\277..."`，于是「改了表格」永远匹配不上，钩子把每次
    带表格的提交都拒了——它拦住了自己的提交。

    这条守的是 `staged_paths()` 用 `-z` 拿原始字节。
    """
    from tools.eng_checks import git_rules

    root = Path(__file__).resolve().parents[1]
    paths = git_rules.staged_paths.__doc__ or ""
    # 静态断言：实现必须用 -z，否则中文路径会被转义
    assert "-z" in paths, "staged_paths 必须用 -z 输出原始字节，否则中文路径匹配失败"

    # 行为断言：中文文件名经过 check_touched 能被正确识别
    problems = task_table.check_touched(
        root, ["kb/search/config.py", task_table.TABLE_NAME]
    )
    assert problems == [], "中文表格名没被识别为「已更新表格」"


# ── 缺表 / 空表 ────────────────────────────────────────────────


def test_missing_table_is_flagged(tmp_path: Path) -> None:
    problems = task_table.check(tmp_path)
    assert [p.rule for p in problems] == ["缺表"]


# ── next_task：按顺序的「下一步」 ──────────────────────────────


def test_next_task_returns_earliest_todo(tmp_path: Path) -> None:
    """按 `顺序` 升序返回第一个 todo。"""
    table = _write(
        tmp_path,
        "1,E-01,工程,R1,先做这个,工程,todo,,,—\n2,E-02,工程,R1,后做,工程,todo,,,—\n",
    )
    rows = task_table.next_task(tmp_path, table=table)
    assert rows[0].code == "E-01"


def test_next_task_skips_done(tmp_path: Path) -> None:
    table = _write(
        tmp_path,
        "1,E-01,工程,R1,已完成,工程,done,,,kb/x.py\n"
        "2,E-02,工程,R1,该做这个,工程,todo,,,—\n",
    )
    rows = task_table.next_task(tmp_path, table=table)
    assert [r.code for r in rows] == ["E-02"]


def test_next_task_respects_unfinished_dependency(tmp_path: Path) -> None:
    """依赖未完成时跳过——这是「按顺序」真正该起作用的地方。

    注意这不是「历史审计」：历史顺序改不了，但下一次做哪个可以查询。
    """
    table = _write(
        tmp_path,
        "1,E-01,工程,R1,前置,工程,todo,,,—\n"
        "2,E-02,工程,R1,依赖前置,工程,todo,E-01,,\n"
        "3,E-03,工程,R1,无依赖,工程,todo,,,—\n",
    )
    codes = [r.code for r in task_table.next_task(tmp_path, table=table)]
    assert "E-02" not in codes, "E-02 依赖 E-01，E-01 还没做，不该进队列"
    assert "E-01" in codes


def test_next_task_excludes_explicitly_out_of_scope(tmp_path: Path) -> None:
    """说明列写「明确不做」的，不进待办。"""
    table = _write(
        tmp_path,
        "1,M6-01,需求,R6,不做的事,工程,todo,,AGENTS.md 明确不做,\n"
        "2,M6-02,需求,R6,要做的事,工程,todo,,,—\n",
    )
    codes = [r.code for r in task_table.next_task(tmp_path, table=table)]
    assert codes == ["M6-02"]


def test_next_task_skips_blocked_task(tmp_path: Path) -> None:
    """被外部条件阻塞（⏸）的任务跳过，但优先级不降。

    真实场景：M3-05（P0 图片 OCR）卡在「没有视觉模型」，但顺序排第一。
    不跳过的话 `--next-task` 每次都推荐它，整个施工停在那里。
    """
    table = _write(
        tmp_path,
        "1,M3-05,需求,R5,被阻塞的P0,P0,todo,,⏸ 等待视觉模型配置,-\n"
        "2,M3-08,需求,R5,能做的P0,P0,todo,,,—\n",
    )
    codes = [r.code for r in task_table.next_task(tmp_path, table=table)]
    assert codes == ["M3-08"], "被阻塞的 M3-05 应跳过"

    # 但它仍在表里、仍是第一位、标记还在——阻塞不等于降级
    blocked = next(r for r in task_table.load(table) if r.code == "M3-05")
    assert blocked.order == 1
    assert "⏸" in blocked.note


def test_blocked_marker_is_not_confused_with_normal_note(tmp_path: Path) -> None:
    """普通说明文字不该被当成阻塞标记。"""
    table = _write(
        tmp_path,
        "1,E-01,工程,R1,正常任务,工程,todo,,等待外部确认后开始,—\n",
    )
    codes = [r.code for r in task_table.next_task(tmp_path, table=table)]
    assert codes == ["E-01"], "没有 ⏸ 标记的不该被跳过"


# ── 真实表格 ──────────────────────────────────────────────────


def test_real_table_is_consistent() -> None:
    """仓库里的真表格必须自洽。

    这条是「交付前自检」的一部分：表格与代码事实不一致时红。
    """
    root = Path(__file__).resolve().parents[1]
    problems = task_table.check(root)
    assert problems == [], "\n".join(str(p) for p in problems)

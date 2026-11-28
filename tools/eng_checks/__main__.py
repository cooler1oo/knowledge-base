"""命令行入口。

用途与调用方：
  ① 提交前钩子  → `--commit-msg <文件>`（提交信息） + `--staged`（提交大小）
  ① 推送前钩子  → `--pre-push`（工作区必须干净）
  ② ③ CI 和本地 → `--arch`（架构边界）
  交付前自检    → `--all`（架构 + 干净 + 已推送）
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import arch, git_rules, task_table

ROOT = Path(__file__).resolve().parents[2]


def _report(problems: Sequence[object], scope: str) -> int:
    if not problems:
        print(f"[eng-checks] {scope}：通过")
        return 0
    for item in problems:
        print(item)
    print(f"[eng-checks] {scope}：{len(problems)} 处不合规")
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.eng_checks")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--arch", action="store_true", help="架构边界（CI 用）")
    g.add_argument(
        "--commit-msg", metavar="FILE", help="校验提交信息（① commit-msg 钩子）"
    )
    g.add_argument("--staged", action="store_true", help="校验提交大小（① 钩子）")
    g.add_argument("--pre-push", action="store_true", help="推送前工作区必须干净")
    g.add_argument(
        "--all", action="store_true", help="交付前自检：架构 + 干净 + 已推送"
    )
    g.add_argument(
        "--task-table",
        action="store_true",
        help="任务总表与代码事实一致性（② ③ + 提交前钩子）",
    )
    g.add_argument(
        "--next-task",
        action="store_true",
        help="按工程顺序列出接下来该做的 3 项（开工前必查）",
    )
    args = ap.parse_args(argv)

    if args.arch:
        return _report(arch.scan(ROOT), "架构边界")

    if args.commit_msg:
        text = Path(args.commit_msg).read_text(encoding="utf-8")
        return _report(git_rules.check_commit_message(text), "提交信息")

    if args.staged:
        files, lines = git_rules.staged_stats()
        staged_problems: list[object] = list(git_rules.check_commit_size(files, lines))
        # 改了 kb/ 或 tools/ 却没动任务表 → 拦。
        # 这条比 --task-table 更早生效：代码进 commit 的那一刻就查，
        # 不用等下次跑 --all。2026-11-28 的事故就是把这条漏在了最后。
        staged_problems += task_table.check_touched(ROOT, git_rules.staged_paths())
        return _report(staged_problems, f"提交大小（{files} 文件 / {lines} 行）")

    if args.pre_push:
        return _report(
            git_rules.check_worktree_clean() + git_rules.check_git_proxy(),
            "推送前工作区",
        )

    if args.task_table:
        return _report(task_table.check(ROOT), "任务总表一致性")

    if args.next_task:
        rows = task_table.next_task(ROOT)
        blocked = [
            r
            for r in task_table.load(ROOT / task_table.TABLE_NAME)
            if r.state == task_table.STATE_TODO and "⏸" in r.note
        ]
        if blocked:
            # 被外部条件阻塞的任务必须显式列出。静默跳过会让读的人以为
            # 它已经做完或被删了——2026-11-28 的 M3-05 就属于这种情况。
            print("[eng-checks] 以下任务被外部条件阻塞，已跳过（优先级不降）：")
            for row in blocked:
                print(f"  {row.code:8} {row.task}")
                print(f"           {row.note}")
            print()
        if not rows:
            print("[eng-checks] 任务总表：没有可做的 todo（或全部被依赖阻塞）")
            return 0
        print("[eng-checks] 按工程顺序，接下来该做：")
        for index, row in enumerate(rows, start=1):
            mark = "← 建议先做这个" if index == 1 else ""
            print(f"  {row.code:8} {row.task}  {mark}")
        return 0

    # --all：任何一项红就算没做完
    problems: list[object] = []
    problems += arch.scan(ROOT)
    problems += task_table.check(ROOT)
    problems += git_rules.check_worktree_clean()
    problems += git_rules.check_git_proxy()
    problems += git_rules.check_pushed()
    try:
        tree = git_rules.working_tree_hash(ROOT)
    except RuntimeError as exc:
        problems.append(git_rules.Problem("取不到内容哈希", str(exc)))
    else:
        problems += git_rules.check_last_green(ROOT / git_rules.MARKER_REL, tree)
        # pg 覆盖只作提示：本机无 Postgres，全跳过不是错误，但「这条绿灯不含
        # 真库验证」这件事必须说出来，否则读的人会以为 passed 就是验过了。
        print(f"[eng-checks] {git_rules.pg_coverage_note(ROOT / git_rules.MARKER_REL)}")
    return _report(problems, "交付前自检")


if __name__ == "__main__":
    sys.exit(main())

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

from . import arch, git_rules

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
    args = ap.parse_args(argv)

    if args.arch:
        return _report(arch.scan(ROOT), "架构边界")

    if args.commit_msg:
        text = Path(args.commit_msg).read_text(encoding="utf-8")
        return _report(git_rules.check_commit_message(text), "提交信息")

    if args.staged:
        files, lines = git_rules.staged_stats()
        return _report(
            git_rules.check_commit_size(files, lines),
            f"提交大小（{files} 文件 / {lines} 行）",
        )

    if args.pre_push:
        return _report(git_rules.check_worktree_clean(), "推送前工作区")

    # --all：任何一项红就算没做完
    problems: list[object] = []
    problems += arch.scan(ROOT)
    problems += git_rules.check_worktree_clean()
    problems += git_rules.check_pushed()
    try:
        tree = git_rules.working_tree_hash(ROOT)
    except RuntimeError as exc:
        problems.append(git_rules.Problem("取不到内容哈希", str(exc)))
    else:
        problems += git_rules.check_last_green(ROOT / git_rules.MARKER_REL, tree)
    return _report(problems, "交付前自检")


if __name__ == "__main__":
    sys.exit(main())

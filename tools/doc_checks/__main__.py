"""命令行入口：① 用 --staged，② 用 --changed <base>，③ 用 --all。"""

from __future__ import annotations

import argparse
import sys

from . import Violation, changed_files, docs_files, run_checks, staged_files


def _report(violations: list[Violation], scope: str) -> int:
    if not violations:
        print(f"[doc-checks] {scope}：通过")
        return 0
    for v in violations:
        print(v)
    print(f"[doc-checks] {scope}：{len(violations)} 处不合规")
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.doc_checks")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="检查全部文档（③ 用）")
    g.add_argument("--staged", action="store_true", help="只查已暂存的文档（① 用）")
    g.add_argument("--changed", metavar="BASE", help="只查相对 BASE 改动的文档（② 用）")
    ap.add_argument("--cross-refs", action="store_true", help="额外检查文档内路径引用")
    args = ap.parse_args(argv)

    if args.staged:
        files = staged_files()
        if not files:
            print("[doc-checks] 暂存区没有文档改动：跳过")
            return 0
        return _report(run_checks(files), f"暂存区 {len(files)} 个文档")

    if args.changed:
        files = changed_files(args.changed)
        if not files:
            print(f"[doc-checks] 相对 {args.changed} 没有文档改动：跳过")
            return 0
        return _report(run_checks(files), f"改动 {len(files)} 个文档")

    files = docs_files()
    # 不带参数等同于 --all：全量文档 + P0 覆盖
    return _report(
        run_checks(None, cross_refs=args.cross_refs),
        f"全量 {len(files)} 个文档",
    )


if __name__ == "__main__":
    sys.exit(main())

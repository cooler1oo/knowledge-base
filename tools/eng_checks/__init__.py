"""工程约束的机器检查：架构边界 + 提交纪律。

对应原 AGENTS.md §八 表格里「架构约束」与 §七 的提交规则。
规则的具体口径见各模块顶部注释，不在文档里复述。
"""

from __future__ import annotations

from .arch import Violation, scan
from .git_rules import Problem, check_commit_message, check_commit_size

__all__ = [
    "Violation",
    "Problem",
    "scan",
    "check_commit_message",
    "check_commit_size",
]

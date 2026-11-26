"""③ 全量：所有文档都得过规则检查。PR 上也会跑这条（只查改动文件）。"""

from tools.doc_checks import run_checks


def test_docs_follow_project_rules() -> None:
    violations = run_checks()
    assert not violations, "文档不合规：\n" + "\n".join(str(v) for v in violations)

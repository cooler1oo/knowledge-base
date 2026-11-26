"""慢检查：文档里互相引用的路径是否真实存在。

标了 slow 是为了演示 ② 与 ③ 的差别：它要扫全库、解析所有引用，
改动单个文档时不值得跑，留给合进 main 之后。
"""

import pytest

from tools.doc_checks import check_referenced_paths, docs_files

pytestmark = pytest.mark.slow


def test_referenced_doc_paths_exist() -> None:
    violations = check_referenced_paths(docs_files())
    assert not violations, "引用失效：\n" + "\n".join(str(v) for v in violations)

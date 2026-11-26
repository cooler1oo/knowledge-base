"""慢检查：技术方案里写的星数与许可，是否与 GitHub 实际一致。

标 slow 的原因：要联网请求 GitHub，改动单个文档时不值得跑，留给合进 main 之后。
断网时跳过而不是失败——离线开发不该被这条拦住。
"""

import pytest

from tools.doc_checks import docs_files
from tools.doc_checks.reuse import mode_for, verify_remote

pytestmark = pytest.mark.slow


def test_reuse_table_matches_github() -> None:
    violations = []
    checked = 0
    for p in docs_files():
        mode = mode_for(p)
        if mode != "strict":
            continue
        text = p.read_text(encoding="utf-8")
        hits = verify_remote(text, p.as_posix(), mode)
        if hits:
            # 全是网络错误说明是断网，不算文档问题
            if all("TypeError" in str(h) or "URLError" in str(h) for h in hits):
                pytest.skip("无法访问 GitHub，跳过联网核对")
            violations.extend(hits)
        checked += 1

    if checked == 0:
        pytest.skip("没有需要联网核对的文档")
    assert not violations, "复用调研与 GitHub 实际不符：\n" + "\n".join(
        str(v) for v in violations
    )

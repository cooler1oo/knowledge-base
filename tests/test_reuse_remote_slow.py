"""慢检查：技术方案里写的星数与许可，是否与 GitHub 实际一致。

标 slow 的原因：要联网请求 GitHub，改动单个文档时不值得跑，留给合进 main 之后。

两类「不通过」要分开：
- 文档写错了（星数虚报、许可不符）→ 失败，必须改文档
- 环境不可用（限流、断网、5xx）→ 跳过，不阻塞离线开发

匿名调用 GitHub 只有 60 次/小时，配 GITHUB_TOKEN 可提到 5000。
"""

import pytest

from tools.doc_checks import docs_files
from tools.doc_checks.reuse import RemoteUnavailable, mode_for, verify_remote

pytestmark = pytest.mark.slow


def test_reuse_table_matches_github() -> None:
    violations = []
    checked = 0
    for p in docs_files():
        mode = mode_for(p)
        if mode != "strict":
            continue
        text = p.read_text(encoding="utf-8")
        try:
            hits = verify_remote(text, p.as_posix(), mode)
        except RemoteUnavailable as exc:
            pytest.skip(str(exc))
        violations.extend(hits)
        checked += 1

    if checked == 0:
        pytest.skip("没有需要联网核对的文档")
    assert not violations, "复用调研与 GitHub 实际不符：\n" + "\n".join(
        str(v) for v in violations
    )

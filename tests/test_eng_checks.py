"""工程检查自身的测试：架构边界与提交纪律。

这些用例是「规则的规格说明」——改规则先改这里。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tools.eng_checks import arch, git_rules

GOOD_MESSAGE = """feat(store): 加一个查条目的函数

做了什么：补 get_item，供 api 层调用。
为什么：之前只能在列表里找，详情端点没有对应查询。

验证：python -m pytest -q → 87 passed
"""


# ── 架构边界 ──────────────────────────────────────────────────


def test_store_may_contain_write_sql():
    source = 'def create(conn):\n    return conn.run("insert into items (title) values (:t)")\n'
    assert arch.check_file("kb/store/items.py", source) == []


def test_non_store_must_not_contain_write_sql():
    source = 'def bad(conn):\n    conn.run("delete from items where id = 1")\n'
    problems = arch.check_file("kb/api/app.py", source)
    assert [p.rule for p in problems] == ["只有 store 能写库"]
    assert problems[0].line == 2


def test_bare_keyword_in_message_is_not_flagged():
    """错误提示里写到 DELETE 不算写库，避免误伤正常人话。"""
    source = 'MSG = "该操作只允许读（DELETE 会被拒绝）"\n'
    assert arch.check_file("kb/api/app.py", source) == []


def test_extract_layer_must_not_import_store():
    source = "from kb.store.items import get_item\n"
    problems = arch.check_file("kb/extract/douyin.py", source)
    assert [p.rule for p in problems] == ["抓取层不依赖 store"]


def test_ai_layer_must_not_import_store():
    source = "import kb.store\n"
    problems = arch.check_file("kb/ai/summarize.py", source)
    assert [p.rule for p in problems] == ["AI 层不依赖 store"]


def test_extract_may_import_other_layers():
    source = "from kb.api.auth import hash_token\n"
    assert arch.check_file("kb/extract/douyin.py", source) == []


def test_syntax_error_is_reported_with_location():
    problems = arch.check_file("kb/api/broken.py", "def f(:\n")
    assert problems and problems[0].rule == "语法"


# ── 提交信息 ──────────────────────────────────────────────────


def test_good_message_passes():
    assert git_rules.check_commit_message(GOOD_MESSAGE) == []


@pytest.mark.parametrize(
    "subject",
    [
        "改了点东西",  # 没有 type
        "feature: 加了功能",  # type 不在白名单
        "feat:加冒号后没空格",
        "feat: ",  # 描述为空
    ],
)
def test_bad_subject_is_rejected(subject):
    message = subject + "\n\n做了什么：x\n\n验证：跑了测试\n"
    problems = git_rules.check_commit_message(message)
    assert any(p.rule == "主题格式" for p in problems)


def test_subject_too_long_is_rejected():
    message = "feat: " + "很长的描述" * 20 + "\n\n做了什么：x\n\n验证：y\n"
    problems = git_rules.check_commit_message(message)
    assert any(p.rule == "主题过长" for p in problems)


def test_message_without_body_is_rejected():
    problems = git_rules.check_commit_message("fix: 修个 bug\n\n验证：pytest\n")
    assert any(p.rule == "缺正文" for p in problems)


def test_message_without_verification_is_rejected():
    problems = git_rules.check_commit_message("fix: 修个 bug\n\n做了什么：改了逻辑\n")
    assert any(p.rule == "缺验证" for p in problems)


def test_empty_message_is_rejected():
    assert git_rules.check_commit_message("   \n\n")[0].rule == "提交信息为空"


# ── 提交大小 ──────────────────────────────────────────────────


def test_oversized_commit_is_rejected():
    problems = git_rules.check_commit_size(git_rules.MAX_FILES + 1, 10)
    assert [p.rule for p in problems] == ["提交过大"]


def test_large_lines_are_rejected():
    problems = git_rules.check_commit_size(2, git_rules.MAX_LINES + 1)
    assert [p.rule for p in problems] == ["提交过大"]


def test_size_limit_can_be_explicitly_bypassed(monkeypatch):
    monkeypatch.setenv(git_rules.BYPASS_ENV, "1")
    assert git_rules.check_commit_size(999, 99999) == []


def test_normal_commit_size_passes():
    assert git_rules.check_commit_size(3, 120) == []


# ── 架构规则的声明式配置 ──────────────────────────────────────


def test_rules_can_allow_write_sql_for_another_layer():
    """规则是配置：把允许前缀改成 kb/api/ 后，api 里写库就不再违规。"""
    rules = {
        "write_sql_allowed_prefixes": ["kb/api/"],
        "import_deny": [],
    }
    source = 'conn.run("delete from items where id = 1")\n'
    assert arch.check_file("kb/api/app.py", source, rules) == []


def test_tighter_rules_still_flag_original_targets():
    rules = {"write_sql_allowed_prefixes": ["kb/api/"], "import_deny": []}
    source = 'conn.run("delete from items where id = 1")\n'
    assert [v.rule for v in arch.check_file("kb/store/items.py", source, rules)] == [
        "只有 store 能写库"
    ]


def test_load_rules_reads_toml(tmp_path):
    cfg = tmp_path / "layer_rules.toml"
    cfg.write_text(
        'dirs = ["kb"]\n'
        'write_sql_allowed_prefixes = ["kb/db/"]\n'
        "[[import_deny]]\n"
        'path_prefix = "kb/web/"\n'
        'layer = "Web 层"\n'
        'modules = ["kb.store"]\n',
        encoding="utf-8",
    )
    rules = arch.load_rules(cfg)
    assert rules["write_sql_allowed_prefixes"] == ["kb/db/"]
    assert rules["import_deny"][0]["layer"] == "Web 层"


def test_load_rules_falls_back_when_missing(tmp_path):
    assert arch.load_rules(tmp_path / "nope.toml") == arch.DEFAULT_RULES


# ── 「测试跑过」的留痕 ────────────────────────────────────────


def test_missing_marker_blocks_delivery(tmp_path):
    problems = git_rules.check_last_green(tmp_path / "marker.json", "abc")
    assert [p.rule for p in problems] == ["没跑过测试"]


def test_stale_marker_blocks_delivery(tmp_path):
    marker = tmp_path / "marker.json"
    git_rules.write_last_green(marker, "old-tree", datetime.now(timezone.utc))
    problems = git_rules.check_last_green(marker, "new-tree")
    assert [p.rule for p in problems] == ["测试不是这份内容跑的"]


def test_matching_marker_passes(tmp_path):
    marker = tmp_path / "marker.json"
    git_rules.write_last_green(marker, "same-tree", datetime.now(timezone.utc))
    assert git_rules.check_last_green(marker, "same-tree") == []


def test_corrupt_marker_is_treated_as_missing(tmp_path):
    marker = tmp_path / "marker.json"
    marker.write_text("{不是 json", encoding="utf-8")
    problems = git_rules.check_last_green(marker, "abc")
    assert [p.rule for p in problems] == ["没跑过测试"]

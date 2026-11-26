"""复用调研检查的测试。

每个用例对应一条真实会发生的错误，不是凑数：
自写不给理由、理由敷衍、用了 AGPL 却不提、小众项目不标风险、
结论写错枚举、星数写成「很多」。
"""

from tools.doc_checks.reuse import audit_text, parse_audit

HEADER = """## 复用调研

| 组件 | 候选 | 星数 | 许可 | 结论 | 理由 |
|---|---|---|---|---|---|
"""


def _doc(*rows: str) -> str:
    return HEADER + "\n".join(rows) + "\n"


def _rules(text: str) -> list[str]:
    return [v.rule for v in audit_text(text, "docs/tech/x.md")]


def _messages(text: str) -> str:
    return "\n".join(str(v) for v in audit_text(text, "docs/tech/x.md"))


# ── 结构 ──────────────────────────────────────────────


def test_missing_section_is_reported() -> None:
    text = "# 方案\n\n没有复用调研章节。\n"
    assert "复用调研" in _rules(text)


def test_empty_section_is_reported() -> None:
    text = "## 复用调研\n\n只有一句话，没有表格。\n"
    assert "复用调研" in _rules(text)


def test_valid_table_passes() -> None:
    text = _doc(
        "| 抓取 | ParseHub | 206 | MIT | 复用 | 覆盖抖音图文视频，省 120 行自写 |",
    )
    assert audit_text(text, "x.md") == []


# ── 结论枚举 ───────────────────────────────────────────


def test_unknown_conclusion_is_rejected() -> None:
    text = _doc(
        "| 抓取 | ParseHub | 206 | MIT | 自己搞 | 想试试 |",
    )
    assert any("不在" in str(v) for v in audit_text(text, "x.md"))


def test_all_self_written_needs_evidence() -> None:
    # 两条都写「自写」，但理由里没有任何检索痕迹 = 没查过就自己写
    text = _doc(
        "| A | 无候选 | 0 | MIT | 自写 | 这个功能比较特殊需要定制处理 |",
        "| B | 无候选 | 0 | MIT | 自写 | 需求独特没有现成方案可直接使用 |",
    )
    msgs = _messages(text)
    assert "全部结论都是「自写」" in msgs


def test_all_self_written_with_evidence_passes() -> None:
    text = _doc(
        "| A | 无候选 | 0 | MIT | 自写 | 检索过 douyin scraper 均不可用 |",
        "| B | 无候选 | 0 | MIT | 自写 | 检索过 meilisearch 客户端不适用 |",
    )
    assert audit_text(text, "x.md") == []


# ── 自写的理由 ─────────────────────────────────────────


def test_self_written_without_reason_is_rejected() -> None:
    text = _doc("| 类型判定 | 无 | 0 | MIT | 自写 | 短 |")
    assert any("理由过短" in str(v) for v in audit_text(text, "x.md"))


def test_self_written_with_filler_reason_is_rejected() -> None:
    text = _doc("| 类型判定 | 无 | 0 | MIT | 自写 | 这个很简单没必要找库 |")
    assert any("敷衍词" in str(v) for v in audit_text(text, "x.md"))


def test_self_written_with_real_reason_passes() -> None:
    text = _doc(
        "| 类型判定 | karakeep 无此枚举 | 30860 | AGPL-3.0 | 自写 | "
        "上游 4 值类型不覆盖 6+1，fork 代价高于重写判定函数 |",
    )
    assert audit_text(text, "x.md") == []


# ── 许可 ─────────────────────────────────────────────


def test_unknown_license_is_rejected() -> None:
    text = _doc("| 抓取 | 某库 | 500 | 自创协议 | 复用 | 用起来方便 |")
    assert any("白名单" in str(v) for v in audit_text(text, "x.md"))


def test_copyleft_requires_mention() -> None:
    text = _doc("| 笔记 | Trilium | 39095 | AGPL-3.0 | 复用 | 功能齐全 |")
    assert any("传染性" in str(v) for v in audit_text(text, "x.md"))


def test_copyleft_mentioned_passes() -> None:
    text = _doc(
        "| 笔记 | Trilium | 39095 | AGPL-3.0 | 复用 | "
        "AGPL-3.0 单人自用不触发分发义务，不对外提供即可 |",
    )
    assert audit_text(text, "x.md") == []


# ── 星数 ─────────────────────────────────────────────


def test_low_stars_without_marker_is_rejected() -> None:
    text = _doc("| 抓取 | 小库 | 12 | MIT | 复用 | 功能正好 |")
    assert any("低于 100" in str(v) for v in audit_text(text, "x.md"))


def test_low_stars_with_marker_passes() -> None:
    text = _doc("| 抓取 | 小库 | 12 | MIT | 复用 | 功能正好，⚠ 停更风险需自留分支 |")
    assert audit_text(text, "x.md") == []


def test_non_numeric_stars_is_rejected() -> None:
    text = _doc("| 抓取 | 某库 | 很多 | MIT | 复用 | 功能齐全好用 |")
    assert any("不是数字" in str(v) for v in audit_text(text, "x.md"))


# ── 解析 ─────────────────────────────────────────────


def test_parse_extracts_rows() -> None:
    text = _doc(
        "| 抓取 | ParseHub | 206 | MIT | 复用 | 覆盖抖音图文视频，省自写 |",
        "| 检索 | Meilisearch | 50000 | MIT | 复用 | 内置中文分词够用 |",
    )
    audit = parse_audit(text)
    assert audit.found_section is True
    assert [r.component for r in audit.rows] == ["抓取", "检索"]
    assert audit.rows[0].conclusion == "复用"


def test_section_stops_at_next_heading() -> None:
    text = (
        "## 复用调研\n\n"
        "| 组件 | 候选 | 星数 | 许可 | 结论 | 理由 |\n"
        "|---|---|---|---|---|---|\n"
        "| 抓取 | ParseHub | 206 | MIT | 复用 | 覆盖抖音图文视频，省自写 |\n"
        "\n## 下一章\n\n"
        "| 无关 | 表 | 格 | 不 | 应 | 解析 |\n"
    )
    audit = parse_audit(text)
    assert len(audit.rows) == 1


def test_fence_in_section_is_ignored() -> None:
    text = (
        "## 复用调研\n\n```\n## 这不是标题\n```\n\n"
        "| 组件 | 候选 | 星数 | 许可 | 结论 | 理由 |\n"
        "|---|---|---|---|---|---|\n"
        "| 抓取 | ParseHub | 206 | MIT | 复用 | 覆盖抖音图文视频，省自写 |\n"
    )
    audit = parse_audit(text)
    assert audit.found_section is True
    assert len(audit.rows) == 1

"""复用调研检查的测试。

每个用例对应一条真实会发生的错误，不是凑数：
自写不给理由、理由敷衍、用了 AGPL 却不提、小众项目不标风险、
结论写错枚举、星数写成「很多」、复用项没写运行环境、藏着付费依赖。

本项目前提是**个人非商用、单机自托管**，所以 GPL/AGPL/BUSL 不拦截，
只要求写清「为什么不触发义务」；真正拦的是「跑不起来」和「要持续付费」。
"""

from tools.doc_checks.reuse import audit_text, parse_audit

HEADER = """## 复用调研

| 组件 | 候选 | 星数 | 许可 | 结论 | 运行环境 | 理由 |
|---|---|---|---|---|---|---|
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
        "| 抓取 | z-mio/ParseHub | 206 | MIT | 复用 | 纯Python | 覆盖抖音图文视频，省 120 行自写 |",
    )
    assert audit_text(text, "x.md") == []


# ── 结论枚举 ───────────────────────────────────────────


def test_unknown_conclusion_is_rejected() -> None:
    text = _doc(
        "| 抓取 | z-mio/ParseHub | 206 | MIT | 自己搞 | - | 想试试 |",
    )
    assert any("不在" in str(v) for v in audit_text(text, "x.md"))


def test_all_self_written_needs_evidence() -> None:
    # 两条都写「自写」，但理由里没有任何检索痕迹 = 没查过就自己写
    text = _doc(
        "| A | 无候选 | 0 | MIT | 自写 | - | 这个功能比较特殊需要定制处理 |",
        "| B | 无候选 | 0 | MIT | 自写 | - | 需求独特没有现成方案可直接使用 |",
    )
    msgs = _messages(text)
    assert "全部结论都是「自写」" in msgs


def test_all_self_written_with_evidence_passes() -> None:
    text = _doc(
        "| A | 无候选 | 0 | MIT | 自写 | - | 检索过 douyin scraper 均不可用 |",
        "| B | 无候选 | 0 | MIT | 自写 | - | 检索过 meilisearch 客户端不适用 |",
    )
    assert audit_text(text, "x.md") == []


# ── 自写的理由 ─────────────────────────────────────────


def test_self_written_without_reason_is_rejected() -> None:
    text = _doc("| 类型判定 | 无 | 0 | MIT | 自写 | - | 短 |")
    assert any("理由过短" in str(v) for v in audit_text(text, "x.md"))


def test_self_written_with_filler_reason_is_rejected() -> None:
    text = _doc("| 类型判定 | 无 | 0 | MIT | 自写 | - | 这个很简单没必要找库 |")
    assert any("敷衍词" in str(v) for v in audit_text(text, "x.md"))


def test_self_written_with_real_reason_passes() -> None:
    text = _doc(
        "| 类型判定 | karakeep 无此枚举 | 30860 | AGPL-3.0 | 自写 | - | "
        "上游 4 值类型不覆盖 6+1，fork 代价高于重写判定函数 |",
    )
    assert audit_text(text, "x.md") == []


# ── 许可 ─────────────────────────────────────────────


def test_unknown_license_is_rejected() -> None:
    text = _doc("| 抓取 | 某库 | 500 | 自创协议 | 复用 | 纯Python | 用起来方便 |")
    assert any("白名单" in str(v) for v in audit_text(text, "x.md"))


def test_copyleft_requires_note_on_self_use() -> None:
    # 个人自用下 AGPL 不拦截，但必须写一句为什么不触发义务
    text = _doc(
        "| 笔记 | TriliumNext/Trilium | 39095 | AGPL-3.0 | 复用 | Docker | 功能齐全 |"
    )
    assert any("个人自用不触发" in str(v) for v in audit_text(text, "x.md"))


def test_copyleft_mentioned_passes() -> None:
    text = _doc(
        "| 笔记 | TriliumNext/Trilium | 39095 | AGPL-3.0 | 复用 | Docker | "
        "AGPL-3.0 个人自用不触发分发义务，不对外提供即可 |",
    )
    assert audit_text(text, "x.md") == []


def test_low_stars_without_marker_is_rejected() -> None:
    text = _doc("| 抓取 | tiny/lib | 12 | MIT | 复用 | 纯Python | 功能正好 |")
    assert any("低于 100" in str(v) for v in audit_text(text, "x.md"))


def test_low_stars_with_marker_passes() -> None:
    text = _doc(
        "| 抓取 | tiny/lib | 12 | MIT | 复用 | 纯Python | 功能正好，⚠ 停更风险需自留分支 |"
    )
    assert audit_text(text, "x.md") == []


def test_non_numeric_stars_is_rejected() -> None:
    text = _doc("| 抓取 | 某库 | 很多 | MIT | 复用 | 纯Python | 功能齐全好用 |")
    assert any("不是数字" in str(v) for v in audit_text(text, "x.md"))


# ── 解析 ─────────────────────────────────────────────


def test_parse_extracts_rows() -> None:
    text = _doc(
        "| 抓取 | z-mio/ParseHub | 206 | MIT | 复用 | 纯Python | 覆盖抖音图文视频，省自写 |",
        "| 检索 | meilisearch/meilisearch | 50000 | MIT | 复用 | 静态二进制 | 内置中文分词够用 |",
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
        "| 抓取 | z-mio/ParseHub | 206 | MIT | 复用 | 覆盖抖音图文视频，省自写 |\n"
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
        "| 抓取 | z-mio/ParseHub | 206 | MIT | 复用 | 覆盖抖音图文视频，省自写 |\n"
    )
    audit = parse_audit(text)
    assert audit.found_section is True
    assert len(audit.rows) == 1


# ── 作用域：设计场景全部纳入 ────────────────────────────


def test_mode_for_tech_is_strict() -> None:
    from tools.doc_checks.reuse import mode_for

    assert mode_for("docs/tech/x.md") == "strict"


def test_mode_for_prd_is_lite() -> None:
    from tools.doc_checks.reuse import mode_for

    assert mode_for("docs/prd/x.md") == "lite"


def test_mode_for_specs_is_lite() -> None:
    from tools.doc_checks.reuse import mode_for

    assert mode_for("docs/specs/x.md") == "lite"


def test_mode_for_research_is_lite() -> None:
    from tools.doc_checks.reuse import mode_for

    assert mode_for("docs/research/x.md") == "lite"


def test_lite_allows_placeholder_stars_and_license() -> None:
    # PRD 调研竞品时不必填星数与许可
    text = _doc(
        "| 双链笔记 | Obsidian | - | - | 不采用 | - | 检索过同类产品，个人库用不到双向链接 |",
    )
    assert audit_text(text, "docs/prd/x.md", "lite") == []


def test_lite_still_requires_conclusion_enum() -> None:
    text = _doc("| 双链笔记 | Obsidian | - | - | 可能要 | - | 看情况再定吧 |")
    assert any("不在" in str(v) for v in audit_text(text, "docs/prd/x.md", "lite"))


def test_lite_still_rejects_filler_reason() -> None:
    text = _doc("| 颜色筛选 | Eagle | - | - | 不采用 | - | 感觉不合适 |")
    assert any("敷衍词" in str(v) for v in audit_text(text, "docs/prd/x.md", "lite"))


def test_strict_rejects_non_repo_candidate() -> None:
    text = _doc(
        "| 抓取 | 某开源库 | 500 | MIT | 复用 | 纯Python | 功能齐全可以直接用起来 |"
    )
    assert any("owner/repo" in str(v) for v in audit_text(text, "x.md", "strict"))


def test_reference_must_say_what_borrowed() -> None:
    text = _doc(
        "| 架构 | karakeep-app/karakeep | 30860 | AGPL-3.0 | 参考 | Docker | AGPL-3.0 自用无义务，整体不套用 |"
    )
    assert any(
        "参考" in str(v) and "没说" in str(v)
        for v in audit_text(text, "x.md", "strict")
    )


def test_reference_with_specifics_passes() -> None:
    text = _doc(
        "| 架构 | karakeep-app/karakeep | 30860 | AGPL-3.0 | 参考 | Docker | AGPL-3.0 自用无义务；取其插件分层，弃其整产品 |"
    )
    assert audit_text(text, "x.md", "strict") == []


# ── 联网核对 ────────────────────────────────────────────


def test_stars_inflation_is_detected(monkeypatch) -> None:
    from tools.doc_checks import reuse

    monkeypatch.setattr(reuse, "fetch_repo", lambda c, timeout=15: (200, "MIT", None))
    text = _doc(
        "| 抓取 | someone/repo | 9999 | MIT | 复用 | 纯Python | 功能齐全直接用 |"
    )
    hits = reuse.verify_remote(text, "x.md", "strict")
    assert any("虚报" in str(v) for v in hits)


def test_stars_growth_is_not_flagged(monkeypatch) -> None:
    from tools.doc_checks import reuse

    # 实际比声称的多，属正常增长，不该报
    monkeypatch.setattr(reuse, "fetch_repo", lambda c, timeout=15: (500, "MIT", None))
    text = _doc(
        "| 抓取 | someone/repo | 200 | MIT | 复用 | 纯Python | 功能齐全直接用 |"
    )
    assert reuse.verify_remote(text, "x.md", "strict") == []


def test_license_mismatch_is_detected(monkeypatch) -> None:
    from tools.doc_checks import reuse

    monkeypatch.setattr(
        reuse, "fetch_repo", lambda c, timeout=15: (500, "AGPL-3.0", None)
    )
    text = _doc(
        "| 抓取 | someone/repo | 500 | MIT | 复用 | 纯Python | 功能齐全直接用 |"
    )
    hits = reuse.verify_remote(text, "x.md", "strict")
    assert any("许可写" in str(v) for v in hits)


# ── 豁免 ───────────────────────────────────────────────


def test_exempt_mark_allows_missing_section() -> None:
    text = "# 历史文档\n\n<!-- reuse-audit: exempt -->\n<!-- 写于规则上线前 -->\n\n正文。\n"
    assert audit_text(text, "docs/prd/x.md", "lite") == []


def test_exempt_mark_without_reason_still_needs_mark() -> None:
    # 没有标记就照常报错，不能默默跳过
    text = "# 历史文档\n\n正文。\n"
    assert "复用调研" in _rules(text)


def test_exempt_does_not_bypass_broken_table() -> None:
    # 有章节但内容有问题时，豁免不生效（豁免只针对「缺章节」）
    text = "<!-- reuse-audit: exempt -->\n" + _doc(
        "| 类型判定 | 无 | 0 | MIT | 自写 | - | 简单没必要找库 |"
    )
    assert any("敷衍词" in str(v) for v in audit_text(text, "x.md", "strict"))


# ── 运行环境（本项目最常被否决的原因） ──────────────────


def test_reuse_requires_runtime() -> None:
    # 只写「复用」不写怎么跑，等于没判断可行性
    text = _doc("| 抓取 | z-mio/ParseHub | 206 | MIT | 复用 | - | 功能齐全够用 |")
    assert any("必须填运行环境" in str(v) for v in audit_text(text, "x.md"))


def test_reuse_runtime_must_be_recognizable() -> None:
    text = _doc("| 抓取 | z-mio/ParseHub | 206 | MIT | 复用 | 可以跑 | 功能齐全够用 |")
    assert any("看不出运行方式" in str(v) for v in audit_text(text, "x.md"))


def test_reuse_with_clear_runtime_passes() -> None:
    text = _doc(
        "| 抓取 | z-mio/ParseHub | 206 | MIT | 复用 | 纯Python | 一个调用拿到视频时长与图文原图 |"
    )
    assert audit_text(text, "x.md") == []


def test_self_written_does_not_require_runtime() -> None:
    # 「自写」本来就跑在自己的代码里，无需填运行环境
    text = _doc(
        "| 类型判定 | 无可复用项 | 0 | MIT | 自写 | - | 检索过 karakeep 枚举不含此逻辑 |"
    )
    assert audit_text(text, "x.md") == []


# ── 付费依赖（个人使用不接受固定开销） ──────────────────


def test_paid_dependency_is_flagged() -> None:
    text = _doc(
        "| 语音转写 | openai/whisper-api | 70000 | MIT | 复用 | 云托管 API 计费 | "
        "转写质量高，按量计费 |"
    )
    assert any("付费" in str(v) for v in audit_text(text, "x.md"))


def test_paid_with_amount_passes() -> None:
    text = _doc(
        "| 语音转写 | openai/whisper-api | 70000 | MIT | 复用 | 云托管 API 计费 | "
        "按量计费，实测每条视频约 0.02 元，月 5 元可接受 |"
    )
    assert audit_text(text, "x.md") == []


def test_free_alternative_not_flagged() -> None:
    text = _doc(
        "| 语音转写 | openai/whisper | 70000 | MIT | 参考 | 纯Python | "
        "取其本地推理思路，免费无付费；不采用云 API 版本 |"
    )
    assert audit_text(text, "x.md") == []


# ── 六列旧格式兼容 ─────────────────────────────────────


def test_legacy_six_column_still_parsed() -> None:
    # 旧文档用六列，不能静默解析成零行（那等于绕过检查）
    text = (
        "## 复用调研\n\n"
        "| 组件 | 候选 | 星数 | 许可 | 结论 | 理由 |\n"
        "|---|---|---|---|---|---|\n"
        "| 抓取 | z-mio/ParseHub | 206 | MIT | 复用 | 功能齐全够用无需额外配置 |\n"
    )
    audit = parse_audit(text)
    assert len(audit.rows) == 1
    assert audit.rows[0].runtime == ""
    # 缺运行环境列，strict 模式下应报错
    assert any("必须填运行环境" in str(v) for v in audit_text(text, "x.md"))


# ── 联网核对的故障区分 ─────────────────────────────────


def test_transient_error_raises_remote_unavailable(monkeypatch) -> None:
    # 限流不该被当成「文档写错」，否则 CI 天天红
    from tools.doc_checks import reuse

    monkeypatch.setattr(
        reuse, "fetch_repo", lambda c, timeout=15: (None, "", "HTTP 403")
    )
    text = _doc(
        "| 抓取 | z-mio/ParseHub | 206 | MIT | 复用 | 纯Python | 功能齐全可直接用 |"
    )
    try:
        reuse.verify_remote(text, "x.md", "strict")
    except reuse.RemoteUnavailable as exc:
        assert "GITHUB_TOKEN" in str(exc)
    else:
        raise AssertionError("限流时应抛 RemoteUnavailable")


def test_transient_classifier() -> None:
    from tools.doc_checks.reuse import is_transient

    assert is_transient("HTTP 403") is True
    assert is_transient("HTTP 429") is True
    assert is_transient("URLError") is True
    assert is_transient("TimeoutError") is True
    # 404 是真的不存在，属于文档问题
    assert is_transient("HTTP 404") is False
    assert is_transient(None) is False

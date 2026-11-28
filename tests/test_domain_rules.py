"""`domain_rules` 的规则读写（M3-08 T01）。

用**假连接**验 SQL 与分支，不需要真库；真库行为另外在
`tests/test_qa_search_pg.py` 与 `tests/test_domain_rules_pg.py` 覆盖。

## 为什么这层值得一堆假连接用例

四个函数里只有三个分支，但它们守着**两个会静默出错的点**：

1. `get_kind_for` 查不到时返回 `None` 而不是抛异常 —— 抛了会让每个调用点
   写 try/except，那种代码很快会演变成 `except: pass`
2. `remember` 先校验 kind 在 `item_kinds` 里 —— 不校验的话非法 kind 会被
   外键拒绝成 500，而真实原因是「用户传了个不存在的类型」，该是 400

第 2 条尤其值得用例守着：外键报错在生产上是 500、在测试里是 IntegrityError，
两个都不是「400 + 可选值列表」，只能靠用例钉住正确行为。
"""

from __future__ import annotations

import pytest

from kb.store import domain_rules

#: 假 `item_kinds` 表的内容。**故意不含 "podcast"**，用来验「不硬编码枚举」：
#: 加一类只需往这个列表里加一项，代码不用动。
_KINDS = ["article", "image", "image_text", "link", "project", "text", "video"]


class FakeConn:
    """按 SQL 特征分派的假连接。

    `rules` 是规则表的当前内容（`domain -> kind`），用例直接读写它，
    这样「查不到」「已存在要 upsert」都能一眼摆出来。
    """

    def __init__(self, rules: dict[str, str] | None = None) -> None:
        self.rules: dict[str, str] = dict(rules or {})
        #: 记录执行过的 (SQL 归一化后, 参数)，用于断言「没查库」这类行为
        self.seen: list[tuple[str, dict[str, object]]] = []

    def run(self, sql: str, **params: object) -> list[list[object]]:
        low = " ".join(sql.split()).lower()
        self.seen.append((low, params))

        if "from item_kinds" in low:
            return [[k] for k in _KINDS]

        if "select kind from domain_type_rules" in low:
            domain = str(params.get("d") or "")
            kind = self.rules.get(domain)
            return [] if kind is None else [[kind]]

        if "insert into domain_type_rules" in low:
            self.rules[str(params["d"])] = str(params["k"])
            return []

        if "delete from domain_type_rules" in low:
            domain = str(params.get("d") or "")
            if domain in self.rules:
                del self.rules[domain]
                return [[domain]]
            return []

        if "select domain, kind from domain_type_rules" in low:
            return [[d, k] for d, k in sorted(self.rules.items())]

        return []


# ── get_kind_for ────────────────────────────────────────────


def test_没有规则时返回None而不是抛异常() -> None:
    """「没有规则」是最常见的正常结果。

    抛异常会逼每个调用点写 try/except，而那些代码很快就会有人写成
    `except Exception: pass`，把真正的数据库故障一起吞掉。
    """
    conn = FakeConn()
    assert domain_rules.get_kind_for(conn, "douyin.com") is None


def test_有规则时返回kind() -> None:
    conn = FakeConn({"douyin.com": "article"})
    assert domain_rules.get_kind_for(conn, "douyin.com") == "article"


def test_空域名不查库() -> None:
    """`domain_of` 对非法 URL 返回 `None`，调用方会转成空串传进来。

    省掉那次往返不只是一点性能：假连接用例里能断言「一次查询都没发」，
    这比「返回了 None」更强——它证明实现走的是早退而不是「查了发现没有」。
    """
    conn = FakeConn()
    assert domain_rules.get_kind_for(conn, "") is None
    assert conn.seen == [], f"空域名不该查库，实际发了 {len(conn.seen)} 条语句"


# ── remember ────────────────────────────────────────────────


def test_remember写入新规则() -> None:
    conn = FakeConn()
    domain_rules.remember(conn, "douyin.com", "article")
    assert conn.rules == {"douyin.com": "article"}


def test_remember已存在时更新而不是报错() -> None:
    """upsert 语义：用户改了两次类型，表里只该有一行，且是最后一次的值。

    用 `on conflict (domain) do update set kind = excluded.kind`。
    写成裸 `insert` 的话第二次会撞主键报 `23505`，表现是「改类型第二次总是失败」。
    """
    conn = FakeConn({"douyin.com": "video"})
    domain_rules.remember(conn, "douyin.com", "article")
    assert conn.rules == {"douyin.com": "article"}


def test_remember拒绝不在item_kinds里的kind() -> None:
    """必须是 `UnknownKindError`（`ValueError` 子类）而不是外键错。

    外键错（SQLSTATE 23503）传到 API 层是 500，而真实原因是「用户传了
    不存在的类型」，该是 400。这条用例钉住「在写库之前就拦住」。
    """
    conn = FakeConn()
    with pytest.raises(domain_rules.UnknownKindError) as excinfo:
        domain_rules.remember(conn, "x.com", "podcast")

    # 消息里要带候选值，否则前端只能显示「400 未知类型」，用户不知道该填什么
    assert "podcast" in str(excinfo.value)
    assert "video" in str(excinfo.value)
    assert conn.rules == {}, "校验失败时不该写进表"


def test_remember的合法类型来自item_kinds表而不是硬编码() -> None:
    """`item_kinds` 加一类之后 `remember` 立刻接受它，代码不用改。

    这条守的是「不硬编码枚举」：把合法 kind 写成模块级常量的话，
    用户往 `item_kinds` 插一行 `podcast` 之后，改类型仍然报
    「未知的类型：podcast（可选：video、article…）」——而它明明就在表里。

    做法是临时往假表里加一项再移除，验的是「实现每次都重新查表」。
    """
    conn = FakeConn()
    assert "podcast" not in _KINDS, "前置条件：podcast 本来不该在表里"

    _KINDS.append("podcast")  # 模拟「用户往 item_kinds 里加了一类」
    try:
        domain_rules.remember(conn, "x.com", "podcast")
        assert conn.rules == {"x.com": "podcast"}
    finally:
        _KINDS.remove("podcast")


def test_remember拒绝空域名() -> None:
    """空串主键会变成「所有解析失败的 URL 共用一条规则」的毒数据。

    之后任何没有域名的链接都会命中那行规则。宁可报错让调用方发现
    自己在没有域名的情况下调了这个函数。
    """
    conn = FakeConn()
    with pytest.raises(ValueError):
        domain_rules.remember(conn, "", "article")
    assert conn.rules == {}


# ── forget ──────────────────────────────────────────────────


def test_forget删掉已有规则返回True() -> None:
    conn = FakeConn({"douyin.com": "article"})
    assert domain_rules.forget(conn, "douyin.com") is True
    assert conn.rules == {}


def test_forget删不存在的规则返回False() -> None:
    """API 层据返回值区分「撤销了一条规则」与「本来就没规则」。

    两种情况都回 200，但文案不同。一律回 True 的话，用户撤销一条
    从未存在的规则会看到「已忘记 douyin.com 的类型规则」——他会以为自己
    刚才记的那条丢过。
    """
    conn = FakeConn()
    assert domain_rules.forget(conn, "douyin.com") is False


def test_forget空域名不查库() -> None:
    conn = FakeConn()
    assert domain_rules.forget(conn, "") is False
    assert conn.seen == []


# ── list_rules ──────────────────────────────────────────────


def test_list_rules按域名升序() -> None:
    """顺序必须确定：不确定顺序的列表在界面上每次刷新都会跳一下。"""
    conn = FakeConn({"zhihu.com": "article", "douyin.com": "video"})
    assert domain_rules.list_rules(conn) == [
        ("douyin.com", "video"),
        ("zhihu.com", "article"),
    ]


def test_list_rules空表返回空列表() -> None:
    assert domain_rules.list_rules(FakeConn()) == []

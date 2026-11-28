"""域名 → 类型修正规则（PRD M3-08 / 技术方案 T1、T2）。

用户在界面上把一条 `douyin.com` 的内容从 `video` 改成 `article` 之后，
**下一次存同一个域名的链接就不该再判成 `video`** —— 这是 M3-08 的全部意义。
表在 `0001_schema.sql` 就建好了（技术方案 §6.3），但直到本轮才有读写代码。

## 为什么写在这里而不在 `kb/extract/`

写库的唯一落点是 `kb/store/`（`layer_rules.toml` 守着）。而「用哪个 kind」
属于抓取层的判定逻辑——两者必须分开：抓取层算出一个**建议**，本层提供
**用户修正过的答案**，装配（谁覆盖谁）只在 `kb/api/` 做。

这条边界不是洁癖：`kb/extract/` 是纯函数区（可单测、无 IO），
一旦它开始查库，`decide_kind` 的几十条用例全部要造连接。

## 优先级（技术方案 T1）

`domain_type_rules` **优先于** `decide_kind` 的结构判定。也就是说用户的手动
修正永远赢过自动判断——否则「被记住」这件事就没有意义了：用户改了一次，
系统下次还按自己的规则判，等于没记住。

## 「合法类型」永远查表，不硬编码

`kind` 有外键指向 `item_kinds`。写死一个 `("video","article",...)` 元组的话，
用户在 `item_kinds` 里加一类之后本模块与数据库会各执一套答案——表现为
「用户改了一条规则，保存时报 23503 外键错」。所以校验走
`items.known_kinds()`，它本身就是查表。
"""

from __future__ import annotations

import logging

import pg8000.native

from kb.store import items as items_store

log = logging.getLogger(__name__)


class UnknownKindError(ValueError):
    """`kind` 不在 `item_kinds` 表里。

    带上 `allowed`：API 层要把它列进 400 的错误消息（「可选：video、article…」）。
    让调用方自己去查一次表也能做，但那是两次往返，且第二次查询与第一次的
    空隙里表可能被改了——错误消息里就会列出与校验时不同的候选集。
    """

    def __init__(self, kind: str, allowed: tuple[str, ...]) -> None:
        self.kind = kind
        self.allowed = allowed
        options = "、".join(allowed) if allowed else "（item_kinds 表为空）"
        super().__init__(f"未知的类型：{kind}（可选：{options}）")


def get_kind_for(conn: pg8000.native.Connection, domain: str) -> str | None:
    """查该域名的规则。**查不到返回 `None`，不抛异常。**

    返回 `None` 而不是抛 `KeyError`/`LookupError`：绝大多数域名没有规则，
    「没有规则」是**最常见**的正常结果，不是异常。调用方写
    `kind = get_kind_for(...) or 原判定` 才是正确的形状；抛异常会逼每个调用点
    写 try/except，而那种代码很快就会有人写成 `except: pass` 把真错误吞掉。

    空域名返回 `None` 且不查库：`domain_of` 对非法 URL 就是返回 `None`，
    调用方会把它转成空串传进来。空串在表里永远查不到，省一次往返。
    """
    if not domain:
        return None
    rows = conn.run(
        "select kind from domain_type_rules where domain = :d",
        d=domain,
    )
    if not rows:
        return None
    return str(rows[0][0])


def remember(conn: pg8000.native.Connection, domain: str, kind: str) -> None:
    """记住「这个域名应该是 kind」。已存在则更新。

    ## kind 必须先校验在 `item_kinds` 里

    不校验的话，非法 kind 会走到 `insert` 并被外键拒绝，pg8000 抛出
    `IntegrityError`（SQLSTATE 23503）——那个异常传到 API 层会变成 500，
    而真实原因是「用户传了个不存在的类型」，应该是 400。
    在这里先查表能给出**带候选值**的清晰错误。

    查表而不是比对常量：`item_kinds` 支持「加新形态 = 插一行」，
    硬编码的元组会漏掉用户新加的类型。

    ## 空域名直接拒绝

    `domain` 是主键，空串会被存成一行「所有非法 URL 共用」的规则——
    那行规则之后会命中的所有解析失败的链接。宁可报错让调用方发现
    自己在没有域名的情况下调了这个函数。
    """
    if not domain:
        raise ValueError("domain 不能为空：没有域名就没有「这个域名该是什么类型」")
    allowed = items_store.known_kinds(conn)
    if kind not in allowed:
        raise UnknownKindError(kind, allowed)
    conn.run(
        "insert into domain_type_rules (domain, kind) values (:d, :k) "
        "on conflict (domain) do update set kind = excluded.kind",
        d=domain,
        k=kind,
    )


def forget(conn: pg8000.native.Connection, domain: str) -> bool:
    """删除该域名的规则。返回**是否真的删了**（`False` = 本来就没有）。

    返回值不是装饰：API 层据此区分「撤销了一条规则」与「本来就没规则」。
    前者值得回一句「已忘记 douyin.com 的类型规则」，后者回它会让人怀疑
    「我刚才记的那条去哪了」。

    用 `returning domain` 而不是「先查再删」：两次往返之间有窗口，
    且「先查后删」在同一函数里读起来像两条独立语句，容易有人只改其中一条。
    """
    if not domain:
        return False
    rows = conn.run(
        "delete from domain_type_rules where domain = :d returning domain",
        d=domain,
    )
    return bool(rows)


def list_rules(conn: pg8000.native.Connection) -> list[tuple[str, str]]:
    """全部规则，按 `domain` 升序。返回 `(domain, kind)`。

    按 domain 排序而不是 created_at：这是「给用户看的一张规则表」，
    按域名找比按「什么时候加的」找更自然。顺序也必须确定——
    不确定顺序的列表在界面上会每次刷新都跳一下。
    """
    rows = conn.run("select domain, kind from domain_type_rules order by domain")
    return [(str(row[0]), str(row[1])) for row in rows]

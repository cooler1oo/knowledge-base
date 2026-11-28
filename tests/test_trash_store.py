"""回收站存储层（M4-03 T01）。

## 为什么这些用例用假连接

`soft_delete` / `restore_item` 的**判定顺序**（先查存在 → 再看是否已删 →
再查冲突 → 再写）是纯逻辑，与 SQL 细节可以分开验。假连接能精确摆出
「库里有一条已删的、一条活的同 URL」这种状态，真库要造一遍还得清干净。

真库行为（`on conflict` 的仲裁索引、`23505` 的真实形态、`make_interval`）
在 `tests/test_trash_pg.py` 验——两类用例的边界是刻意划开的。

## 守的是什么

三条最容易写错、且**错了不报错**的地方：

1. `and deleted_at is null` 漏了 → 连点两次删除会续期，回收站里那条永远不清
2. 恢复时 `not exists` 守卫漏了 → 撞部分唯一索引 23505，用户看到 500
3. `purge_*` 返回条数而不是 id 列表 → 清扫线程拿不到要删的索引文档
"""

from __future__ import annotations

from datetime import datetime, timezone

from kb.store import items as items_store

NOW = datetime(2026, 11, 28, 15, 20, 31, tzinfo=timezone.utc)


class FakeConn:
    """按 SQL 特征分派的假连接。

    `active` / `deleted` 是两张「虚拟表」：id → 行。用例直接改它们来摆状态，
    这样「同 URL 的活条目」这种场景一眼可见。
    """

    def __init__(
        self,
        *,
        active: dict[int, dict[str, object]] | None = None,
        deleted: dict[int, dict[str, object]] | None = None,
    ) -> None:
        self.active = dict(active or {})
        self.deleted_at: dict[int, datetime] = {}
        for i, row in (deleted or {}).items():
            self.deleted_at[i] = row.get("deleted_at", NOW)  # type: ignore[assignment]
            self.active[i] = row
        self.seen: list[tuple[str, dict[str, object]]] = []
        self.enqueued: list[str] = []

    # ── 查询辅助 ──

    def _row(self, item_id: int) -> dict[str, object] | None:
        return self.active.get(item_id)

    def _is_deleted(self, item_id: int) -> bool:
        return item_id in self.deleted_at

    def run(self, sql: str, **params: object) -> list[list[object]]:
        low = " ".join(sql.split()).lower()
        self.seen.append((low, params))
        i = params.get("i")
        item_id = int(str(i)) if i is not None else None

        # ── soft_delete ──
        if low.startswith("with upd as") and "set deleted_at = now()" in low:
            if (
                item_id is None
                or self._row(item_id) is None
                or self._is_deleted(item_id)
            ):
                return []  # ALREADY_DELETED / NOT_FOUND 由调用方再查
            # 记一笔 CTE 里的入队（真实 SQL 里由 `queued` 完成）
            if "insert into jobs" in low:
                self.enqueued.append("index")
            return [[item_id, NOW]]

        # ── restore 的守卫 UPDATE ──
        if low.startswith("with upd as") and "set deleted_at = null" in low:
            if item_id is None or not self._is_deleted(item_id):
                return []
            url_norm = self._row(item_id).get("url_norm")  # type: ignore[union-attr]
            if "not exists" in low and url_norm is not None:
                for other_id, other in self.active.items():
                    if (
                        other_id != item_id
                        and other.get("url_norm") == url_norm
                        and not self._is_deleted(other_id)
                    ):
                        return []  # 守卫拦下
            if "insert into jobs" in low:
                self.enqueued.append("index")
            del self.deleted_at[item_id]
            return [[item_id]]

        # ── 单行查询：存在性 / 是否已删 ──
        if low.startswith("select deleted_at from items"):
            if item_id is None or self._row(item_id) is None:
                return []
            return [[self.deleted_at.get(item_id)]]

        # ── 冲突查询 ──
        # 真实的 `_RESTORE_CONFLICT_SQL` 以 `select o.id from items o` 开头
        if low.startswith("select o.id from items o") and "url_norm" in low:
            if item_id is None or self._row(item_id) is None:
                return []
            url_norm = self._row(item_id).get("url_norm")
            out = []
            for other_id, other in self.active.items():
                if (
                    other_id != item_id
                    and other.get("url_norm") == url_norm
                    and url_norm is not None
                    and not self._is_deleted(other_id)
                ):
                    out.append([other_id])
            return out

        # ── 计数 ──
        if "count(*)" in low and "from items" in low:
            if "deleted_at is not null" in low:
                return [[len(self.deleted_at)]]
            return [[len(self.active)]]

        # ── 列表 ──
        if low.startswith("select i.id") and "i.deleted_at is not null" in low:
            rows = []
            for did, ts in sorted(
                self.deleted_at.items(), key=lambda kv: (kv[1], kv[0]), reverse=True
            ):
                row = self.active[did]
                rows.append(
                    [
                        did,
                        row.get("kind", "link"),
                        row.get("url"),
                        row.get("title", ""),
                        row.get("source_domain", ""),
                        row.get("saved_at", NOW),
                        row.get("status", "pending"),
                        row.get("summary"),
                        ts,
                        # purge_at（简化：+30 天）
                        ts,
                        # days_left
                        30,
                        # conflicts：同 url_norm 的活条目？
                        any(
                            oid != did
                            and o.get("url_norm") == row.get("url_norm")
                            and row.get("url_norm") is not None
                            and not self._is_deleted(oid)
                            for oid, o in self.active.items()
                        ),
                    ]
                )
            limit = int(str(params.get("limit") or 30))
            return rows[:limit]

        # ── purge ──
        # `purge_expired` 的 SQL 是 `with doomed as (...) delete from items ...`
        if low.startswith("with doomed as"):
            limit = int(str(params.get("limit") or 200))
            ids = sorted(self.deleted_at.keys(), key=lambda i: (self.deleted_at[i], i))
            out = []
            for did in ids[:limit]:
                del self.active[did]
                del self.deleted_at[did]
                out.append([did])
            return out

        if low.startswith("delete from items") and "deleted_at is not null" in low:
            if item_id is not None:
                if not self._is_deleted(item_id):
                    return []
                del self.active[item_id]
                del self.deleted_at[item_id]
                return [[item_id]]
            # purge_all：全删
            ids = sorted(self.deleted_at.keys())
            out = []
            for did in ids:
                del self.active[did]
                del self.deleted_at[did]
                out.append([did])
            return out

        return []


def _row(
    item_id: int = 1,
    *,
    url_norm: str | None = "https://x.com/a",
    kind: str = "link",
) -> dict[str, object]:
    return {
        "id": item_id,
        "kind": kind,
        "url": None if url_norm is None else "https://x.com/a",
        "url_norm": url_norm,
        "title": f"标题{item_id}",
        "source_domain": "x.com",
        "saved_at": NOW,
        "status": "pending",
        "summary": None,
    }


# ── 常量 ────────────────────────────────────────────────────


def test_保留期是30天() -> None:
    """PRD §7 第 2 条写死的产品决定，不是运维参数。

    这条用例的真正作用是「有人改它时必须显式改一条测试」——
    30 天变成别的数字，用户对「删掉的东西还能找回多久」的预期就变了。
    """
    assert items_store.TRASH_RETENTION_DAYS == 30


# ── soft_delete ─────────────────────────────────────────────


def test_软删成功返回deleted状态() -> None:
    conn = FakeConn(active={1: _row(1)})
    result = items_store.soft_delete(conn, 1)  # type: ignore[arg-type]

    assert result.status is items_store.DeleteStatus.DELETED
    assert result.item_id == 1
    assert result.deleted_at is not None
    assert result.purge_at is not None


def test_软删不存在的id返回not_found() -> None:
    conn = FakeConn()
    result = items_store.soft_delete(conn, 999)  # type: ignore[arg-type]
    assert result.status is items_store.DeleteStatus.NOT_FOUND
    assert result.deleted_at is None


def test_连删两次第二次是already_deleted且时间戳不续期() -> None:
    """**这条是本轮最重要的一条**（判据 2）。

    `update ... where deleted_at is null` 里的 `and deleted_at is null` 漏了，
    第二次删除就把戳写成新的 `now()`，保留期重新计时。现象是「回收站里有一条
    三个月前删的东西一直不清」——而它为什么不清，从任何日志里都看不出来。
    """
    first_ts = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
    conn = FakeConn(deleted={1: {**_row(1), "deleted_at": first_ts}})

    result = items_store.soft_delete(conn, 1)  # type: ignore[arg-type]

    assert result.status is items_store.DeleteStatus.ALREADY_DELETED
    assert result.deleted_at == first_ts, "必须返回**原来那次**的时间戳，不能续期"


def test_软删成功时入队index() -> None:
    """入队与盖戳在同一条 CTE 里（设计 §3.3 ②）。

    分开两条语句时，若第二条失败，那条内容在界面上已经消失、在搜索里还在，
    且没有任何机制会补。
    """
    conn = FakeConn(active={1: _row(1)})
    items_store.soft_delete(conn, 1)  # type: ignore[arg-type]
    assert "index" in conn.enqueued


def test_已删的条目不再重复入队index() -> None:
    """第二次删除现在什么都不做——包括不入队。

    `jobs_pending_uniq` 会挡掉重复入队，但更早挡住是对的：
    一次「没有改变任何状态」的请求不该产生任何副作用。
    """
    conn = FakeConn(deleted={1: _row(1)})
    items_store.soft_delete(conn, 1)  # type: ignore[arg-type]
    assert conn.enqueued == []


# ── restore_item ────────────────────────────────────────────


def test_恢复成功() -> None:
    conn = FakeConn(deleted={1: _row(1)})
    result = items_store.restore_item(conn, 1)  # type: ignore[arg-type]

    assert result.status is items_store.RestoreStatus.RESTORED
    assert result.item_id == 1
    assert result.conflict_item_id is None


def test_恢复本来没删的条目是already_active() -> None:
    """幂等：它已经可用了，不需要也不该改动什么。"""
    conn = FakeConn(active={1: _row(1)})
    result = items_store.restore_item(conn, 1)  # type: ignore[arg-type]

    assert result.status is items_store.RestoreStatus.ALREADY_ACTIVE


def test_恢复不存在的id是not_found() -> None:
    conn = FakeConn()
    result = items_store.restore_item(conn, 999)  # type: ignore[arg-type]
    assert result.status is items_store.RestoreStatus.NOT_FOUND


def test_恢复时同url有活条目返回conflict并带id() -> None:
    """**判据 3**：删 A → 存同 URL 得 B → 恢复 A 必须 409 且带 B 的 id。

    判定顺序（设计 §3.4）第 2 步的作用就是这个：给用户**原因**。
    没有它，用户只会看到「恢复失败」或一个 23505 的 500。
    """
    conn = FakeConn(
        active={
            1: _row(1, url_norm="https://x.com/a"),
            2: _row(2, url_norm="https://x.com/a"),
        },
        deleted={1: {**_row(1, url_norm="https://x.com/a"), "deleted_at": NOW}},
    )
    result = items_store.restore_item(conn, 1)  # type: ignore[arg-type]

    assert result.status is items_store.RestoreStatus.CONFLICT
    assert result.conflict_item_id == 2


def test_恢复时冲突的是别的url不算冲突() -> None:
    conn = FakeConn(
        active={
            1: _row(1, url_norm="https://x.com/a"),
            2: _row(2, url_norm="https://x.com/b"),
        },
        deleted={1: {**_row(1, url_norm="https://x.com/a"), "deleted_at": NOW}},
    )
    result = items_store.restore_item(conn, 1)  # type: ignore[arg-type]
    assert result.status is items_store.RestoreStatus.RESTORED


def test_恢复时同url的另一条也处于已删状态不算冲突() -> None:
    """两条都被删了，恢复其中一条是合法的——冲突只针对**活条目**。"""
    conn = FakeConn(
        deleted={
            1: {**_row(1, url_norm="https://x.com/a"), "deleted_at": NOW},
            2: {**_row(2, url_norm="https://x.com/a"), "deleted_at": NOW},
        }
    )
    result = items_store.restore_item(conn, 1)  # type: ignore[arg-type]
    assert result.status is items_store.RestoreStatus.RESTORED


def test_恢复时url_norm为null不判冲突() -> None:
    """纯文字条目（无 URL）没有 url_norm，永远不该被判成冲突。

    部分唯一索引的条件是 `where url_norm is not null`，两条无 URL 的条目
    在库里是合法的。
    """
    conn = FakeConn(
        active={1: _row(1, url_norm=None), 2: _row(2, url_norm=None)},
        deleted={1: {**_row(1, url_norm=None), "deleted_at": NOW}},
    )
    result = items_store.restore_item(conn, 1)  # type: ignore[arg-type]
    assert result.status is items_store.RestoreStatus.RESTORED


def test_恢复成功时入队index() -> None:
    """不重新写回索引就等于「恢复了但搜不到」——数据对、搜索结果错、不报错。"""
    conn = FakeConn(deleted={1: _row(1)})
    items_store.restore_item(conn, 1)  # type: ignore[arg-type]
    assert "index" in conn.enqueued


# ── list_trash / count_trash ────────────────────────────────


def test_回收站列表按删除时间倒序() -> None:
    """第一诉求是「撤销我刚才那次删除」，所以最近删的在最前。"""
    early = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
    late = datetime(2026, 11, 28, 9, 0, tzinfo=timezone.utc)
    conn = FakeConn(
        active={1: _row(1), 2: _row(2)},
        deleted={
            1: {**_row(1), "deleted_at": early},
            2: {**_row(2), "deleted_at": late},
        },
    )
    entries = items_store.list_trash(
        conn, limit=30, before_deleted_at=None, before_id=None
    )  # type: ignore[arg-type]

    assert [e.id for e in entries] == [2, 1]


def test_回收站列表标出与活条目的冲突() -> None:
    """`conflictsWithActive` 让用户在**点恢复之前**就知道会 409。

    没有它，用户点恢复 → 409 → 才去看为什么。提前标出来只需要一次子查询。
    """
    conn = FakeConn(
        active={
            1: _row(1, url_norm="https://x.com/a"),
            2: _row(2, url_norm="https://x.com/a"),
        },
        deleted={1: {**_row(1, url_norm="https://x.com/a"), "deleted_at": NOW}},
    )
    entries = items_store.list_trash(
        conn, limit=30, before_deleted_at=None, before_id=None
    )  # type: ignore[arg-type]

    assert len(entries) == 1
    assert entries[0].conflicts_with_active is True


def test_回收站条目的days_left非负() -> None:
    """`0` 表示今天就到期。负数会让界面显示「已过期 -3 天」这种没人看得懂的话。"""
    conn = FakeConn(deleted={1: _row(1)})
    entries = items_store.list_trash(
        conn, limit=30, before_deleted_at=None, before_id=None
    )  # type: ignore[arg-type]

    assert entries[0].days_left >= 0


def test_回收站列表尊重limit() -> None:
    conn = FakeConn(
        active={i: _row(i) for i in range(1, 6)},
        deleted={i: {**_row(i), "deleted_at": NOW} for i in range(1, 6)},
    )
    entries = items_store.list_trash(
        conn, limit=2, before_deleted_at=None, before_id=None
    )  # type: ignore[arg-type]
    assert len(entries) == 2


def test_count_trash只数已删的() -> None:
    conn = FakeConn(
        active={1: _row(1), 2: _row(2)},
        deleted={1: {**_row(1), "deleted_at": NOW}},
    )
    assert items_store.count_trash(conn) == 1  # type: ignore[arg-type]


def test_空回收站count为0() -> None:
    assert items_store.count_trash(FakeConn()) == 0  # type: ignore[arg-type]


# ── purge_* ────────────────────────────────────────────────


def test_purge_item删掉已删的条目返回True() -> None:
    conn = FakeConn(deleted={1: _row(1)})
    assert items_store.purge_item(conn, 1) is True  # type: ignore[arg-type]
    assert 1 not in conn.active


def test_purge_item对活条目返回False() -> None:
    """**只能删回收站里的**。直接清一条活条目等于跳过软删的确认环节——
    那正是「用户误点清空」时唯一的安全垫。
    """
    conn = FakeConn(active={1: _row(1)})
    assert items_store.purge_item(conn, 1) is False  # type: ignore[arg-type]
    assert 1 in conn.active, "活条目不该被删"


def test_purge_item对不存在的id返回False() -> None:
    assert items_store.purge_item(FakeConn(), 999) is False  # type: ignore[arg-type]


def test_purge_all返回被删的id列表() -> None:
    """返回 **id 列表**而不是条数：清扫线程要拿它去删索引文档。

    返回条数就得再查一次「刚删了谁」，而那时行已经不存在了。
    """
    conn = FakeConn(
        active={1: _row(1), 2: _row(2), 3: _row(3)},
        deleted={
            1: {**_row(1), "deleted_at": NOW},
            2: {**_row(2), "deleted_at": NOW},
        },
    )
    ids = items_store.purge_all(conn)  # type: ignore[arg-type]

    assert sorted(ids) == [1, 2]
    assert 3 in conn.active, "活条目不该被删"


def test_purge_expired返回id列表并尊重limit() -> None:
    conn = FakeConn(
        active={i: _row(i) for i in range(1, 6)},
        deleted={i: {**_row(i), "deleted_at": NOW} for i in range(1, 6)},
    )
    ids = items_store.purge_expired(conn, older_than_days=30, limit=3)  # type: ignore[arg-type]
    assert len(ids) == 3
    assert all(isinstance(i, int) for i in ids)


def test_purge_expired空回收站返回空列表() -> None:
    ids = items_store.purge_expired(FakeConn(), older_than_days=30, limit=200)  # type: ignore[arg-type]
    assert ids == []


# ── SQL 文本的守卫（假连接验不了这一层）──────────────────────
#
# 上面那批用例用假连接，验的是**判定顺序**（先查存在、再看冲突、再写）。
# 但假连接不解析 SQL——它按自己的 `_is_deleted` 状态决定返回什么，
# 所以「SQL 里到底有没有写 `and deleted_at is null`」这一层它测不到。
#
# 实测确认过：把 `_SOFT_DELETE_SQL` 里的 `and deleted_at is null` 删掉，
# `test_连删两次第二次是already_deleted且时间戳不续期` **仍然通过**。
# 那条用例守的是「函数返回什么」，不是「SQL 里写了什么」。
#
# 所以这一类守卫只能靠两种手段：
#   1. 读源码断言（本节的用例）——快、不依赖环境、抓得住「有人删掉了条件」
#   2. 真库用例（`tests/test_trash_pg.py`）——验 SQL 在 PG 上的真实语义
#
# 两者都要有：只有真库用例的话，本机没库时 CI 全跳过，改错了不会红；
# 只有源码断言的话，验不出「SQL 语法对不对」。


def test_软删SQL带不续期守卫() -> None:
    """`_SOFT_DELETE_SQL` 必须含 `and deleted_at is null`（判据 2 的根）。

    没有它，连点两次删除就把戳写成新的 `now()`，保留期重新计时。现象是
    「回收站里有一条三个月前删的东西一直不清」——从任何日志里都看不出来。

    ⚠️ 这条**必须读 SQL 常量而不是调函数**：假连接不解析 SQL，
    函数级用例抓不到这个条件被删掉。
    """
    assert "and deleted_at is null" in items_store._SOFT_DELETE_SQL, (
        "软删 SQL 少了「不续期」守卫：`where id = :i and deleted_at is null`。"
        "没有它，重复删除会重置保留期，回收站里的条目永远不会被自动清理。"
    )


def test_软删SQL在一条语句里同时盖戳与入队() -> None:
    """盖戳与入队必须在同一条 CTE 里（设计 §3.3 ②）。

    分成两条语句时，若第二条失败，那条内容在界面上已经消失、在搜索里还在，
    且没有任何机制会补。
    """
    sql = items_store._SOFT_DELETE_SQL
    assert "update items" in sql and "set deleted_at = now()" in sql
    assert "insert into jobs" in sql, "入队必须与盖戳在同一条语句里"
    # `queued` 必须显式读 `upd`，否则两个 data-modifying CTE 会变成兄弟、
    # 执行顺序不可预测（见 soft_delete 的 docstring）
    assert "from upd" in sql, "`queued` 没有显式引用 `upd`，CTE 执行顺序无法保证"


def test_软删SQL不需要恒真条件锚点() -> None:
    """这里**不该**有 `attachments.py` 那种 `(select count(*) from X) >= 0`。

    `upd` 与 `queued` 不是兄弟：`queued` 的 `from upd` 已建立依赖。
    多加一行恒真条件不会更安全，只会让下一个读者以为这里有 bug
    （设计 §3.3 ③ 明确要求不照抄）。
    """
    assert ">= 0" not in items_store._SOFT_DELETE_SQL, (
        "软删 SQL 里出现了恒真条件锚点。设计 §3.3 ③ 说明了为什么这里不需要："
        "`queued` 已显式读 `upd`，依赖已建立。"
    )


def test_恢复SQL带冲突守卫() -> None:
    """恢复必须带 `not exists` 守卫，否则并发下会撞 23505 → 500。"""
    sql = items_store._RESTORE_SQL
    assert "not exists" in sql, "恢复 SQL 少了同 URL 活条目的守卫"
    assert "set deleted_at = null" in sql
    assert "insert into jobs" in sql, "恢复也要入队 index，否则「恢复了但搜不到」"


def test_清理SQL带三条护栏() -> None:
    """`purge_expired` 的三条护栏都在 SQL 里（设计 §4.4）。

    | 护栏 | 没有它会怎样 |
    |---|---|
    | 保留期判断 | 会删掉用户昨天刚删的东西 |
    | `state='running'` 跳过 | 正在抓取的条目被删行，worker 拿着已不存在的 id 继续写，全程不报错 |
    | `limit` | 一次删几千行把 worker 占住几十秒 |
    """
    sql = items_store._PURGE_EXPIRED_SQL
    assert "make_interval(days =>" in sql, "少了保留期判断"
    assert "state = 'running'" in sql, "少了「跳过有 running 任务的条目」护栏"
    assert "limit :limit" in sql, "少了分批上限"
    # 顺序必须确定，否则两次跑之间「先删谁」会变
    assert "order by deleted_at, id" in sql, "排序少了 id 兜底键，分批策略会随机"


def test_清理SQL用数据库的now而不是python算() -> None:
    """容器时区 UTC、PG 时区 PRC，差 8 小时（`_db_now` 的实测记录）。

    「30 天前」若在 Python 里算，两侧会差 8 小时。30 天差 8 小时不致命，
    但同一件事有两处算法就有第二处漂移的机会，而这次是「东西被多留半天」
    这种没人会去核对的现象。
    """
    assert "now() - make_interval" in items_store._PURGE_EXPIRED_SQL, (
        "保留期应在 SQL 里用 now() 算，不要在 Python 侧算「30 天前的时刻」"
    )


def test_列表SQL的游标是复合键() -> None:
    """用 `id` 单键当游标会漏条：`deleted_at desc` 下一条早删的可能 id 更大。"""
    sql = items_store._LIST_TRASH_SQL_CURSOR
    assert "(i.deleted_at, i.id) <" in sql, "游标必须是 (deleted_at, id) 复合键"
    # 两处 cast 都不能省：pg8000 会把参数当 unknown，PG 推断不出类型
    assert "cast(:before_ts as timestamptz)" in sql
    assert "cast(:before_id as bigint)" in sql


def test_列表SQL判空不写进SQL() -> None:
    """`:before is null` 会让 pg8000 把参数当 unknown，PG 报
    `could not determine data type of parameter`。

    所以按有无游标拼两段 SQL，不做成一段带判空的。

    ⚠️ 不能简单断言「整条 SQL 里没有 `is null`」——`conflicts` 那个子查询里
    本来就有一处合法的 `o.deleted_at is null`（判「同 URL 的**活**条目」）。
    这里要抓的是「游标参数被拿去判空」这个具体模式。
    """
    sql = items_store._LIST_TRASH_SQL

    # 无游标那段不该出现任何游标参数
    assert "before_ts" not in sql, "无游标的 SQL 不该引用 before_ts 参数"
    assert "before_id" not in sql, "无游标的 SQL 不该引用 before_id 参数"

    # 两段都不该把游标参数用于判空（这是 pg8000 的坑）
    for name, text in (
        ("无游标", items_store._LIST_TRASH_SQL),
        ("有游标", items_store._LIST_TRASH_SQL_CURSOR),
    ):
        for param in ("before_ts", "before_id"):
            assert f"{param} is null" not in text, (
                f"{name}那段 SQL 把 `{param}` 用于判空。"
                "pg8000 会把参数当 unknown 类型，PG 报 "
                "`could not determine data type of parameter`"
            )

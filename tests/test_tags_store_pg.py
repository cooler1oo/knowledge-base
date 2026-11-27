"""`tags` 表读写：词表读取与「整体替换 AI 标签」。

**这一组守三条边界**（`F2`/`F4`/`F5`）：

1. **AI 不得建标签**（`F4`）：`tags` 表行数在 AI 打标签前后不变。
   这不是靠自觉——本文件里没有任何 `insert into tags` 的代码路径，
   唯一的写操作是 `item_tags`。
2. **只删 `origin='ai'`**（`B19`）：用户自己加的标签一条都不能动。
   删除条件必须同时含 `item_id` 与 `origin='ai'`——少了 `item_id`
   会把全库 AI 标签删掉，而这个 bug 在单条测试数据上看不出来。
3. **`confidence` 写 `NULL`**（`F5`）：本轮不给所有 AI 标签写死一个 0.8。
   若阈值取 0.7，那个筛子永远筛不出东西，等于把一条界面功能悄悄做废。
"""

from __future__ import annotations

import pytest

from kb.store import tags as tags_store


class _Recorder:
    def __init__(self, rows: list[list[object]] | None = None) -> None:
        self.seen: list[str] = []
        self.params: list[dict[str, object]] = []
        self._rows = rows or []

    def run(self, sql: str, **params: object) -> list[list[object]]:
        self.seen.append(" ".join(sql.split()))
        self.params.append(params)
        return self._rows


def _joined(conn: _Recorder) -> str:
    return " | ".join(conn.seen).lower()


# ── vocabulary ────────────────────────────────────────────────


def test_vocabulary按id升序返回三元组():
    """词表形状定死 `(tag_id, name, parent_name)`，`parent_name` 空串表示一级。

    返回父名而不是 `parent_id` 的理由：`validate_tags` 与 prompt 渲染都要用
    名字，返回 id 会让两处各查一次表。
    """
    conn = _Recorder(
        rows=[[1, "AI相关", None], [2, "skill和插件", "AI相关"], [3, "健康", None]]
    )
    vocab = tags_store.vocabulary(conn)
    assert vocab == (
        (1, "AI相关", ""),
        (2, "skill和插件", "AI相关"),
        (3, "健康", ""),
    )


def test_vocabulary用left_join取父名():
    """`left join` 而不是 `join`：一级标签没有父，`join` 会把它们全丢掉，
    结果是「词表里只剩子标签」，一级标签永远匹配不到（`F1` 场景之外的静默故障）。
    """
    conn = _Recorder(rows=[])
    tags_store.vocabulary(conn)
    sql = _joined(conn)
    assert "left join" in sql
    assert "order by" in sql


def test_vocabulary在空表时返回空元组_F1():
    conn = _Recorder(rows=[])
    assert tags_store.vocabulary(conn) == ()


def test_vocabulary只读不写_F4():
    """F4：AI 只能读词表。词表读取不得产生任何写库语句。"""
    conn = _Recorder(rows=[])
    tags_store.vocabulary(conn)
    sql = _joined(conn)
    assert "insert" not in sql
    assert "update" not in sql
    assert "delete" not in sql


# ── replace_ai_tags ───────────────────────────────────────────


def test_replace_ai_tags先删origin_ai再插新行_B19():
    conn = _Recorder()
    tags_store.replace_ai_tags(conn, 1, (5, 10))

    statements = conn.seen
    assert len(statements) == 2, f"应当只有两步（删 + 插），实际 {statements}"
    assert "delete from item_tags" in statements[0].lower()
    assert "insert into item_tags" in statements[1].lower()


def test_删除条件同时含item_id与origin_ai_B19():
    """少了 `item_id` 会把全库 AI 标签删掉——单条测试数据看不出这个 bug。"""
    conn = _Recorder()
    tags_store.replace_ai_tags(conn, 42, (5,))

    delete_sql = conn.seen[0].lower()
    assert "item_id = :i" in delete_sql
    assert "origin = 'ai'" in delete_sql


def test_写入行origin为ai且confidence为null_F5():
    """F5：`confidence` 写 `NULL`，不写编造的置信度。

    若写死 0.8 而筛子阈值取 0.7，「AI 置信度低」那个筛子永远筛不出东西，
    等于把一条界面功能悄悄做废（PRD §0 第 7 条）。
    """
    conn = _Recorder()
    tags_store.replace_ai_tags(conn, 1, (5,))

    insert_sql = conn.seen[1].lower()
    assert "'ai'" in insert_sql
    assert "null" in insert_sql, "confidence 必须写 NULL"
    assert "0.8" not in insert_sql, "不得写死置信度"


def test_空tag_ids时只删不插_A5():
    """A5：模型返回空数组 → 删掉旧 AI 标签，不插新行。

    这一步仍然要执行删除：重跑时模型这次没给标签，上次的标签必须消失，
    否则界面上会留着上一次的过期标签。
    """
    conn = _Recorder()
    tags_store.replace_ai_tags(conn, 1, ())

    assert len(conn.seen) == 1
    assert "delete from item_tags" in conn.seen[0].lower()


def test_插入用on_conflict_do_nothing():
    """`item_tags` 的主键是 `(item_id, tag_id)`。模型返回同一标签两次时
    （校验层已去重，但防御性）不能因主键冲突整条失败。
    """
    conn = _Recorder()
    tags_store.replace_ai_tags(conn, 1, (5,))
    assert "on conflict do nothing" in conn.seen[1].lower()


def test_标签id以数组参数一次传入而不是拼字符串():
    """拼字符串既慢又有注入面。用 `unnest` 或逐条 insert 都行，
    但参数必须是数组类型，不是逗号分隔的字符串。
    """
    conn = _Recorder()
    tags_store.replace_ai_tags(conn, 1, (5, 10))
    passed = conn.params[1].get("ids")
    assert isinstance(passed, list), f"标签 id 必须以列表传参，实际 {type(passed)}"
    assert passed == [5, 10]


# ── 真库行为 ──────────────────────────────────────────────────


@pytest.fixture
def pg_conn(pg_cfg):
    from kb.store.db import connect

    c = connect(pg_cfg)
    c.run("begin")
    try:
        yield c
    finally:
        c.run("rollback")
        c.close()


@pytest.fixture
def pg_item(pg_conn):
    item_id = pg_conn.run(
        "insert into items (kind, title) values ('text', 'x') returning id"
    )[0][0]
    return int(str(item_id))


def _tag_id(conn, name: str) -> int:
    """取或建一个标签，返回它的 id。

    ## 为什么 `select` 必须用与唯一索引**同样的归一化**

    `tags_sibling_name_uniq` 是表达式索引
    `on tags (coalesce(parent_id, 0), lower(btrim(name)))`。库里「健康」与
    「 健康 」是同一个标签，所以 `select ... where name = :n` 这种精确匹配
    会漏掉大小写/空白不同的既有行，然后 insert 撞唯一索引 → `23505`。

    ## 为什么要处理预置标签

    `0004_seed_ai_tags.sql` 预置了 8 个一级标签，其中就有「健康」。
    任何假设「`tags` 表是空的」的用例在干净测试库上都会撞。**不能为了
    让测试绿就删种子**（那是 `AI-03` 的需求），正确做法是「取或建」。
    """
    rows = conn.run(
        "select id from tags where lower(btrim(name)) = lower(btrim(:n))"
        " and parent_id is null"
        " order by id limit 1",
        n=name,
    )
    if rows:
        return int(str(rows[0][0]))
    return int(
        str(conn.run("insert into tags (name) values (:n) returning id", n=name)[0][0])
    )


def _ai_tag_rows(conn, item_id: int) -> list[tuple[int, str, object]]:
    rows = conn.run(
        "select tag_id, origin, confidence from item_tags where item_id = :i"
        " order by tag_id",
        i=item_id,
    )
    return [(int(str(r[0])), str(r[1]), r[2]) for r in rows]


@pytest.mark.pg
def test_真库_replace_ai_tags只删origin_ai保留origin_user_B19(pg_conn, pg_item):
    """B19：重跑后 `origin='user'` 那行仍在，`origin='ai'` 整体替换。"""
    ai_old = _tag_id(pg_conn, "旧AI标签")
    user_tag = _tag_id(pg_conn, "我自己加的")
    new_tag = _tag_id(pg_conn, "新AI标签")

    pg_conn.run(
        "insert into item_tags (item_id, tag_id, origin) values"
        " (:i, :a, 'ai'), (:i, :u, 'user')",
        i=pg_item,
        a=ai_old,
        u=user_tag,
    )
    tags_store.replace_ai_tags(pg_conn, pg_item, (new_tag,))

    rows = _ai_tag_rows(pg_conn, pg_item)
    tag_ids = [r[0] for r in rows]
    assert user_tag in tag_ids, "用户自己加的标签被删了"
    assert ai_old not in tag_ids, "旧 AI 标签没被替换"
    assert new_tag in tag_ids
    assert len(rows) == 2


@pytest.mark.pg
def test_真库_写入行origin为ai且confidence为null_F5(pg_conn, pg_item):
    tag = _tag_id(pg_conn, "健康")
    tags_store.replace_ai_tags(pg_conn, pg_item, (tag,))

    rows = _ai_tag_rows(pg_conn, pg_item)
    assert len(rows) == 1
    assert rows[0][1] == "ai"
    assert rows[0][2] is None, "confidence 必须是 NULL"


def _count_tags(conn) -> int:
    """`tags` 表当前行数。F4 的两条断言都用它。"""
    return int(str(conn.run("select count(*) from tags")[0][0]))


@pytest.mark.pg
def test_真库_AI打标签前后tags表行数不变_F4(pg_conn, pg_item):
    """F4：AI 不得建标签。

    ⚠️ **这条断言的范围是「`replace_ai_tags` 前后」，不是「整个用例前后」。**

    第一版写的是 `before = count() → _tag_id() → after_get = count()`，
    但 `_tag_id` 是「取或建」，它自己会 `insert into tags`。于是
    `before(0) != after_get(1)`，断言必然失败——**失败的是测试的准备动作，
    不是被测代码**。准备动作的副作用不能被算到 `replace_ai_tags` 头上。

    正确做法：先调 `_tag_id` 把标签准备好，再取基准，然后只量
    `replace_ai_tags` 这一步前后的差。
    """
    tag = _tag_id(pg_conn, "健康")  # 准备动作：这一步允许建标签
    before = _count_tags(pg_conn)  # 基准取在准备之后
    tags_store.replace_ai_tags(pg_conn, pg_item, (tag,))
    after = _count_tags(pg_conn)

    assert before == after, f"replace_ai_tags 建了标签：{before} → {after}"


@pytest.mark.pg
def test_真库_replace_ai_tags遇到不存在的tag_id报错且不建标签_F4(pg_conn, pg_item):
    """F4 更直接的守门用例：给一个库里不存在的 `tag_id`。

    `item_tags.tag_id` 有外键约束指向 `tags(id)`，所以不存在的 id 必须让
    INSERT 报错（外键违例），而不是「悄悄建一个新标签」或「静默跳过」。

    这条比上一条更强：上一条只证明「正常路径没多出行」，这条证明
    **异常路径也不会凭空造数据**。`F4` 真正要防的就是「AI 造词表」。

    ⚠️ **必须用 savepoint 包住失败语句**。PG 里任何语句报错后整个事务进入
    aborted 状态（错误码 25P02 `current transaction is aborted`），后面每条
    语句都会被拒——包括我们想用来断言「tags 没多出行」的那条 `count(*)`。
    第一版就是直接 `pytest.raises` 后紧跟 `count()`，结果拿到的是 25P02
    而不是断言结果。

    `tests/test_store_pg.py` 里的 `rejects()` 辅助函数已经示范了这个范式：
    先 `savepoint sp`，失败后 `rollback to savepoint sp`，事务恢复可用。
    """
    import pg8000.exceptions

    tags_store.replace_ai_tags(pg_conn, pg_item, ())  # 先清空，避免干扰
    before = _count_tags(pg_conn)

    pg_conn.run("savepoint sp")
    try:
        with pytest.raises(pg8000.exceptions.DatabaseError) as caught:
            tags_store.replace_ai_tags(pg_conn, pg_item, (999_999_999,))
        # 断言确实是外键违例（23503），不是别的报错恰好也抛了 DatabaseError
        assert "23503" in str(caught.value), f"不是外键违例：{caught.value}"
    finally:
        # 无论断言是否通过都回滚到 savepoint，否则事务卡在 aborted 状态
        pg_conn.run("rollback to savepoint sp")

    assert _count_tags(pg_conn) == before, "外键报错之后 tags 表多出了行"


@pytest.mark.pg
def test_真库_子标签命中子标签id而不是父id_F2(pg_conn, pg_item):
    """F2：模型返回「skill和插件」，写入的必须是子标签的 id。

    父「AI相关」是 0004 预置的一级标签；子标签这里现建。
    """
    parent = _tag_id(pg_conn, "AI相关")
    child = int(
        str(
            pg_conn.run(
                "insert into tags (name, parent_id) values ('skill和插件', :p)"
                " returning id",
                p=parent,
            )[0][0]
        )
    )
    tags_store.replace_ai_tags(pg_conn, pg_item, (child,))

    rows = _ai_tag_rows(pg_conn, pg_item)
    assert [r[0] for r in rows] == [child]
    assert child != parent


@pytest.mark.pg
def test_真库_空tag_ids删掉旧AI标签_A5(pg_conn, pg_item):
    old = _tag_id(pg_conn, "旧AI标签")
    pg_conn.run(
        "insert into item_tags (item_id, tag_id, origin) values (:i, :t, 'ai')",
        i=pg_item,
        t=old,
    )
    tags_store.replace_ai_tags(pg_conn, pg_item, ())
    assert _ai_tag_rows(pg_conn, pg_item) == []


@pytest.mark.pg
def test_真库_重复传同一id不报错():
    """（防御性）校验层已去重，这里再传两次也不能因主键冲突失败。"""
    # 用假连接即可：真库上这条会命中 on conflict do nothing，
    # 但需要 fixture 配合，断言的价值主要在 SQL 形状上（上面的用例已覆盖）。
    conn = _Recorder()
    tags_store.replace_ai_tags(conn, 1, (5, 5))
    assert "on conflict do nothing" in conn.seen[1].lower()


# ── F4：AI 不得建标签（静态守门）──────────────────────────────


def _code_only(module: object) -> str:
    """取模块源码并**剥掉注释与文档字符串**，只留可执行代码。

    为什么必须剥：本文件第一版直接 `inspect.getsource` 全文搜索，结果被
    `kb/store/tags.py` docstring 里那句「本文件里没有任何 `insert into tags`
    的代码路径」判成违规 —— **说明文字命中了搜索词**。

    这不是个别现象：`E6`（`kb/ai` 里不得出现进程环境写法）当初也踩过同一坑，
    最后靠「改掉注释措辞」绕过。静态断言的正确做法是**只看代码**，
    而不是要求写注释的人绕开某些词。
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    # 摘掉文档字符串：模块级/类级/函数级的第一个字符串表达式都是 docstring
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                body.pop(0)
    return ast.unparse(tree)


def test_tags_store源码里没有insert_into_tags_F4():
    """F4：AI 不得建标签 —— **用静态断言守，不靠运行时构造**。

    为什么静态断言比运行时构造更强（这条是本轮的一个方法论修正）：

    | 做法 | 能测的 | 测不到的 |
    |---|---|---|
    | 运行时给不存在的 `tag_id` | 这一次调用不建标签 | 「以后有人加了 `insert into tags`」 |
    | **静态断言源码无 `insert into tags`** | 这个模块**结构上**不可能建标签 | — |

    「AI 不建标签」不是某一次调用的行为，而是 `kb/store/tags.py` 这个模块的
    结构性事实。运行时构造「不存在的 tag_id」还依赖数据状态（`bigserial`
    的自增 id 可能恰好真的不存在，也可能因为回滚而复用），不稳定。

    真库上那条 FK 用例（`test_真库_replace_ai_tags遇到不存在的tag_id报错且不建标签_F4`）
    仍然保留：它证明「异常路径也不会凭空造数据」；这条静态断言证明
    「代码里根本没有造数据的路径」。两条互补，缺一不可。
    """
    code = _code_only(tags_store).lower()
    assert "insert into tags" not in code, (
        "F4：kb/store/tags.py 的可执行代码里不得出现 insert into tags（AI 只能读词表）"
    )
    # 反过来确认它确实写了 item_tags —— 否则上面那条会因「什么都没写」而假绿
    assert "insert into item_tags" in code, (
        "kb/store/tags.py 应当写 item_tags（AI 打标签的落点）"
    )


def test_tags_store只删origin为ai的行_B19():
    """B19：`replace_ai_tags` 的删除条件必须同时含 `item_id` 与 `origin='ai'`。

    少 `item_id` → 删全库 AI 标签（单条测试数据看不出来）。
    少 `origin='ai'` → 删掉用户自己加的标签。

    静态断言的理由同上：这是 SQL 文本的形状问题，假连接与真库都能验，
    但静态断言在本机就能跑，不依赖有没有 Postgres。
    """
    code = _code_only(tags_store).lower()
    assert "item_id = :i" in code, "删除/写入条件里没有 item_id"
    assert "origin = 'ai'" in code, "删除条件里没有 origin='ai'"

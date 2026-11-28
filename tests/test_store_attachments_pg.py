"""`attachments` 表读写：替换语义、唯一索引、角色与 mime 约束（真库）。

连不上 Postgres 时整组 skip（见 `tests/conftest.py`）。在平板上跑：

```bash
KB_PG_HOST=127.0.0.1 KB_PG_USER=u0_a201 python -m pytest tests/test_store_attachments_pg.py -q
```

## 五条必须成立的性质

1. **整体替换是原子的**：`replace_for_item` 是单条 `WITH` 语句。分成两条就有一个
   窗口——旧的删了、新的没进去 → 条目没有封面，而 `attachments` 里一行都不剩。
2. **只动 `cover` / `image`**：将来 `snapshot` / `video` 的附件不能被 fetch 重跑误删。
3. **空列表也删**：语义是「这条内容现在没有任何封面与图片」。不删的话，
   上游改成纯文字后旧封面会永远挂着，用户看到一张与内容无关的图。
4. **`attachments_key_uniq` 真的会拦**：两个 item 用同一个 key 必须被拒。
   这是「key 必须混入 item_id」的直接依据。
5. **`role` / `mime` 约束真的生效**：`image/svg+xml` 能被 `image/` 前缀放过入库
   ——所以拒它必须发生在下载阶段（`sniff`），这条用例钉住「为什么必须在下载阶段拦」。
"""

from __future__ import annotations

import pg8000.exceptions
import pytest

from kb.media.models import StoredBlob
from kb.store import attachments as attachments_store
from kb.store.db import connect
from kb.store.migrate import discover, migrate

#: 整份文件都要真库。**这个标记不能省**：它让 `-m pg` 能选中本文件。
#:
#: 2026-11-28 发现本文件原先只有 `pg_cfg` fixture（连不上就 skip），缺这个标记
#: ——功能上不影响本机跑（照样 skip），但 `-m pg` 只收集到 76 条而不是 119 条，
#: 也就是说「在平板上只跑 pg 用例」会静默漏掉本文件全部 43 条，而这 43 条
#: 正是唯一能验证 CTE 执行顺序与唯一索引的地方。
pytestmark = pytest.mark.pg


@pytest.fixture(scope="session")
def fresh_db(pg_cfg):
    """重建 kb_test 并跑一遍迁移。"""
    admin = connect(pg_cfg.with_database("postgres"))
    admin.run(f'drop database if exists "{pg_cfg.database}" with (force)')
    admin.run(f'create database "{pg_cfg.database}"')
    admin.close()

    conn = connect(pg_cfg)
    try:
        migrate(conn, discover())
    finally:
        conn.close()
    return pg_cfg


@pytest.fixture
def conn(fresh_db):
    """每个用例一个事务，结束回滚。"""
    c = connect(fresh_db)
    c.run("begin")
    try:
        yield c
    finally:
        c.run("rollback")
        c.close()


def _item(conn, *, kind: str = "video") -> int:
    return conn.run(
        "insert into items (kind, title) values (:k, 'x') returning id", k=kind
    )[0][0]


def _blob(
    *,
    role: str = "cover",
    key: str = "ab/" + "c" * 32 + ".jpg",
    mime: str = "image/jpeg",
    size: int = 12345,
    order: int = 0,
) -> StoredBlob:
    return StoredBlob(
        role=role,
        storage_key=key,
        mime=mime,
        size_bytes=size,
        width=640,
        height=480,
        duration_sec=None,
        sha256="0" * 64,
        sort_order=order,
    )


def _rows(conn, item_id: int) -> list[tuple[str, str, int]]:
    raw = conn.run(
        "select role, storage_key, sort_order from attachments "
        "where item_id = :i order by sort_order, id",
        i=item_id,
    )
    return [(str(r[0]), str(r[1]), int(r[2])) for r in raw]


def rejects(conn, sql: str, **params: object) -> None:
    """断言这条 SQL 被拒，回滚到 savepoint 以免污染后续语句。"""
    conn.run("savepoint sp")
    try:
        conn.run(sql, **params)
    except pg8000.exceptions.DatabaseError:
        conn.run("rollback to savepoint sp")
        return
    raise AssertionError(f"期望被拒绝，但执行成功了：{sql}")


# ── 替换语义 ────────────────────────────────────────────────


def test_首次写入返回写入行数(conn):
    item_id = _item(conn)
    written = attachments_store.replace_for_item(
        conn,
        item_id,
        [_blob(), _blob(role="image", key="ab/" + "d" * 32 + ".jpg", order=1)],
    )
    assert written == 2
    assert len(_rows(conn, item_id)) == 2


def test_重跑一遍结果与跑一遍相同(conn):
    """整体替换的幂等：重跑不产生第二份行。

    ⚠️ 这条用例的验证力完全依赖「DELETE 先于 INSERT」在库里成立。
    PG 不保证 WITH 子语句的顺序（§7.8.2），所以 `_REPLACE_SQL` 里有
    `where (select count(*) from gone) >= 0` 强制依赖。这条用例若在真库上
    绿，只说明当前版本的执行计划是那样；**不能**替代
    `test_CTE的ins必须依赖gone` 那条静态断言。
    """
    item_id = _item(conn)
    blobs = [_blob(), _blob(role="image", key="ab/" + "d" * 32 + ".jpg", order=1)]

    attachments_store.replace_for_item(conn, item_id, blobs)
    first = _rows(conn, item_id)
    attachments_store.replace_for_item(conn, item_id, blobs)
    second = _rows(conn, item_id)

    assert first == second
    assert len(second) == 2, "重跑多出了行"


def test_同key连续替换两次不撞唯一索引(conn):
    """**这条是「依赖锚点」的主要守门用例——它断言的是行为，不是字符串。**

    首次 fetch 永远绿：表里没有旧行，DELETE 跑不跑都不影响。只有**重跑**
    （新旧 `storage_key` 完全相同）才会撞 `attachments_key_uniq`。

    触发条件正好是「重试幂等」这个核心场景：上游换了一张不同内容的封面时
    key 会变、不撞；但**重跑同一批媒体**时 key 不变、必撞。

    ## 为什么这条比 `test_CTE的ins必须依赖gone` 更重要

    那条用字符串断言守同一件事，**重构 SQL 时（换写法但行为不变）会误报**，
    而行为坏了它可能照样绿（比如把依赖写成别的形式）。破例说清楚：
    team-lead 在平板真 PG 上做过 A/B 对照——

    ```
    A 组（保留 `where (select count(*) from gone) >= 0`）→ count=1 成功
    B 组（去掉那一行）→ duplicate key value violates unique constraint
    ```

    这条用例就是那个 A/B 的可执行形态。
    """

    item_id = _item(conn)
    same = _blob(role="cover", key="ab/" + "c" * 32 + ".jpg")

    first = attachments_store.replace_for_item(conn, item_id, [same])
    second = attachments_store.replace_for_item(conn, item_id, [same])

    assert first == 1
    assert second == 1, "第二次没写进去（DELETE 可能被推迟到 INSERT 之后）"
    assert _rows(conn, item_id) == [("cover", "ab/" + "c" * 32 + ".jpg", 0)]


def test_同一item重复写同一批多行也不撞索引(conn):
    """上一条只覆盖 1 行；这条覆盖**多行 + 混合 role** 的重跑。

    多行时 `unnest` 会一次插多条，若 DELETE 被推迟，第一条 INSERT 就撞。
    单独一条是因为「1 行能过、多行不能过」在 PG 的执行计划上是不同路径
    ——1 行可能走 Result 节点，多行走 Function Scan，两者的 CTE 折叠行为
    不受同一条保证约束。
    """
    item_id = _item(conn)
    blobs = [
        _blob(role="cover", key="ab/" + "1" * 32 + ".jpg", order=0),
        _blob(role="image", key="ab/" + "2" * 32 + ".jpg", order=1),
        _blob(role="image", key="ab/" + "3" * 32 + ".jpg", order=2),
    ]

    for _ in range(3):
        written = attachments_store.replace_for_item(conn, item_id, blobs)
        assert written == 3, "多行重跑有行没写进去"

    assert len(_rows(conn, item_id)) == 3, "多行重跑产生了重复行"


def test_内容变化后旧行消失(conn):
    """上游换了封面 → key 变 → 新的行替换旧的行。旧文件成孤儿（本轮不清理）。"""
    item_id = _item(conn)
    attachments_store.replace_for_item(conn, item_id, [_blob()])
    attachments_store.replace_for_item(
        conn, item_id, [_blob(key="ab/" + "e" * 32 + ".jpg")]
    )

    rows = _rows(conn, item_id)
    assert len(rows) == 1
    assert rows[0][1] == "ab/" + "e" * 32 + ".jpg"


def test_空列表把旧行全部删掉(conn):
    """**这条最容易写错。**

    语义是「这条内容现在没有任何封面与图片」。若空列表直接 `return 0` 而不删，
    上游内容改成纯文字后，库里那张旧封面会永远挂着——用户看到的是一张与内容
    无关的图，且没有任何逻辑会再碰它。
    """
    item_id = _item(conn)
    attachments_store.replace_for_item(conn, item_id, [_blob()])
    assert len(_rows(conn, item_id)) == 1

    written = attachments_store.replace_for_item(conn, item_id, [])

    assert written == 0
    assert _rows(conn, item_id) == [], "空列表没有清掉旧行"


def test_只替换cover与image不动其他role(conn):
    """`snapshot`（抽帧）/ `video`（将来按需下载）/ `doc` / `audio` 不能被
    「fetch 重跑」误删——那些附件的产出代价比封面高得多。
    """
    item_id = _item(conn)
    conn.run(
        "insert into attachments (item_id, role, storage_key, mime, sha256, sort_order) "
        "values (:i, 'snapshot', :k, 'image/jpeg', :s, 5)",
        i=item_id,
        k="ff/" + "a" * 32 + ".jpg",
        s="1" * 64,
    )
    attachments_store.replace_for_item(conn, item_id, [_blob()])
    attachments_store.replace_for_item(conn, item_id, [])

    rows = _rows(conn, item_id)
    assert len(rows) == 1, "snapshot 附件被误删了"
    assert rows[0][0] == "snapshot"


def test_不属于白名单的role被跳过而不是抛异常(conn):
    """调用方（worker）只会传 cover/image。若传了别的，跳过比抛好：
    抛会让整条 fetch 失败（`mark_failed`），而正确结果是「那一条不落库、其余照常」。
    """
    item_id = _item(conn)
    written = attachments_store.replace_for_item(
        conn,
        item_id,
        [_blob(), _blob(role="snapshot", key="ee/" + "a" * 32 + ".jpg", order=1)],
    )
    assert written == 1, "不该写入 snapshot 那条"
    assert [r[0] for r in _rows(conn, item_id)] == ["cover"]


def test_替换不影响其他item的附件(conn):
    first = _item(conn)
    second = _item(conn)
    attachments_store.replace_for_item(conn, first, [_blob()])
    attachments_store.replace_for_item(
        conn, second, [_blob(key="ab/" + "f" * 32 + ".jpg")]
    )

    attachments_store.replace_for_item(conn, first, [])

    assert _rows(conn, first) == []
    assert len(_rows(conn, second)) == 1, "另一个 item 的附件被牵连了"


def test_多行写入保持sort_order(conn):
    item_id = _item(conn)
    blobs = [
        _blob(role="cover", key="ab/" + "1" * 32 + ".jpg", order=0),
        _blob(role="image", key="ab/" + "2" * 32 + ".jpg", order=1),
        _blob(role="image", key="ab/" + "3" * 32 + ".jpg", order=2),
    ]
    attachments_store.replace_for_item(conn, item_id, blobs)

    # 只 select 了一列，所以取 `r[0]`。写 `r[2]` 会 IndexError
    # ——真库上才暴露（本机无 PG 时整组 skip）。
    orders = [
        int(str(r[0]))
        for r in conn.run(
            "select sort_order from attachments where item_id = :i order by id",
            i=item_id,
        )
    ]
    assert orders == [0, 1, 2]


def test_width_height写入并被读回(conn):
    item_id = _item(conn)
    attachments_store.replace_for_item(conn, item_id, [_blob()])

    row = conn.run(
        "select width, height, duration_sec, size_bytes from attachments where item_id = :i",
        i=item_id,
    )[0]
    assert (int(row[0]), int(row[1])) == (640, 480)
    assert row[2] is None, "不下视频，duration_sec 该是 null"
    assert int(row[3]) == 12345


def test_可空字段传None不报错(conn):
    item_id = _item(conn)
    blob = StoredBlob(
        role="cover",
        storage_key="ab/" + "9" * 32 + ".png",
        mime="image/png",
        size_bytes=1,
        width=None,
        height=None,
        duration_sec=None,
        sha256="2" * 64,
        sort_order=0,
    )
    assert attachments_store.replace_for_item(conn, item_id, [blob]) == 1


# ── 唯一索引真的会拦 ───────────────────────────────────────


def test_不同item用同一个key被唯一索引拒绝(conn):
    """`attachments_key_uniq` 是 `storage_key` 上的**全局**唯一索引。

    这是「key 必须混入 `item_id`」的直接依据：纯内容哈希会让两个 item 的同一张图
    撞索引，补救只有 `on conflict do nothing`——于是第二个 item 永远没有封面，
    而字节明明在磁盘上。
    """
    first = _item(conn)
    second = _item(conn)
    attachments_store.replace_for_item(conn, first, [_blob()])

    rejects(
        conn,
        "insert into attachments (item_id, role, storage_key, mime, sha256) "
        "values (:i, 'cover', :k, 'image/jpeg', :s)",
        i=second,
        k="ab/" + "c" * 32 + ".jpg",  # 与 first 的 key 相同
        s="0" * 64,
    )


def test_replace_for_item不写on_conflict_do_nothing(conn):
    """冲突不该发生，若发生按**编程错误**处理。

    写了 `do nothing` 等于把一次真实的数据完整性异常伪装成正常运行，而
    「第二个 item 悄悄没有封面」这种故障排查起来极慢。

    ## 为什么断言 `_REPLACE_SQL` 而不是 `inspect.getsource(模块)`

    第一版用的是 `inspect.getsource(attachments_store)`，结果**恒红**：
    模块 docstring 里有一行小节标题写着
    ``## 为什么不写 `on conflict do nothing``` ——「on conflict」这几个字
    出现在**注释**里，而 `getsource` 拿到的是含注释的完整源码。

    断言的对象必须是「SQL 常量里没有这段子句」，不是「文件里没有这几个字」。
    否则注释怎么写都会影响用例，而注释本来就是要写清楚为什么不用它。
    """
    sql = attachments_store._REPLACE_SQL.lower()
    assert "on conflict" not in sql, (
        f"`_REPLACE_SQL` 里出现了 on conflict：{attachments_store._REPLACE_SQL}"
    )
    assert "insert into attachments" in sql


# ── 约束生效（证明「必须在下载阶段拦」）──────────────────────


def test_svg的mime能过CHECK_所以必须在下载阶段拒(conn):
    """`attachments_mime_allowed` 只做**前缀**白名单，`image/svg+xml` 满足
    `image/` 前缀、**能入库**。

    这条用例的意义不是「库该拒它」，恰恰相反：**库拦不住**。所以拒它必须发生在
    下载阶段（`kb/media/sniff.py` 的 `FORBIDDEN_MIMES`），而那一步的用例在
    `tests/test_media_sniff.py`。这里钉住「为什么不能只靠 CHECK」。
    """
    item_id = _item(conn)
    conn.run(
        "insert into attachments (item_id, role, storage_key, mime, sha256) "
        "values (:i, 'cover', :k, 'image/svg+xml', :s)",
        i=item_id,
        k="ab/" + "7" * 32 + ".jpg",
        s="3" * 64,
    )
    # 没有抛异常 = CHECK 放过了它。这正是必须在 sniff 阶段拒的理由。
    assert len(_rows(conn, item_id)) == 1


def test_非法role被CHECK拒绝(conn):
    item_id = _item(conn)
    rejects(
        conn,
        "insert into attachments (item_id, role, storage_key, mime, sha256) "
        "values (:i, 'avatar', :k, 'image/jpeg', :s)",
        i=item_id,
        k="ab/" + "8" * 32 + ".jpg",
        s="4" * 64,
    )


def test_非法mime被CHECK拒绝(conn):
    """`application/x-msdownload` 这类可执行类型不在白名单里。"""
    item_id = _item(conn)
    rejects(
        conn,
        "insert into attachments (item_id, role, storage_key, mime, sha256) "
        "values (:i, 'doc', :k, 'application/x-msdownload', :s)",
        i=item_id,
        k="ab/" + "5" * 32 + ".exe",
        s="5" * 64,
    )


def test_删除item级联删附件(conn):
    """`on delete cascade` 必须在：否则删条目会留下永远没人引用的附件行。"""
    item_id = _item(conn)
    attachments_store.replace_for_item(conn, item_id, [_blob()])
    conn.run("delete from items where id = :i", i=item_id)
    assert _rows(conn, item_id) == []


# ── mime_for_key ────────────────────────────────────────────


def test_mime_for_key查得到(conn):
    item_id = _item(conn)
    attachments_store.replace_for_item(
        conn, item_id, [_blob(mime="image/webp", key="ab/" + "0" * 32 + ".webp")]
    )
    assert (
        attachments_store.mime_for_key(conn, "ab/" + "0" * 32 + ".webp") == "image/webp"
    )


def test_mime_for_key查不到返回None(conn):
    """`None` 而不是空串：端点据此给 **404**。空串会让 `FileResponse`
    拿不到 media_type、由 Starlette 按扩展名猜——「库里没有记录」被伪装成
    「有记录但类型不明」。
    """
    assert attachments_store.mime_for_key(conn, "zz/" + "9" * 32 + ".jpg") is None


# ── stats / count_for_item / primary_media_id ───────────────


def test_stats按role汇总字节(conn):
    item_id = _item(conn)
    attachments_store.replace_for_item(
        conn,
        item_id,
        [
            _blob(role="cover", key="ab/" + "1" * 32 + ".jpg", size=100, order=0),
            _blob(role="image", key="ab/" + "2" * 32 + ".jpg", size=200, order=1),
            _blob(role="image", key="ab/" + "3" * 32 + ".jpg", size=300, order=2),
        ],
    )
    totals = attachments_store.stats(conn, item_id)
    assert totals == {"cover": 100, "image": 500}


def test_count_for_item按role计数(conn):
    item_id = _item(conn)
    attachments_store.replace_for_item(
        conn,
        item_id,
        [
            _blob(role="cover", key="ab/" + "1" * 32 + ".jpg", order=0),
            _blob(role="image", key="ab/" + "2" * 32 + ".jpg", order=1),
        ],
    )
    assert attachments_store.count_for_item(conn, item_id) == {"cover": 1, "image": 1}


def test_没有附件时stats为空字典(conn):
    assert attachments_store.stats(conn, _item(conn)) == {}


def test_primary_media_id指向封面(conn):
    """`items.primary_media` 的语义是「这条内容用来做展示图的那张附件」，
    不是「视频附件」。视频条目指向**封面**。语义写错，M7-01 的卡片拿不到图。
    """
    item_id = _item(conn)
    attachments_store.replace_for_item(
        conn,
        item_id,
        [
            _blob(role="cover", key="ab/" + "1" * 32 + ".jpg", order=0),
            _blob(role="image", key="ab/" + "2" * 32 + ".jpg", order=1),
        ],
    )
    cover_id = attachments_store.primary_media_id(conn, item_id)
    assert cover_id is not None
    row = conn.run("select role from attachments where id = :i", i=cover_id)[0]
    assert str(row[0]) == "cover"


def test_没有cover时primary_media_id回落到第一张image(conn):
    """**真机验证抓到的第二个 bug 的配套修复。**

    图文没有 `thumb_url`，而 `plan.py` 刻意不额外产 `role='cover'`——那张图与
    第一张 `role='image'` 字节相同、`storage_key` 相同，真库里会撞
    `attachments_key_uniq`（索引里没有 `role`）。

    所以图文的展示图必须靠 image 回落拿到。没有这条回落，图文条目的
    `primary_media` 永远是 `null`，卡片**全部空白**——而图文是小红书/微博
    这类内容的主形态。
    """
    item_id = _item(conn)
    attachments_store.replace_for_item(
        conn,
        item_id,
        [
            _blob(role="image", key="ab/" + "1" * 32 + ".jpg", order=1),
            _blob(role="image", key="ab/" + "2" * 32 + ".jpg", order=2),
        ],
    )
    primary = attachments_store.primary_media_id(conn, item_id)
    assert primary is not None, "图文没有 cover 行时该回落到第一张 image"
    row = conn.run(
        "select role, storage_key from attachments where id = :i", i=primary
    )[0]
    assert str(row[0]) == "image"
    assert str(row[1]) == "ab/" + "1" * 32 + ".jpg", "该取 sort_order 最小的那张"


def test_cover优先于image即使image的sort_order更小(conn):
    """视频同时有封面与配图时，展示图必须是**封面**，不是 sort_order 更小的图。

    判据是 `role = 'cover'` 优先，不是纯粹的 `sort_order` 排序。
    """
    item_id = _item(conn)
    conn.run(
        "insert into attachments "
        " (item_id, role, storage_key, mime, sha256, sort_order) values "
        " (:i, 'image', :k1, 'image/jpeg', :s, 0), "
        " (:i, 'cover', :k2, 'image/jpeg', :s, 9)",
        i=item_id,
        k1="ab/" + "1" * 32 + ".jpg",
        k2="ab/" + "2" * 32 + ".jpg",
        s="7" * 64,
    )
    chosen = attachments_store.primary_media_id(conn, item_id)
    row = conn.run("select role, storage_key from attachments where id = :i", i=chosen)[
        0
    ]
    assert str(row[0]) == "cover", "cover 没优先于 sort_order 更小的 image"
    assert str(row[1]) == "ab/" + "2" * 32 + ".jpg"


def test_完全没有附件时primary_media_id返回None(conn):
    """`None` 让 `items.primary_media` 留 `null`，前端按无封面渲染占位。"""
    assert attachments_store.primary_media_id(conn, _item(conn)) is None


def test_多条cover时取sort_order最小的(conn):
    """一张内容最多一个封面，但排序兜底能保证「万一有多条」时结果确定。"""
    item_id = _item(conn)
    conn.run(
        "insert into attachments (item_id, role, storage_key, mime, sha256, sort_order) "
        "values (:i, 'cover', :k1, 'image/jpeg', :s, 5), "
        "       (:i, 'cover', :k2, 'image/jpeg', :s, 1)",
        i=item_id,
        k1="ab/" + "1" * 32 + ".jpg",
        k2="ab/" + "2" * 32 + ".jpg",
        s="6" * 64,
    )
    chosen = attachments_store.primary_media_id(conn, item_id)
    row = conn.run("select storage_key from attachments where id = :i", i=chosen)[0]
    assert str(row[0]) == "ab/" + "2" * 32 + ".jpg"

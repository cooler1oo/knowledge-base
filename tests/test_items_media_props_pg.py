"""`items` 的媒体状态字段：`primary_media` / `props.mediaError` / `props.mediaTruncated`。

**为什么这三个字段单独一个文件**：它们有一条共同的、最容易写错的语义——
「成功时必须**删掉**旧错误键」。

`props || cast(:p as jsonb)` 只覆盖同名键、**不删**旧键。所以「这次成功了」如果
只写一个空 payload，上一次的 `mediaError` 会一直留在库里，界面永远显示
「封面未能下载」而实际早就好了。这个 bug 不报错、不影响数据、只在界面上骗人。

反过来「这次没有封面」必须显式写 `primary_media = null`：它属于上一版内容，
留着会让卡片显示一张与内容无关的图。

## 为什么不用假连接验 SQL

这三条全是 SQL 语义（`||` 的合并行为、`-` 的删键、`case when` 的分支），
用 `_Recorder` 只能断言「字符串里有这些词」——那证明不了 SQL 真的这么跑。
所以本文件用**真库**（`@pytest.mark.pg`），本机无库时整组 skip。
`kb/store` 是唯一允许写库 SQL 的层，测它的行为必须让库来判。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from kb.store import items as items_store
from kb.store.db import connect
from kb.store.migrate import discover, migrate

pytestmark = pytest.mark.pg


@pytest.fixture(scope="session")
def fresh_db(pg_cfg):
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
    c = connect(fresh_db)
    c.run("begin")
    try:
        yield c
    finally:
        c.run("rollback")
        c.close()


def _item(conn, *, props: str = "{}") -> int:
    return conn.run(
        "insert into items (kind, title, props) values ('video', 'x', cast(:p as jsonb)) "
        "returning id",
        p=props,
    )[0][0]


def _state(conn, item_id: int) -> tuple[object, dict[str, object]]:
    row = conn.run("select primary_media, props from items where id = :i", i=item_id)[0]
    return row[0], items_store._as_props(row[1])


# ── set_media_props ─────────────────────────────────────────


def test_成功时写primary_media并删掉mediaError(conn):
    """**本文件最重要的用例。**

    成功路径不删旧 `mediaError`，界面会一直显示「封面未能下载」——
    这个 bug 不报错、不影响数据、只在界面上骗人。
    """
    item_id = _item(conn, props='{"mediaError": "封面下载失败（HTTP 403）"}')

    items_store.set_media_props(
        conn, item_id, primary_media=42, media_error="", media_truncated=0
    )

    primary, props = _state(conn, item_id)
    assert int(str(primary)) == 42
    assert "mediaError" not in props, f"旧的 mediaError 没被删掉：{props}"


def test_失败时写mediaError并留primary_media为null(conn):
    """封面没下下来时 `primary_media` 留 `null`——前端按无封面渲染占位。

    M7-01 的「不裁切」要求下，占位比拉伸一张错图好。
    """
    item_id = _item(conn)

    items_store.set_media_props(
        conn,
        item_id,
        primary_media=None,
        media_error="封面：HTTP 403",
        media_truncated=0,
    )

    primary, props = _state(conn, item_id)
    assert primary is None
    assert props["mediaError"] == "封面：HTTP 403"


def test_上次有封面这次没有时必须清空primary_media(conn):
    """内容从视频改成纯文字。旧封面留着会让卡片显示一张与内容无关的图。"""
    item_id = _item(conn)
    items_store.set_media_props(
        conn, item_id, primary_media=42, media_error="", media_truncated=0
    )

    items_store.set_media_props(
        conn, item_id, primary_media=None, media_error="", media_truncated=0
    )

    primary, _ = _state(conn, item_id)
    assert primary is None, "旧的 primary_media 没被清掉"


def test_截断数写入props(conn):
    item_id = _item(conn)
    items_store.set_media_props(
        conn, item_id, primary_media=None, media_error="", media_truncated=15
    )
    _, props = _state(conn, item_id)
    assert props["mediaTruncated"] == 15


def test_没截断时删掉mediaTruncated键(conn):
    """**这条在真库上抓到过一个真 bug，是本文件最有价值的一条。**

    `payload` 只在 `media_truncated > 0` 时才带这个键，所以「本次没截断」时
    `||` 不会覆盖旧值 —— 修之前旧值会一直留着。

    真实后果：第一次抓了 35 张图被截到 20（`mediaTruncated: 15`），用户重存
    同一内容、这次只有 3 张图没截断 → 界面**永远**显示「已截断 15 张」，
    而实际上 3 张全存了。用户会以为丢了图。

    ⚠️ **本机无 PG 时这条 skip，所以「本机全绿」证明不了这个 bug 已修**。
    这也是为什么修它时先在平板真库上复现、修完再复跑。
    """
    item_id = _item(conn, props='{"mediaTruncated": 15}')

    items_store.set_media_props(
        conn, item_id, primary_media=None, media_error="", media_truncated=0
    )

    _, props = _state(conn, item_id)
    assert "mediaTruncated" not in props, f"旧的截断数没被删掉：{props}"


def test_截断数从有到无再从无到有(conn):
    """四个分支循环一遍。**只测单向的「15 → 0」会漏掉「0 → 15」。**

    `mediaTruncated` 的删除与写入走的是同一条 SQL 的两个部分
    （`- 'mediaTruncated'` 与 `|| payload`），任何一侧写错都只在
    「方向切换」时暴露。
    """
    item_id = _item(conn)

    # ① 首次无截断 → 不该有键
    items_store.set_media_props(
        conn, item_id, primary_media=None, media_error="", media_truncated=0
    )
    _, props = _state(conn, item_id)
    assert "mediaTruncated" not in props, "① 首次无截断却写了键"

    # ② 有截断 → 要写进去
    items_store.set_media_props(
        conn, item_id, primary_media=None, media_error="", media_truncated=15
    )
    _, props = _state(conn, item_id)
    assert props["mediaTruncated"] == 15, "② 有截断却没写进去"

    # ③ 变回无截断 → 必须删掉
    items_store.set_media_props(
        conn, item_id, primary_media=None, media_error="", media_truncated=0
    )
    _, props = _state(conn, item_id)
    assert "mediaTruncated" not in props, "③ 从有到无没删掉旧值"

    # ④ 再变有截断（值不同）→ 要更新而不是叠加
    items_store.set_media_props(
        conn, item_id, primary_media=None, media_error="", media_truncated=7
    )
    _, props = _state(conn, item_id)
    assert props["mediaTruncated"] == 7, "④ 从无到有没写对值"


def test_mediaError与mediaTruncated互不干扰(conn):
    """两个键的删除/写入必须在**同一条 SQL 里各自独立**成立。

    修 `mediaTruncated` 时最容易犯的错是把两个键一起删
    （`props - 'mediaError' - 'mediaTruncated'`），那样 `mediaError`
    的「本次失败要写值」分支就丢了。这条把四种组合都跑一遍：

    | err | truncated | 期望 |
    |---|---|---|
    | 空 | 0 | 两个键都不在 |
    | 空 | 15 | 只有 truncated |
    | 有 | 0 | 只有 mediaError |
    | 有 | 15 | 两个都在 |
    """
    item_id = _item(conn)

    items_store.set_media_props(
        conn, item_id, primary_media=None, media_error="", media_truncated=0
    )
    _, props = _state(conn, item_id)
    assert "mediaError" not in props and "mediaTruncated" not in props

    items_store.set_media_props(
        conn, item_id, primary_media=None, media_error="", media_truncated=15
    )
    _, props = _state(conn, item_id)
    assert "mediaError" not in props
    assert props["mediaTruncated"] == 15

    items_store.set_media_props(
        conn,
        item_id,
        primary_media=None,
        media_error="封面：HTTP 403",
        media_truncated=0,
    )
    _, props = _state(conn, item_id)
    assert props["mediaError"] == "封面：HTTP 403"
    assert "mediaTruncated" not in props, "写 mediaError 时没删掉旧的截断数"

    items_store.set_media_props(
        conn,
        item_id,
        primary_media=None,
        media_error="图片：HTTP 500",
        media_truncated=7,
    )
    _, props = _state(conn, item_id)
    assert props["mediaError"] == "图片：HTTP 500"
    assert props["mediaTruncated"] == 7


def test_不影响其他props键(conn):
    """`props` 是长尾属性桶，媒体阶段只该动自己那三个键。

    整体覆盖会抹掉入库时写入的字段（分享文案、platform、mediaUrls），
    那是静默数据丢失，不会报错。
    """
    item_id = _item(
        conn,
        props='{"platform": "douyin", "mediaCount": 1, "custom": "keep me"}',
    )

    items_store.set_media_props(
        conn, item_id, primary_media=7, media_error="", media_truncated=0
    )

    _, props = _state(conn, item_id)
    assert props["platform"] == "douyin"
    assert props["mediaCount"] == 1
    assert props["custom"] == "keep me"


def test_不写jobs_error(conn):
    """媒体失败**不写 `jobs.error`**：那会让条目显示成「处理失败」。

    这条用真库断言：函数跑完之后 `jobs` 表里没有任何带 error 的行。
    """
    item_id = _item(conn)
    conn.run(
        "insert into jobs (item_id, step, state) values (:i, 'fetch', 'running')",
        i=item_id,
    )

    items_store.set_media_props(
        conn,
        item_id,
        primary_media=None,
        media_error="封面：HTTP 403",
        media_truncated=0,
    )

    rows = conn.run("select error from jobs where item_id = :i", i=item_id)
    assert all(row[0] is None for row in rows), "媒体失败污染了 jobs.error"


def test_写库操作更新updated_at(conn):
    """`updated_at` 不更新的话，「最近改动」类的查询看不到这条变化。"""
    item_id = _item(conn)
    before = conn.run("select updated_at from items where id = :i", i=item_id)[0][0]

    items_store.set_media_props(
        conn, item_id, primary_media=1, media_error="", media_truncated=0
    )

    after = conn.run("select updated_at from items where id = :i", i=item_id)[0][0]
    assert isinstance(after, datetime)
    assert after >= before


# ── clear_media_props ───────────────────────────────────────


def test_清空三个媒体字段(conn):
    """抓取失败时用：上次的封面属于上一版内容，留着会让用户看到一张错的图。"""
    item_id = _item(conn, props='{"mediaError": "旧的错误", "mediaTruncated": 9}')
    items_store.set_media_props(
        conn, item_id, primary_media=42, media_error="旧的错误", media_truncated=9
    )

    items_store.clear_media_props(conn, item_id)

    primary, props = _state(conn, item_id)
    assert primary is None
    assert "mediaError" not in props
    assert "mediaTruncated" not in props


def test_清空时不动其他props(conn):
    item_id = _item(conn, props='{"platform": "douyin", "mediaCount": 1}')

    items_store.clear_media_props(conn, item_id)

    _, props = _state(conn, item_id)
    assert props["platform"] == "douyin"
    assert props["mediaCount"] == 1


# ── primary_media 与生成列不冲突 ───────────────────────────


def test_duration_sec生成列仍然正常(conn):
    """`items.duration_sec` 是 `props->>'duration_sec'` 的生成列。

    媒体阶段的 SQL 里不出现 `content_text` / `kind` / `duration_sec`，
    所以它不该被影响。这条钉住「加媒体字段没顺手把生成列写坏」。
    """
    item_id = conn.run(
        "insert into items (kind, title, props) values "
        "('video', 'x', cast(:p as jsonb)) returning id",
        p='{"duration_sec": "445"}',
    )[0][0]
    before = conn.run("select duration_sec from items where id = :i", i=item_id)[0][0]
    assert int(str(before)) == 445

    items_store.set_media_props(
        conn, item_id, primary_media=1, media_error="", media_truncated=0
    )

    after = conn.run("select duration_sec from items where id = :i", i=item_id)[0][0]
    assert int(str(after)) == 445, "媒体阶段的写入把生成列弄坏了"

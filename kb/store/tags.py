"""标签词表读取 + 整体替换 AI 标签。

**为什么现在新建这个文件**（设计 §3.1）：`kb/store/` 之前没有 `tags.py`，
标签读写散落在未来各处会让「AI 不得写词表」这条边界失去唯一落点。
写 `tags` 表的代码只允许出现在这里，读出来一看就知道有没有违规。

三条硬边界：

| 边界 | 验收 | 怎么保证 |
|---|---|---|
| AI 只读词表，不得建标签 | `F4` | 本文件里**没有任何** `insert into tags` 的代码路径 |
| 只替换 `origin='ai'` | `B19` | 删除条件同时含 `item_id` 与 `origin='ai'` |
| `confidence` 写 `NULL` | `F5` | INSERT 里显式写 `null`，不写编造的常数 |

`F5` 那条的理由值得记住：本轮若给所有 AI 标签写死 0.8，而筛子阈值取 0.7，
「AI 置信度低于阈值」这个筛子**永远筛不出东西**——等于把一条界面功能
悄悄做废，且不会报错。
"""

from __future__ import annotations

import pg8000.native


def vocabulary(
    conn: pg8000.native.Connection,
) -> tuple[tuple[int, str, str], ...]:
    """读全部标签，返回 `(tag_id, name, parent_name)`，按 `id` 升序。

    **必须是 `left join`**：一级标签没有父，内连接会把它们全丢掉，
    结果是「词表里只剩子标签」，一级标签永远匹配不到。这个故障不报错，
    只表现为「AI 不打一级标签」——很难从现象倒推原因。

    返回父**名字**而不是 `parent_id`：`validate_tags` 与 prompt 渲染都要用名字，
    返回 id 会让两处各再查一次表。
    """
    rows = conn.run(
        "select t.id, t.name, p.name from tags t "
        "left join tags p on p.id = t.parent_id "
        "order by t.id"
    )
    return tuple(
        (
            int(str(row[0])),
            str(row[1]),
            "" if row[2] is None else str(row[2]),
        )
        for row in rows
    )


def replace_ai_tags(
    conn: pg8000.native.Connection, item_id: int, tag_ids: tuple[int, ...]
) -> None:
    """整体替换某条内容的 AI 标签：先删 `origin='ai'`，再插新行。

    为什么是「整体替换」而不是「增量添加」（`B19`）：重跑时模型这次给了
    2 个标签、上次给了 3 个，增量添加会留下第 3 个过期标签；用户看到的是
    「AI 打了 3 个」而模型其实只说了 2 个。

    **删除条件必须同时含 `item_id` 与 `origin='ai'`**：少了 `item_id` 会把
    全库 AI 标签删掉——这个 bug 在只有一条测试数据的用例里完全看不出来。

    `tag_ids` 为空时**仍然执行删除**（`A5`）：这次模型没给标签，上次的就该消失。

    参数以**数组**传（`unnest`），不拼字符串：拼字符串既慢又有注入面。
    """
    conn.run(
        "delete from item_tags where item_id = :i and origin = 'ai'",
        i=item_id,
    )
    if not tag_ids:
        return
    conn.run(
        "insert into item_tags (item_id, tag_id, origin, confidence) "
        "select :i, tid, 'ai', null from unnest(cast(:ids as bigint[])) as tid "
        "on conflict do nothing",
        i=item_id,
        ids=list(tag_ids),
    )

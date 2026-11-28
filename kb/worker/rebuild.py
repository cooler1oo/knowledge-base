"""全量重建检索索引：`python -m kb.worker.rebuild`。

## 为什么放在 worker 层而不是 `kb/search/`

重建要读库（`items` + `item_tags`），而 `kb/search/` 不允许 import `kb.store`
（设计 §1.3）。装配只能在能读库的层做——这是 `kb/worker/rebuild.py` 存在的
唯一理由。

## 为什么不是「先删索引再灌」（设计 §7.2）

删与灌之间有一段时间搜索结果为空。这个窗口在对账任务里可能长达几十秒，
而用户不会知道「正在重建」，只会认为搜索坏了。

先写后删孤儿全程可搜，代价是多拿一次全索引 id 列表（几千条以内一次请求可完成）。

## 索引与库不同步的三个来源（设计 §7.1）

| 来源 | 现象 | 对策 |
|---|---|---|
| 写路径漏入队 index | 改了标题/标签后搜不到新值 | 硬规则（见 `kb/store/search.py` 的模块 docstring）+ 本命令兜住 |
| Meili 数据目录损坏 / 换设备 | 搜索结果缺失或全空 | 本命令全量重灌 |
| 索引里留下已删条目 | 卡片点进去 404 | §3.7 的「查回来少了就跳过」+ 本命令清孤儿 |

## 跑法

```
python -m kb.worker.rebuild          # 平板容器内，读 KB_PG_* 与 KB_MEILI_*
```

输出「灌 N 条，删 M 条孤儿」。**跑两遍第二遍 M 应为 0**——那是「差集算法
正确」的可观测证据。
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Iterator

from kb.search.client import client_from_env
from kb.search.config import INDEX_BATCH
from kb.search.indexer import Indexer
from kb.store import search as search_store
from kb.store.config import PgConfig
from kb.store.db import Pool

log = logging.getLogger(__name__)

#: 每页读多少条。与 `INDEX_BATCH` 取同一个值：一页正好一批请求体，
#: 两个数字不同时「读 200 条、一次只能写 50 条」会让日志里的批数看起来莫名其妙。
PAGE_SIZE = INDEX_BATCH


def _pages(conn: Any, page_size: int) -> Iterator[list[Any]]:
    """按游标翻页产出文档。**惰性的**：5000 条内容不会一次性堆在内存里。"""
    after_id = 0
    while True:
        page = search_store.index_source_page(conn, after_id=after_id, limit=page_size)
        if not page:
            return
        yield page
        after_id = page[-1].id


def _max_item_id(conn: Any) -> int | None:
    """重建开始时的 `max(items.id)` 快照。空库返回 `None`。

    ## 为什么需要这个快照

    `Indexer.rebuild` 算孤儿用 `existing - seen`。rebuild 翻页遍历库的几十秒里，
    若 worker 为一条**新入库**的内容写了索引，那条文档在 `existing` 里、
    不在任何 `page` 里 → 被当孤儿删掉。现象是「刚存的内容 rebuild 后从搜索里
    消失，库里还在」，而那条内容的 `index` job 已是 `done`，不会自动补。

    有了快照之后只删 `id <= max_id` 的孤儿——重建期间新插入的条目 id 必然
    大于它，天然免疫。

    ⚠️ 这是「尽力而为」的保护，不是事务隔离：若并发写入的**正文更新**发生在
    已翻过的页上（id 小于 max_id 但内容变了），索引里会是旧内容，下一轮
    rebuild 或该条内容的下次 `index` 会修正。要彻底消除竞态需要库级快照
    （`repeatable read` 事务），代价远大于收益——个人库的 rebuild 是运维动作，
    通常不与 worker 并发跑。
    """
    rows = conn.run("select max(id) from items")
    if not rows or rows[0][0] is None:
        return None
    return int(str(rows[0][0]))


def rebuild(
    pool: Pool, indexer: Indexer, *, page_size: int = PAGE_SIZE
) -> dict[str, int]:
    """重灌全部文档并清孤儿。返回 `{"written": N, "deleted": M, "orphans": M}`。

    `indexer.rebuild` 拿的是一个 `Iterable[Sequence[IndexDoc]]`——逐页消费，
    所以连接只在取页的那一刻被持有，中途不长期占用池里的连接。

    **`max_id` 在翻页之前取**：那是「重建开始时库里最大 id」的快照，用于
    保护并发写入的新内容（见 `_max_item_id` 的 docstring）。取在翻页之后
    就失去意义了——那时新内容已经进库，会被当成「重建范围内的数据」。
    """
    with pool.connection() as conn:
        max_id = _max_item_id(conn)
        log.info("重建快照 max_id=%s（本次只清理 id <= 它的孤儿）", max_id)
        return indexer.rebuild(_pages(conn, page_size), max_id=max_id)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m kb.worker.rebuild")
    parser.add_argument(
        "--page-size",
        type=int,
        default=PAGE_SIZE,
        help=f"每页读取条数（默认 {PAGE_SIZE}）",
    )
    parser.add_argument("--log-level", default="info")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    pool = Pool(PgConfig.from_env())
    try:
        indexer = Indexer(client_from_env())
        result = rebuild(pool, indexer, page_size=int(args.page_size))
    except Exception as exc:  # noqa: BLE001 CLI 的职责是把失败讲清楚
        sys.stderr.write(f"\n[rebuild] 失败：{type(exc).__name__}: {exc}\n")
        sys.stderr.write(
            "  检查：① Meilisearch 是否在跑（curl 127.0.0.1:7700/health）\n"
        )
        sys.stderr.write("        ② KB_MEILI_KEY 是否正确\n")
        sys.stderr.write("        ③ KB_PG_* 是否指向正确的库\n\n")
        return 1

    # **这一行是判据**：跑两遍，第二遍的 `deleted` 必须是 0。
    sys.stdout.write(
        f"\n[rebuild] 灌 {result['written']} 条，删 {result['deleted']} 条孤儿\n"
    )
    if result["orphans"] != result["deleted"]:
        sys.stdout.write(
            f"[rebuild] ⚠️ 孤儿数 {result['orphans']} 与删除数 "
            f"{result['deleted']} 不一致（部分删除失败）\n"
        )
    if result.get("missing"):
        # 写入静默失败：`add_documents` 拿到 202 + taskUid，任务却在 Meili 里
        # failed。2026-11-28 真机踩过（请求体包错一层），现象是「搜索永远 0 条」。
        sys.stderr.write(
            f"[rebuild] ✗ 有 {result['missing']} 条写入未落地——"
            "检查 Meili 任务列表里有没有 failed 的 documentAdditionOrUpdate：\n"
            f"  curl -H 'Authorization: Bearer $KB_MEILI_KEY' "
            f"'http://127.0.0.1:7700/tasks?indexUids=kb_items&limit=5'\n"
        )
        return 2
    sys.stdout.write(f"[rebuild] 索引现有 {result.get('indexed', 0)} 条\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    sys.exit(main())

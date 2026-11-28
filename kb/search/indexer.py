"""索引编排：建索引 / 写文档 / 删文档 / 全量重建。

**只做编排，不做装配**：本文件的输入是 `IndexDoc` 列表，输出是对客户端的调用。
「items 行 + 标签名数组 → IndexDoc」那次读库在 `kb/store/search.py`，
由 worker 层的 `run_index` 把两边接起来（设计 §1.3 的单向箭头）。

这样拆的收益是可测：`tests/test_search_indexer.py` 只用一个假 HTTP 实现和
一个 dict 列表就能验「upsert 的请求体长什么样」「200 条怎么切」「重建的删除
集合怎么算」——不连库、不联网。
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterable, Sequence

from .client import MeiliClient, client_from_env
from .config import INDEX_BATCH, INDEX_UID, index_settings
from .models import IndexDoc

log = logging.getLogger(__name__)


class Indexer:
    """把 `IndexDoc` 写进 Meili。

    `index_uid` 默认来自 `config.INDEX_UID`——**不给第二处字面量**
    （设计 §10，`tests/test_search_arch.py` 数这个字符串）。
    """

    def __init__(
        self,
        client: MeiliClient,
        *,
        index_uid: str = INDEX_UID,
        batch_size: int = INDEX_BATCH,
    ) -> None:
        self.client = client
        self.index_uid = index_uid
        self.batch_size = batch_size
        #: `ensure_index` 是否已在本进程里成功跑过一次。见 `ensure_index` 的说明。
        self._ensured = False
        #: 最近一次写文档拿到的 `taskUid`。**只给 `rebuild` 用**：
        #: 对账前必须等它完成，否则「刚提交、还没落地」会被误判成「写入失败」。
        #: 热路径（`run_index`）不等它——那里等的代价是把一条内容占住 worker 数秒。
        self.last_write_task = 0

    # ── 设置 ─────────────────────────────────────────────────

    def ensure_index(self) -> None:
        """确保索引存在，然后幂等 PATCH 设置并**等任务完成**。

        三步都不能省：

        1. **先建索引**。Meili 的 PATCH `/settings` 对不存在的索引返回 404，
           而 404 在薄客户端里判 `MeiliRejected`（编程错误）——第一次部署时
           每条内容的 `run_index` 都会被判成「我们的 bug」，且重试永远不成功。
        2. **等设置任务**。不等的话后续第一次写入落在默认设置下
           （`filterableAttributes` 只有 `id`），带 `kind` 筛选的搜索会报
           `Attribute kind is not filterable`。
        3. **只做一次**。`run_index` 每条内容都会调它，每调一次就是
           一次 PATCH + 一次任务等待。

        ⚠️ 缓存的是「本进程内成功过一次」。若期间别处改了设置，本进程不会重设
        ——这正是 `python -m kb.search.indexer --create` 存在的场景之一。
        """
        if self._ensured:
            return
        if not self.client.index_exists(self.index_uid):
            self.client.create_index(self.index_uid)
        task_uid = self.client.patch_settings(self.index_uid, index_settings())
        self.client.wait_task(task_uid)
        self._ensured = True
        log.info("索引设置已生效 uid=%s", self.index_uid)

    # ── 写 ───────────────────────────────────────────────────

    def upsert(self, docs: Sequence[IndexDoc]) -> int:
        """写入/更新一批文档，返回**提交的文档条数**。

        按 `batch_size` 切批：一次请求体过大在平板上会超时，而 Meili 内部
        仍按任务队列串行处理，切批不损失吞吐。

        **不等任务**：Meili 的写是异步队列，等它会把一次 200 条的批量
        变成秒级阻塞。代价是「标 done 后 1–2 秒索引才可搜」，对单人使用无影响。
        """
        if not docs:
            return 0
        written = 0
        for start in range(0, len(docs), self.batch_size):
            chunk = docs[start : start + self.batch_size]
            self.ensure_index()
            self.last_write_task = self.client.add_documents(
                self.index_uid, [doc.to_meili_dict() for doc in chunk]
            )
            written += len(chunk)
        log.info(
            "索引写入 %d 条（%d 批）uid=%s",
            written,
            _batches(len(docs), self.batch_size),
            self.index_uid,
        )
        return written

    def delete_ids(self, ids: Sequence[int]) -> int:
        """按主键删一批文档，返回**提交的 id 条数**。

        `ids` 为空时**零请求**：rebuild 第二遍的差集为空，打一次空请求会让
        日志里出现「删了 0 条」这种噪声，也会让「第二遍 M=0」的验收变成
        「既要看请求次数又要看返回条数」两件事。
        """
        if not ids:
            return 0
        self.client.delete_documents(self.index_uid, list(ids))
        log.info("索引删除 %d 条 uid=%s", len(ids), self.index_uid)
        return len(ids)

    # ── 重建（设计 §7.2）─────────────────────────────────────

    def rebuild(
        self,
        pages: Iterable[Sequence[IndexDoc]],
        *,
        max_id: int | None = None,
    ) -> dict[str, int]:
        """全量重建：**先写后删孤儿**，全程可搜。

        ## 为什么不是「先删索引再灌」

        删与灌之间有一段时间搜索结果为空。这个窗口在对账任务里可能长达几十秒，
        而用户不会知道「正在重建」，只会认为搜索坏了。

        先写后删孤儿全程可搜，代价是多拿一次全索引 id 列表（几千条以内一次
        请求可完成）。删的集合是「索引里有、库里没有」的差集——它同时清掉
        `items.deleted_at` 非空的软删条目（`index_source_page` 不返回它们）。

        `pages` 是**惰性的**：调用方传一个生成器，每页一次 SQL。
        这样 5000 条内容的库不会一次性把全部文档堆在内存里。

        ## ⚠️ `max_id`：并发写入的保护

        `orphans = existing - seen` 有一个时序漏洞：rebuild 翻页遍历库的
        几十秒里，若 worker 为一条**新入库**的内容写了索引，那条文档在
        `existing` 里、不在任何 `page` 里 → 被当孤儿删掉。现象是「刚存的内容
        rebuild 后从搜索里消失，库里还在」，而那条内容的 `index` job 已是
        `done`，不会自动补。

        传 `max_id`（重建开始时 `select max(id) from items` 的快照）后，
        只删 `id <= max_id` 的孤儿——重建期间新插入的条目 id 必然大于它，
        天然免疫。

        `None` 表示不做这个保护（单测与「不与 worker 并发」的场景）。

        为什么 `max_id` 由调用方取而不是在这里查：`kb/search/` 不读库
        （`layer_rules.toml` 的 `import_deny` 守着）。`kb/worker/rebuild.py`
        在开始翻页前取一次。这也是为什么本参数是**可选**的——不知道快照的
        调用方不该被强制构造一个假值。
        """
        seen: set[int] = set()
        written = 0
        for page in pages:
            if not page:
                continue
            written += self.upsert(page)
            seen.update(doc.id for doc in page)

        # 等最后一批写入落地再对账，否则「还没写完」会被误判成「写入失败」。
        # `wait_task` 超时抛 `MeiliUnavailable`，那是真实故障，该向上抛。
        self.client.wait_task(self.last_write_task)

        existing = set(self.client.index_document_ids(self.index_uid))
        # 只删「快照范围内」的孤儿。`max_id` 之后的 id 是重建期间新插入的，
        # 它们进 `existing` 是因为 worker 已经给它们写了索引——**不是孤儿**。
        # 删掉它们的现象是「刚存的内容 rebuild 后从搜索里消失」，而那条内容的
        # `index` job 已 done，不会自动补。
        orphans = sorted(
            doc_id for doc_id in existing - seen if max_id is None or doc_id <= max_id
        )
        deleted = self.delete_ids(orphans)
        missing = sorted(seen - existing)
        if missing:
            log.error(
                "索引写入未落地 %d 条（id 前 10 个：%s）uid=%s；"
                "检查 Meili 任务列表（GET /tasks?indexUids=%s）里有没有 failed",
                len(missing),
                missing[:10],
                self.index_uid,
                self.index_uid,
            )
        return {
            "written": written,
            "deleted": deleted,
            "orphans": len(orphans),
            "indexed": len(existing),
            "missing": len(missing),
        }


def _batches(total: int, size: int) -> int:
    """要发几次请求。只为日志可读，不参与控制流。"""
    if size <= 0:
        return 1
    return (total + size - 1) // size


def create_index(client: MeiliClient, index_uid: str = INDEX_UID) -> None:
    """建索引（若无）+ 让设置生效。`--create` 的实现体，也给 `ensure_index` 复用。

    先查再建，不靠「建失败就忽略」：`POST /indexes` 对已存在的 uid 返回 409，
    而 409 落在薄客户端的 4xx 兜底分支里会变成 `MeiliUnavailable`——
    那是一句「重试可能成功」的错话，实际重试永远一样。
    """
    if not client.index_exists(index_uid):
        client.create_index(index_uid)
    Indexer(client, index_uid=index_uid).ensure_index()


def main(argv: list[str] | None = None) -> int:
    """`python -m kb.search.indexer --create`：建索引并等设置生效。

    这条命令是给「不想等第一条内容入库」的场景用的，以及手工排障：
    首装时 `ensure_index` 会被 worker 在第一次 `run_index` 时自动调用，
    所以它不是必需步骤。
    """
    parser = argparse.ArgumentParser(prog="python -m kb.search.indexer")
    parser.add_argument(
        "--create",
        action="store_true",
        help="建立索引并等设置任务完成（幂等）",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    if not args.create:
        parser.print_help()
        return 0

    try:
        create_index(client_from_env())
    except Exception as exc:  # noqa: BLE001 CLI 的职责是把失败讲清楚，不是抛栈给用户
        sys.stderr.write(f"\n[search] 建索引失败：{type(exc).__name__}: {exc}\n")
        sys.stderr.write(
            "  检查：① Meilisearch 是否在跑（curl 127.0.0.1:7700/health）\n"
        )
        sys.stderr.write("        ② KB_MEILI_KEY 是否正确\n\n")
        return 1
    sys.stdout.write(f"[search] 索引 {INDEX_UID} 已就绪（设置已生效）\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    sys.exit(main())

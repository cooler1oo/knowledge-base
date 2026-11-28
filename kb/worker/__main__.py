"""worker 启动入口：`python -m kb.worker`。

两个参数之外的复杂性都在这里收掉：连接池装配、心跳定时、信号退出。

心跳为什么必须写：`/api/health` 的 `workerOk` 靠 `settings.WORKER_HEARTBEAT_KEY`
判断 worker 是否活着，90 秒没更新就报 false。没有心跳，即使 worker 在满负荷
干活，健康检查也会说它死了。所以心跳间隔必须显著小于 90 秒——见 HEARTBEAT_SECONDS。
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from datetime import datetime, timezone
from typing import Any

from kb.extract.router import extract
from kb.store import settings
from kb.store.config import PgConfig
from kb.store.db import Pool
from kb.store.settings import WORKER_HEARTBEAT_KEY

from .loop import run_forever
from .purge import purge_loop

log = logging.getLogger(__name__)


def ensure_ai_available() -> None:
    """启动阶段就确认 AI 的**传输实现**能 import。缺 `httpx` 时报错并退出（`B27`）。

    **为什么不能在领到第一条任务时才失败**：那时 worker 已经写了心跳、
    `/api/health` 显示 `workerOk: true`、用户以为它在干活。等到第一条
    `summarize` 任务到达才崩，表现为「任务一直 pending、日志里一条 ImportError」，
    而真正的原因（少装一个包）被埋在几十条抓取日志之后。

    **为什么 import 的是 `kb.ai.client` 而不是 `kb.ai`**：`kb/ai/__init__.py`
    刻意把 `AiClient` / `HttpxTransport` 做成惰性导出，好让纯函数单测
    （`parse` / `validate` / `prompts`）在没装 `httpx` 的环境里也能跑（`E6`）。
    所以「依赖是否齐全」这件事必须在**真实实现模块**上验，而不是包入口。

    错误文案必须含**包名**与 `pip install httpx`：用户非技术背景，
    「ImportError: No module named 'httpx'」对他是无信息量的一句话。
    """
    try:
        import kb.ai.client  # noqa: F401 只验证可导入
        import kb.ai.transport  # noqa: F401 同上
    except ImportError as exc:
        sys.stderr.write(
            "\n[worker] AI 层缺少依赖：httpx\n"
            f"  原始错误：{exc}\n"
            "  修复：pip install httpx\n"
            "  （安装后重启 worker；在此之前不会处理摘要任务，"
            "已有条目保持待处理状态）\n\n"
        )
        sys.exit(2)


def build_ai_client() -> Any:
    """构造真实 AI 客户端。**配置是否齐备在每轮任务里现判**，不在启动时判。

    启动时判会把「未配置」变成一个启动错误，而 `AI-04` 要求的是
    「内容照常入库、不报错、不发请求」——用户可能后补配置（`E4`）。
    所以这里无条件构造客户端，把「该不该调用」留给 `steps.run_summarize`。
    """
    from kb.ai import AiConfig, build_client

    # 这里的 AiConfig 只是占位：真正的配置每轮从 `settings` 现读
    # （`ai_runtime.load_config`），客户端持有的那份会被替换。
    # 传给 `build_client` 是为了让装配有唯一入口，不在这里 new 实现类。
    return build_client(AiConfig(base_url="", api_key="", model=""))


def build_media_stage() -> Any:
    """构造媒体阶段装配件。**缺 Pillow/httpx 时不让 worker 起不来。**

    `PillowEncoder` 与 `HttpxDownloader` 的库都在方法体内 import
    （`encode.normalize` / `fetch.get`），所以这里构造永远不会抛——缺包的失败
    会落在真正下载那一刻，带明确原因进 `props.mediaError`。

    这条口径与 `ensure_ai_available()` 不同，而且不一致是**有意的**：
    AI 缺 httpx 时整个摘要步骤都跑不了，早退一次比「任务一直 pending」好排障；
    而媒体缺包只影响封面，正文摘要照常能用——为一张封面拒绝启动是过度反应。
    """
    from kb.media import persist
    from kb.media.config import media_root_from_env

    return persist.production_stage(media_root_from_env())


def build_indexer() -> Any:
    """构造索引写入器（R6）。**构造不联网**，第一次 `upsert` 才发请求。

    为什么不让启动阶段探一次 Meili：Meili 与 worker 都在同一个 proot 容器里，
    但 supervisor 的启动顺序不保证——worker 先起来时探活必然失败，
    而那是**正常的启动顺序**，不是故障。让它失败会把「服务还没起」变成
    「worker 启动失败」，进而被 supervisor 反复重启，Meili 反而更晚起来。

    失败留给 `run_index` 按三类分级处理：挂 → defer 60 秒、
    401 → defer 600 秒 + 提示检查 `KB_MEILI_KEY`、4xx → mark_failed。
    """
    from kb.search.client import client_from_env
    from kb.search.indexer import Indexer

    return Indexer(client_from_env())


# 心跳间隔。必须小于 api 侧 WORKER_STALE_AFTER（90 秒），
# 否则 worker 明明在跑也会被报成 false。留 3 倍余量给偶发的慢抓取。
HEARTBEAT_SECONDS = 30.0


def write_heartbeat(conn: Any, at: datetime) -> None:
    """把当前时间写进 settings。

    用 upsert 而不是 update：首次启动时这个键还不存在。
    值存 ISO 字符串而不是 {"at": ...}——health 的 `parse_heartbeat` 两种都认，
    但纯字符串少一层结构，将来改字典形状也不会破坏历史数据。
    """
    settings.set_value(conn, WORKER_HEARTBEAT_KEY, at.isoformat())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行。`argv=None` 表示读 sys.argv——交给 argparse 自己处理，
    不要自己填 []（`kb/api/__main__.py` 踩过：那样 supervisor 传的参数会被整个吞掉）。
    """
    parser = argparse.ArgumentParser(prog="python -m kb.worker")
    parser.add_argument(
        "--idle",
        type=float,
        default=2.0,
        help="队列空时每轮等待秒数",
    )
    parser.add_argument("--log-level", default="info")
    return parser.parse_args(argv)


def default_extractor() -> Any:
    """生产用的抓取器：把 ParseHub 单例接进 `router.extract`。

    **不能直接返回 `extract`。** `extract(url, *, hub=None)` 在不传 `hub` 时
    会返回「抓取器未注入，无法抓取」——真机踩过：worker 每一条任务都失败，
    而单测全绿（因为只验了 callable，没验参数）。

    这里包一层，把 `parsehub_adapter.parse` 作为 hub 传进去。包一层还有个好处：
    `run_once` 只需要 `Callable[[str], Coroutine]` 这么一个窄契约，
    不需要知道 hub 的存在。

    单例由 `parsehub_adapter` 内部维护——R3 实测：首次冷启 18.49 秒，
    复用单例后 0.83 秒。每条任务新建一个 hub 会把 worker 拖垮。
    """
    from kb.extract.parsehub_adapter import parse

    class _Hub:
        async def parse(self, url: str) -> object:
            return await parse(url)

    async def _extractor(url: str) -> Any:
        return await extract(url, hub=_Hub())

    return _extractor


def _heartbeat_loop(pool: Pool, stop: threading.Event) -> None:
    """独立线程写心跳。

    为什么不在主循环里写：一次抓取要 10–20 秒（R3 实测），主循环被占住时
    心跳就断了，health 会误判 worker 死亡。心跳必须与干活解耦。
    """
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            with pool.connection() as conn:
                write_heartbeat(conn, datetime.now(timezone.utc))
        except Exception:  # noqa: BLE001 心跳失败不该让 worker 退出，库可能只是瞬断
            log.exception("心跳写入失败")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # B27：必须在写心跳、进循环之前就确认 AI 层可用。缺 httpx 时
    # 早退一次，比「领到第一条任务才崩」好排障得多——后者表现为
    # 任务一直 pending 而 health 报 workerOk:true。
    ensure_ai_available()
    ai_client = build_ai_client()
    media_stage = build_media_stage()
    indexer = build_indexer()

    pool = Pool(PgConfig.from_env())
    stop = threading.Event()

    # 启动时清一次 `.incoming/`：上次被 kill 留下的 `.part` 与正式文件同目录，
    # `attachments.stats()` 看不到，会悄悄吃掉磁盘。失败不阻止 worker 起来。
    try:
        from kb.media import persist as media_persist

        cleaned = media_persist.clean_incoming(media_stage.media_root)
        if cleaned:
            log.info("清理媒体临时文件 %d 个", cleaned)
    except Exception:  # noqa: BLE001 启动清理失败不影响 worker 干活
        log.exception("媒体临时文件清理失败")

    # 先写一次心跳再启动循环：否则最长要等 30 秒 health 才转绿，
    # 那段时间里运维会以为 worker 没起来。
    try:
        with pool.connection() as conn:
            write_heartbeat(conn, datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001 起不来也要继续尝试，库可能稍后才可用
        log.exception("首次心跳失败，继续启动")

    hb = threading.Thread(target=_heartbeat_loop, args=(pool, stop), daemon=True)
    hb.start()

    # M4-03：回收站清扫。独立线程、独立连接，与主循环解耦——
    # 与心跳同一理由：一次清扫要删几百行，占住主循环会让任务队列停摆。
    # 启动时先跑一次（`purge_loop` 内部），否则停机多天后要等一小时才开始清。
    purge_thread = threading.Thread(
        target=purge_loop, args=(pool, stop, indexer), daemon=True
    )
    purge_thread.start()

    def _on_signal(_signum: int, _frame: object) -> None:
        log.info("收到退出信号，等待当前任务收尾")
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _on_signal)

    log.info("worker 启动，idle=%.1fs 心跳=%.0fs", args.idle, HEARTBEAT_SECONDS)
    log.info("媒体目录：%s", media_stage.media_root)
    log.info("检索索引：%s（服务地址 %s）", indexer.index_uid, indexer.client.base_url)
    try:
        run_forever(
            pool,
            default_extractor(),
            idle_seconds=args.idle,
            ai_client=ai_client,
            media_stage=media_stage,
            indexer=indexer,
        )
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        pool.close_all()
        log.info("worker 已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())

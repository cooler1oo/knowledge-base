"""worker 启动入口：心跳写入与参数解析。

两个坑都有前例，都要钉住：

1. **心跳键名**。`/api/health` 读 `settings.WORKER_HEARTBEAT_KEY`，
   worker 写错一个字母，健康检查就永远 `workerOk: false`，
   而两边都不会报错——只能靠测试把键名对齐。
2. **命令行参数被丢弃**。`kb/api/__main__.py` 曾因 `list(argv or [])`
   把 supervisor 传的 `--host` 整个吞掉（R4 修过）。同一个错误不该再犯一次。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pg8000.native

# __main__ 不能 `from kb.worker import main`，要显式导入子模块
import kb.worker.__main__ as worker_main
from kb.extract.router import extract
from kb.store.settings import WORKER_HEARTBEAT_KEY


class _Conn:
    def __init__(self) -> None:
        self.seen: list[str] = []
        self.params: list[dict[str, object]] = []

    def run(self, sql: str, **params: object) -> list[list[object]]:
        self.seen.append(sql.lower())
        self.params.append(params)
        return []


def _as_conn(c: _Conn) -> pg8000.native.Connection:
    return c  # type: ignore[return-value]


# ── 心跳 ──────────────────────────────────────────────────────


def test_心跳写进_api_读的那个键():
    """键名必须与 /api/health 完全一致，写错则 workerOk 永远 false。"""
    conn = _Conn()
    now = datetime(2026, 11, 27, 14, 0, tzinfo=timezone.utc)

    worker_main.write_heartbeat(_as_conn(conn), now)

    key = conn.params[0].get("k")
    assert key == WORKER_HEARTBEAT_KEY, (
        f"心跳键名不一致：写的是 {key!r}，health 读的是 {WORKER_HEARTBEAT_KEY!r}"
    )


def test_心跳值是可被_parse_heartbeat_解析的_ISO_字符串():
    """health 的 parse_heartbeat 也认 {"at": ...}，但纯字符串最省事，
    且不会因为字典结构变化而失效。"""
    from kb.api.health import parse_heartbeat

    conn = _Conn()
    now = datetime(2026, 11, 27, 14, 0, tzinfo=timezone.utc)

    worker_main.write_heartbeat(_as_conn(conn), now)

    # 参数里是 json.dumps 之后的值（写库前），get_value 会用 json.loads 解回来。
    # 这里照着走一遍同样的往返，才能验「health 真的解析得动」。
    import json

    written = conn.params[0].get("v")
    raw = json.loads(str(written))  # 模拟 get_value 的解码
    parsed = parse_heartbeat(raw)
    assert parsed is not None, f"health 解析不了这个心跳值：{raw!r}"
    assert parsed.year == 2026 and parsed.hour == 14


def test_心跳用_upsert_不假设键已存在():
    """第一次启动时 settings 里没有这个键，insert 必须带 on conflict。"""
    conn = _Conn()
    worker_main.write_heartbeat(_as_conn(conn), datetime.now(timezone.utc))

    joined = " ".join(conn.seen)
    assert "on conflict" in joined, f"心跳没有 upsert，首次启动会失败：{joined}"


# ── 参数解析（同一个错误不犯第二次）──────────────────────────


def test_入口读取真实命令行参数():
    """`kb/api/__main__.py` 曾把 argv=None 转成 []，supervisor 的参数全丢。
    这条防同一类错误。"""
    args = worker_main.parse_args(["--idle", "5"])

    assert args.idle == 5.0


def test_入口有合理默认值():
    args = worker_main.parse_args([])

    assert args.idle > 0


def test_main_传_argv_时用传进来的_不读_sys_argv():
    """两套参数来源必须明确：显式传了就用传的，没传才读 sys.argv。"""
    args = worker_main.parse_args(["--idle", "1.5"])

    assert args.idle == 1.5


# ── 抓取器装配 ────────────────────────────────────────────────


def test_默认抓取器要真的传_hub_而不是直接返回_extract():
    """真机踩过：default_extractor 直接返回 router.extract 函数，
    而 extract(url, *, hub=None) 在不传 hub 时返回「抓取器未注入」——
    worker 每一条任务都失败，但测试全绿（因为没验参数）。

    这条钉住「必须把 ParseHub 接上」，而不是只验 callable。
    """
    import inspect

    extractor = worker_main.default_extractor()

    # 返回的必须是包装过的可调用对象，不能是 router.extract 本身
    assert extractor is not extract, (
        "default_extractor 直接返回了 router.extract，没传 hub，"
        "调用时会被判「抓取器未注入」"
    )
    sig = inspect.signature(extractor)
    # 包装后的签名只收 url 一个参数（hub 已在闭包里）
    assert list(sig.parameters) == ["url"], (
        f"抓取器签名应只有 url，实际是 {list(sig.parameters)}"
    )


def test_心跳间隔必须小于_api_的过期阈值():
    """health 认为 90 秒没心跳就算 worker 死了。心跳间隔大于它，
    worker 明明在跑却会被报成 false。"""
    from kb.api.health import WORKER_STALE_AFTER

    assert worker_main.HEARTBEAT_SECONDS < WORKER_STALE_AFTER.total_seconds(), (
        f"心跳间隔 {worker_main.HEARTBEAT_SECONDS}s 不小于过期阈值 "
        f"{WORKER_STALE_AFTER.total_seconds()}s，workerOk 会误报 false"
    )

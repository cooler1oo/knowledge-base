"""pg8000 连接与极简连接池。

不引第三方池：单用户场景下池只负责复用连接，不负责扛并发。
不做借出探活：连接坏了就在异常路径丢弃重建，比每条语句多一次往返便宜。
"""

from __future__ import annotations

import queue
import threading
import time
from contextlib import contextmanager
from typing import Iterator

import pg8000.exceptions
import pg8000.native

from .config import PgConfig

# 瞬时连不上是这台设备上真实发生过的事故（2026-11-26 21:54:48，16 次连接
# 在 70ms 内全部被拒，PG 侧一条日志都没有）。事后唯一的线索就是错误码与耗时：
#   连接被拒（毫秒级）  → 端口没有服务在听，或路径被主动拒绝
#   主机/网络不可达     → 路由没了（Tailscale 断开、网卡换了）
#   超时（秒级）        → SYN 被黑洞，多半是防火墙丢包
# 所以这里必须把 errno 和耗时拼进异常，不能只留一句「连不上」。
_ERRNO_HINT = {
    111: "连接被拒：对方端口没有服务在监听",
    10061: "连接被拒：对方端口没有服务在监听（Windows）",
    101: "网络不可达：本机到对方没有路由",
    10051: "网络不可达：本机到对方没有路由（Windows）",
    113: "主机不可达：路由存在但对端不可达",
    10065: "主机不可达：路由存在但对端不可达（Windows）",
    110: "连接超时：SYN 无响应，像被防火墙丢包",
    10060: "连接超时：SYN 无响应，像被防火墙丢包",
}


class PgUnreachable(RuntimeError):
    """连不上数据库。带 errno、耗时、目标地址，便于区分「本机问题」与「网络问题」。"""

    def __init__(
        self, cfg: PgConfig, errno: int | None, elapsed_ms: float, raw: str
    ) -> None:
        self.errno = errno
        self.elapsed_ms = elapsed_ms
        hint = (
            _ERRNO_HINT.get(errno, "错误码未知") if errno is not None else "错误码未知"
        )
        super().__init__(
            f"{cfg.host}:{cfg.port} 连不上（耗时 {elapsed_ms:.0f}ms，"
            f"errno={errno}，{hint}）：{raw}"
        )


def connect(cfg: PgConfig) -> pg8000.native.Connection:
    started = time.monotonic()
    try:
        return pg8000.native.Connection(**cfg.params)
    except pg8000.exceptions.InterfaceError as exc:
        # pg8000 把底层 OSError 包成 InterfaceError，原始错误码在 __cause__ 上
        cause = exc.__cause__
        errno = getattr(cause, "errno", None)
        raise PgUnreachable(
            cfg, errno, (time.monotonic() - started) * 1000, str(exc)
        ) from exc


class Pool:
    def __init__(self, cfg: PgConfig, size: int = 5) -> None:
        self._cfg = cfg
        self._size = size
        self._free: queue.LifoQueue[pg8000.native.Connection] = queue.LifoQueue()
        self._lock = threading.Lock()

    @contextmanager
    def connection(self) -> Iterator[pg8000.native.Connection]:
        try:
            conn = self._free.get_nowait()
        except queue.Empty:
            with self._lock:
                conn = connect(self._cfg)

        try:
            yield conn
        except BaseException:
            # 出错说明事务状态未知，这条连接不能再给别人用
            _close_quietly(conn)
            raise

        if self._free.qsize() < self._size:
            self._free.put(conn)
        else:
            _close_quietly(conn)

    def close_all(self) -> None:
        while True:
            try:
                _close_quietly(self._free.get_nowait())
            except queue.Empty:
                return


def _close_quietly(conn: pg8000.native.Connection) -> None:
    try:
        conn.close()
    except Exception:
        pass

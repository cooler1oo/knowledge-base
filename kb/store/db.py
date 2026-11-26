"""pg8000 连接与极简连接池。

不引第三方池：单用户场景下池只负责复用连接，不负责扛并发。
不做借出探活：连接坏了就在异常路径丢弃重建，比每条语句多一次往返便宜。
"""

from __future__ import annotations

import queue
import threading
from contextlib import contextmanager
from typing import Iterator

import pg8000.native

from .config import PgConfig


def connect(cfg: PgConfig) -> pg8000.native.Connection:
    return pg8000.native.Connection(**cfg.params)


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

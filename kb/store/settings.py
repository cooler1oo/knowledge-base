"""settings 键值表的读写。

worker 心跳（`worker_heartbeat`）就存在这张表里，健康检查读它判断 worker 是否活着。
值统一按 JSON 存：`value jsonb`。
"""

from __future__ import annotations

import json
from typing import Any

import pg8000.native

WORKER_HEARTBEAT_KEY = "worker_heartbeat"


def get_value(conn: pg8000.native.Connection, key: str) -> Any:
    rows = conn.run("select value from settings where key = :k", k=key)
    if not rows:
        return None
    value = rows[0][0]
    # pg8000 对 jsonb 有时给 Python 对象、有时给字符串，两种都认
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def set_value(conn: pg8000.native.Connection, key: str, value: Any) -> None:
    conn.run(
        "insert into settings (key, value) values (:k, :v::jsonb) "
        "on conflict (key) do update set value = excluded.value, updated_at = now()",
        k=key,
        v=json.dumps(value, ensure_ascii=False),
    )

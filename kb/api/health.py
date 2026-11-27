"""五项健康检查。

口径按技术方案 §8.2：

```
{ "ok": true, "dbOk": true, "meiliOk": true, "workerOk": true,
  "diskFreeGb": 42.1, "workerHeartbeatAt": "..." }
```

`workerOk` 必须有真实写入方（worker 每 30s 写 `settings('worker_heartbeat')`）。
本轮还没有 worker，所以它就该是 `false`——**不许为了变绿而写死 true**，
否则这个字段永远通过，等于没有。
"""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kb.store import settings
from kb.store.db import Pool
from kb.store.settings import WORKER_HEARTBEAT_KEY

WORKER_STALE_AFTER = timedelta(seconds=90)
GB = 1024**3


def db_ok(pool: Pool) -> bool:
    try:
        with pool.connection() as conn:
            return int(conn.run("select 1")[0][0]) == 1
    except Exception:
        return False


def meili_ok(base_url: str, timeout: float = 1.5) -> bool:
    """只认 `{"status":"available"}`。连不上、超时、字段变了都算不可用。"""
    url = base_url.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False
    # 先确认是对象再取字段：返回数组或裸字符串时 .get 会抛 AttributeError，
    # 而上面那几个 except 抓不到它，健康检查会变成 500。
    if not isinstance(payload, dict):
        return False
    return str(payload.get("status")) == "available"


def worker_heartbeat_at(pool: Pool) -> datetime | None:
    try:
        with pool.connection() as conn:
            raw = settings.get_value(conn, WORKER_HEARTBEAT_KEY)
    except Exception:
        return None
    return parse_heartbeat(raw)


def parse_heartbeat(raw: object) -> datetime | None:
    """心跳可能是纯 ISO 字符串，也可能是 {"at": "..."}，两种都认。"""
    if isinstance(raw, dict):
        raw = raw.get("at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:  # 不带时区的当成本地时间是错的，一律按 UTC
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def disk_free_gb(path: Path) -> float:
    """取 path 所在分区的剩余空间；path 不存在就往上找第一个存在的祖先。"""
    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    return round(shutil.disk_usage(target).free / GB, 1)


def check(pool: Pool, meili_url: str, media_root: Path) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    heartbeat = worker_heartbeat_at(pool)
    worker_alive = heartbeat is not None and now - heartbeat < WORKER_STALE_AFTER
    database = db_ok(pool)
    search = meili_ok(meili_url)
    return {
        "ok": database and search,
        "dbOk": database,
        "meiliOk": search,
        "workerOk": worker_alive,
        "diskFreeGb": disk_free_gb(media_root),
        "workerHeartbeatAt": heartbeat.isoformat() if heartbeat else None,
    }

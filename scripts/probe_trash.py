"""回收站健康探针（M4-03 T04）。

一条命令看三件事：**回收站有多深、有没有该清没清的、磁盘上有多少孤儿文件**。

    python -m scripts.probe_trash
    python -m scripts.probe_trash --json     # 给监控/脚本用

## 三个数字各自回答什么

| 输出 | 回答的问题 |
|---|---|
| `trash` / `oldest_days` | 回收站现在有多少条、最老的多久了 |
| `overdue` | 超过 30 天还没被清掉的（清扫线程没跑或跑挂了） |
| `disk_orphans` | **磁盘上有、库里已经没有**的媒体文件占了多少字节 |

## ⚠️ `disk_orphans` 是估算，不是精确值

本轮**不做文件 GC**：`purge` 只删行，`media_root` 下的文件还在，成为无人
引用的孤儿。做 GC 要让 store 碰磁盘（破坏「store 只读写库」的边界），
或者给 `jobs` 加第六步（要一次迁移）。设计 §9 明确留到后续轮次。

所以这里只**量化**：`du -sb media_root` 与 `sum(attachments.size_bytes)` 的
差值就是「目录里比库记录多出来的部分」。差值包括：

- 真正被 purge 掉行之后留下的文件（用户可见的浪费）
- `.incoming/` 里的临时文件（`clean_incoming` 会清，但只在 worker 启动时跑）
- 同一份文件被多条附件引用时的重复计数（`attachments` 允许同 `storage_key`
  多行吗？查过：`storage_key` 有唯一索引，所以不会重复）

**这是一个上界估计**：真实孤儿 ≤ 这个差值。用它做趋势监控足够，别拿它当
「删掉就省这么多」的承诺。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from kb.store import items as items_store
from kb.store.config import PgConfig
from kb.store.db import connect

#: 过期判定。与 `items.TRASH_RETENTION_DAYS` 同一个来源，不另写字面量。
OVERDUE_DAYS = items_store.TRASH_RETENTION_DAYS


def _media_root() -> Path:
    """媒体根目录。环境变量与 `kb/api/app.py` 用的是同一个名字。"""
    import os

    return Path(os.environ.get("KB_MEDIA_ROOT", "") or "./data/media")


def _du_bytes(path: Path) -> int:
    """目录实际占用字节数。`du -sb` 与 Python 遍历的差别见 docstring。

    用 `du -sb` 而不是 `sum(p.stat().st_size for p in rglob())`：
    后者会跳过目录本身占的块、也拿不到稀疏文件的真实占用。而 `du` 是
    「磁盘上真的占了多少」，正是要回答的问题。

    `du -sb` 在 GNU coreutils 上有 `-b`（apparent size），BusyBox 上可能
    不认——那就退到纯 Python 遍历，并在输出里标注 `method` 让读的人知道
    这个数字的口径变过。
    """
    if not path.exists():
        return 0
    try:
        out = subprocess.run(
            ["du", "-sb", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return int(out.stdout.split()[0])
    except (subprocess.SubprocessError, ValueError, IndexError):
        total = 0
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
        return total


def collect(conn: Any, *, media_root: Path | None = None) -> dict[str, object]:
    """采集三项指标。**不抛异常**（探针要能在一个坏掉的库上仍然输出）。

    返回的 dict 就是要打印/序列化的东西，形状固定——监控脚本可以直接
    按 key 取值，不用解析文本。
    """
    root = media_root if media_root is not None else _media_root()

    # ① 回收站现状
    total = items_store.count_trash(conn)
    oldest = conn.run(
        "select extract(epoch from (now() - min(deleted_at))) / 86400 "
        "from items where deleted_at is not null"
    )
    oldest_days = (
        round(float(str(oldest[0][0])), 1)
        if oldest and oldest[0][0] is not None
        else 0.0
    )

    # ② 过期的（清扫线程该清而没清的）
    overdue_rows = conn.run(
        "select count(*) from items "
        "where deleted_at is not null "
        "  and deleted_at < now() - make_interval(days => cast(:d as int))",
        d=OVERDUE_DAYS,
    )
    overdue = int(str(overdue_rows[0][0])) if overdue_rows else 0

    # ③ 磁盘 vs 库记录
    disk_bytes = _du_bytes(root)
    db_rows = conn.run("select coalesce(sum(size_bytes), 0) from attachments")
    db_bytes = int(str(db_rows[0][0])) if db_rows else 0

    return {
        "trash": total,
        "oldest_days": oldest_days,
        "overdue": overdue,
        "overdue_threshold_days": OVERDUE_DAYS,
        "media_root": str(root),
        "disk_bytes": disk_bytes,
        "db_bytes": db_bytes,
        # 上界估计：真实孤儿 ≤ 这个差值（见模块 docstring）
        "disk_orphans": max(0, disk_bytes - db_bytes),
        "disk_orphans_is_upper_bound": True,
    }


def render(data: dict[str, object], *, as_json: bool) -> str:
    if as_json:
        return json.dumps(data, ensure_ascii=False, indent=2)

    lines = [
        "回收站探针（M4-03）",
        "─" * 46,
        f"回收站条数          {data['trash']}",
        f"最老的已删多久      {data['oldest_days']} 天",
        f"超过保留期未清      {data['overdue']}（阈值 {data['overdue_threshold_days']} 天）",
        "",
        f"媒体目录            {data['media_root']}",
        f"磁盘占用            {_human(int(str(data['disk_bytes'])))}",
        f"库记录合计          {_human(int(str(data['db_bytes'])))}",
        f"疑似孤儿（上界）    {_human(int(str(data['disk_orphans'])))}",
    ]

    # 只在这两种情况下给一句判读——探针的价值是「看到数字就知道要不要管」，
    # 把三个数字原样吐出来让人自己算，等于没做。
    if int(str(data["overdue"])) > 0:
        lines.append("")
        lines.append(
            f"⚠️ 有 {data['overdue']} 条超过 {OVERDUE_DAYS} 天未清理。"
            "检查 worker 的清扫线程是否在跑（日志搜「回收站自动清理」）。"
        )
    if int(str(data["disk_orphans"])) > 100 * 1024 * 1024:
        lines.append("")
        lines.append(
            f"⚠️ 疑似孤儿文件超过 100MB（{_human(int(str(data['disk_orphans'])))}）。"
            "本轮不做文件 GC；需要时手工核对 media_root 与 attachments 表。"
        )

    return "\n".join(lines)


def _human(size: int) -> str:
    """人类可读的字节数。

    ⚠️ 除法要用浮点：`size = int(size / 1024)` 在每一级都截断，1234567 会
    变成 "1.0 MB"（真实是 1.2 MB）——差值刚好落在 100MB 阈值附近时，
    这种截断会让告警漏报或误报。
    """
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回收站健康探针")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    cfg = PgConfig.from_env()
    try:
        conn = connect(cfg)
    except Exception as exc:  # noqa: BLE001 探针的失败要说人话，不要 traceback
        print(
            f"连不上数据库（{cfg.host}:{cfg.port}/{cfg.database}）：{exc}",
            file=sys.stderr,
        )
        return 2

    try:
        data = collect(conn)
    finally:
        conn.close()

    print(render(data, as_json=args.json))
    return 0


if __name__ == "__main__":
    sys.exit(main())

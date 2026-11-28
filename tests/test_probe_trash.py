"""回收站探针（M4-03 T04）。

## 探针的三项指标各自防什么

| 输出 | 防的问题 |
|---|---|
| `trash` / `oldest_days` | 回收站涨起来了但没人注意（`oldest_days` 超过 30 说明清扫失效） |
| `overdue` | 清扫线程没跑或跑挂了——这是**必须有人管**的信号 |
| `disk_orphans` | 物理删只删行，`media_root` 下的文件成了孤儿（本轮不做 GC，只量化） |

## 为什么探针值得一堆用例

它是**运维唯一会看的那个数**。探针自己算错了，比没有探针更糟：一个永远
显示 `overdue: 0` 的探针会让人以为清扫在跑，而实际三个月没清过。
所以 `overdue` 的判定条件必须有独立用例守着。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import probe_trash

NOW = datetime(2026, 11, 28, 15, 0, tzinfo=timezone.utc)


class FakeConn:
    """按 SQL 特征返回预置值。

    `trash_count` / `oldest_days` / `overdue` / `db_bytes` 四个直接摆出来，
    用例读起来就是「这种情况该报什么」。
    """

    def __init__(
        self,
        *,
        trash_count: int = 0,
        oldest_days: float | None = None,
        overdue: int = 0,
        db_bytes: int = 0,
    ) -> None:
        self.trash_count = trash_count
        self.oldest_days = oldest_days
        self.overdue = overdue
        self.db_bytes = db_bytes
        self.seen: list[str] = []

    def run(self, sql: str, **params: object) -> list[list[object]]:
        low = " ".join(sql.split()).lower()
        self.seen.append(low)

        # ⚠️ 顺序有意义：overdue 那条也含 `deleted_at is not null`，
        # 若先匹配「回收站条数」就会把它也吃掉，`overdue` 永远是 0——
        # 而一个永远报 0 的探针会让人以为清扫在跑。
        if "deleted_at < now() - make_interval" in low:
            return [[self.overdue]]
        if "min(deleted_at)" in low:
            return [[self.oldest_days]]
        if "count(*) from items where deleted_at is not null" in low:
            return [[self.trash_count]]
        if "from attachments" in low:
            return [[self.db_bytes]]
        return []


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    """一个空媒体目录。用例往里写文件来造「磁盘比库多」的情形。"""
    return tmp_path / "media"


# ── collect 的三项指标 ──────────────────────────────────────


def test_空回收站全部为零(media_dir) -> None:
    media_dir.mkdir()
    data = probe_trash.collect(FakeConn(), media_root=media_dir)

    assert data["trash"] == 0
    assert data["oldest_days"] == 0.0
    assert data["overdue"] == 0
    assert data["disk_orphans"] == 0


def test_回收站条数与最老天数(media_dir) -> None:
    media_dir.mkdir()
    data = probe_trash.collect(
        FakeConn(trash_count=3, oldest_days=12.5), media_root=media_dir
    )

    assert data["trash"] == 3
    assert data["oldest_days"] == 12.5


def test_过期判定用items里的保留期常量(media_dir) -> None:
    """**不能另写字面量**：探针报的阈值与清扫线程用的必须是同一个数。

    两处各写 30 的话，改了其中一处就会出现「清扫按 30 天清、探针按 60 天
    报」——探针永远显示 0 过期，而清扫一直在删东西。
    """
    from kb.store import items as items_store

    media_dir.mkdir()
    data = probe_trash.collect(FakeConn(), media_root=media_dir)

    assert data["overdue_threshold_days"] == items_store.TRASH_RETENTION_DAYS


def test_有未清理的过期条目时报出来(media_dir) -> None:
    media_dir.mkdir()
    data = probe_trash.collect(FakeConn(overdue=2), media_root=media_dir)

    assert data["overdue"] == 2
    rendered = probe_trash.render(data, as_json=False)
    assert "2 条超过" in rendered
    assert "清扫线程" in rendered, "要说下一步该查什么"


def test_过期数为0时不给告警(media_dir) -> None:
    media_dir.mkdir()
    rendered = probe_trash.render(
        probe_trash.collect(FakeConn(overdue=0), media_root=media_dir), as_json=False
    )
    assert "⚠️" not in rendered


# ── 磁盘孤儿估算 ────────────────────────────────────────────


def test_磁盘比库多时差值就是孤儿上界(media_dir) -> None:
    """`disk_orphans` 是**上界估计**，不是精确值（模块 docstring 有说明）。

    本轮不做文件 GC（设计 §9 留到后续轮次），探针只负责让它可见。
    """
    media_dir.mkdir()
    (media_dir / "a.jpg").write_bytes(b"x" * 1000)
    (media_dir / "b.jpg").write_bytes(b"x" * 500)

    data = probe_trash.collect(FakeConn(db_bytes=800), media_root=media_dir)

    assert data["disk_bytes"] >= 1500
    # 磁盘 1500+ 、库 800 → 差值至少 700
    assert data["disk_orphans"] == data["disk_bytes"] - 800
    assert data["disk_orphans_is_upper_bound"] is True, (
        "必须标注这是上界，否则读的人会把它当精确值去对账"
    )


def test_库比磁盘多时孤儿数为0不是负数(media_dir) -> None:
    """差值为负说明库里有记录而磁盘上没有文件（`missing` 那一类问题）。

    那是另一个问题（附件丢失），不该让 `disk_orphans` 变成负数——
    负数会让「孤儿总量」这类求和指标失去意义。
    """
    media_dir.mkdir()
    data = probe_trash.collect(FakeConn(db_bytes=99999), media_root=media_dir)

    assert data["disk_orphans"] == 0


def test_媒体目录不存在时不报错(media_dir) -> None:
    """目录没建（还没存过带媒体的内容）是正常状态，不是错误。"""
    assert not media_dir.exists()
    data = probe_trash.collect(FakeConn(), media_root=media_dir)
    assert data["disk_bytes"] == 0


def test_孤儿超过100MB时给告警(media_dir) -> None:
    media_dir.mkdir()
    (media_dir / "big.jpg").write_bytes(b"x" * (101 * 1024 * 1024))

    rendered = probe_trash.render(
        probe_trash.collect(FakeConn(), media_root=media_dir), as_json=False
    )
    assert "疑似孤儿文件超过 100MB" in rendered
    assert "本轮不做文件 GC" in rendered, "要说清这是已知缺口而不是故障"


# ── 输出格式 ────────────────────────────────────────────────


def test_json输出可被机器解析(media_dir) -> None:
    media_dir.mkdir()
    data = probe_trash.collect(FakeConn(trash_count=1), media_root=media_dir)

    parsed = json.loads(probe_trash.render(data, as_json=True))

    # 监控脚本按 key 取值，形状必须稳定
    for key in (
        "trash",
        "oldest_days",
        "overdue",
        "overdue_threshold_days",
        "disk_bytes",
        "db_bytes",
        "disk_orphans",
    ):
        assert key in parsed, f"JSON 少了 {key}"


def test_文本输出含三项指标的标签(media_dir) -> None:
    media_dir.mkdir()
    rendered = probe_trash.render(
        probe_trash.collect(FakeConn(), media_root=media_dir), as_json=False
    )

    assert "回收站条数" in rendered
    assert "最老的已删多久" in rendered
    assert "超过保留期未清" in rendered
    assert "疑似孤儿" in rendered


# ── 人类可读大小 ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (999, "999 B"),
        (1024, "1.0 KB"),
        (1234567, "1.2 MB"),
        (104857600, "100.0 MB"),
        (5 * 1024**3, "5.0 GB"),
    ],
)
def test_人类可读大小(size: int, expected: str) -> None:
    """⚠️ `1234567 → 1.2 MB` 这条是有来历的。

    第一版写 `size = int(size / 1024)`，每一级都截断，结果 1234567 显示成
    "1.0 MB"。差值刚好落在 100MB 告警阈值附近时，这种截断会让告警漏报。
    """
    assert probe_trash._human(size) == expected


# ── 连不上库要说人话 ────────────────────────────────────────


def test_连不上库时返回非零并给可读错误(monkeypatch, capsys) -> None:
    """探针的失败不该是一屏 traceback——运维要能一眼看出连的是哪个库。"""

    def boom(cfg):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(probe_trash, "connect", boom)

    code = probe_trash.main([])

    assert code == 2
    err = capsys.readouterr().err
    assert "连不上数据库" in err
    assert "connection refused" in err

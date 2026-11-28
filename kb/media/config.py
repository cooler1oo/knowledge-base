"""媒体下载的全部数值常量，集中一处。

**为什么单独成文件**：这些数字不是随手写的默认值，每一个都对应一条实测结论
或产品要求。散在 `fetch.py` / `encode.py` / `persist.py` 里，改一个要翻三个
文件核对口径，而「超时 5 秒」在传输层与编排层必须是同一个数——两处各写一次
必然漂移，漂移之后的表现是「偶发超时」，没有日志能指出来。

## 三组数字的来源

| 组 | 值 | 依据 |
|---|---|---|
| 超时 | 连接 5s / 读 10s / 单条 20s / 阶段 90s | 前三个与 `kb/ai/config.py` 同值（同一台设备实测可用）。阶段闸门是必须的另一层：20 张 × 20 秒 = 400 秒的上界不能当真 |
| 体积 | 原始 8 MiB / 单条 16 MiB / 目标 200 KiB | 封面到不了 8 MiB，超过只可能是「签名的 URL 失效后 CDN 返回 HTML 错误页」。目标字节是 PRD §5.5 的产品要求 |
| 图片 | 长边 1600 / q82→q70→缩 0.75 | 卡片显示宽度按 2.5–3 倍 DPR 算是 850–1200 物理像素，1600 留「点开看大图」的余量 |
"""

from __future__ import annotations

import os
from pathlib import Path

# ── 超时（秒）─────────────────────────────────────────────────
#: 单次建连。与 `kb/ai/config.TIMEOUT_CONNECT` 同值。
CONNECT_SEC = 5.0
#: 单次读。与 `kb/ai/config.TIMEOUT_READ` 同值。
READ_SEC = 10.0
#: 单条媒体的总时长。`httpx.Timeout` 没有 `total` 字段（`kb/ai/transport.py`
#: 踩过），所以这个上限由读循环自己计时。
REQUEST_TOTAL_SEC = 20.0
#: 整个媒体阶段的闸门。超出后剩余条目记 skipped 并继续，**不抛异常**。
STAGE_BUDGET_SEC = 90.0

# ── 体积（字节）───────────────────────────────────────────────
#: 单文件原始字节上限。边读边累计判定，不看 `Content-Length`：服务端可以撒一个
#: 100 字节的 Content-Length 然后吐 1 GB（`kb/ai/transport.py` 同一个教训）。
MAX_SOURCE_BYTES = 8 * 1024 * 1024
#: 单条内容的媒体总字节上限。20 张压缩后 ≤200 KiB 是 4 MiB，留 4 倍余量。
ITEM_MAX_BYTES = 16 * 1024 * 1024
#: 归一化压缩的目标字节（PRD §5.5「单张 ≤ 200KB」）。
TARGET_BYTES = 200 * 1024

# ── 图片归一化 ────────────────────────────────────────────────
#: 长边上限。**只缩不放**：640×360 的封面不放到 1600（与 `kb/vision/grab.py`
#: 的 `long_side` 口径一致——上限不是目标尺寸）。
LONG_SIDE = 1600
#: 三轮编码的质量序列。第 3 轮同时把长边乘 `SHRINK_FACTOR`。
JPEG_QUALITIES = (82, 70, 70)
#: 质量降到 70 仍超目标时的缩小比例。
SHRINK_FACTOR = 0.75
#: 编码轮数上限。没有上限时「很小但仍超」的图会无限缩下去。
MAX_ENCODE_ROUNDS = 3

# ── 磁盘 ──────────────────────────────────────────────────────
#: 低于这个剩余量时整个媒体阶段跳过，写 `props.mediaError`，fetch 仍算成功。
MIN_FREE_BYTES = 1024**3
#: 下载中的临时目录。**必须与正式文件同分区**：`os.replace` 跨文件系统会退化成
#: 「复制 + 删除」，既慢又不是原子的。
INCOMING_DIR = ".incoming"

#: 默认媒体根目录**不在这里求值**。写成 `DEFAULT_MEDIA_ROOT = Path.home() / "media"`
#: 会让 `import kb.media` 去读 HOME 环境变量——而在 HOME 缺失的环境里
#: `Path.home()` 直接抛 `RuntimeError("Could not determine home directory.")`。
#:
#: 这不是理论风险：`tests/test_ai_e2e_pg.py` 的 `B27` 用例为了让子进程落到
#: 干净环境，只传了 PATH 与 SYSTEMROOT，于是 `python -m kb.worker` 在
#: **import 阶段**就崩了（RealMachine 实测）。而那个用例要验的是「缺 httpx 时
#: 给友好提示」——它被一个不相干的崩溃抢先了。
#:
#: 所以路径解析放进函数，只在真正要用的时候做。
MEDIA_ROOT_ENV = "KB_MEDIA_ROOT"


def media_root_from_env() -> Path:
    """读 `KB_MEDIA_ROOT`，未设置时用 `~/media`。

    worker 与 api 必须落在同一个目录上，否则 worker 落盘的文件 `/api/media`
    读不到——而两边的错误表现分别是「有记录但 404」和「一切正常」，
    最难查的那一类。所以这个函数是两处共用的唯一入口。

    `Path.home()` 可能抛 `RuntimeError`（HOME 与 USERPROFILE 都不可用）。
    到那时退回当前工作目录下的 `media`：一个能跑但位置可能不对的目录，
    好过让进程起不来——而位置不对这件事由启动日志里的那行「媒体目录：…」
    暴露出来。
    """
    raw = os.environ.get(MEDIA_ROOT_ENV, "").strip()
    if raw:
        return Path(raw)
    try:
        return Path.home() / "media"
    except RuntimeError:
        return Path.cwd() / "media"

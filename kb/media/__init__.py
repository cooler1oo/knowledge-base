"""媒体层：把第三方 URL 变成磁盘上的字节，再交给 `kb/store` 落库。

## 为什么单独一层

三条理由，每条都对应一个已有的架构决定：

1. **`kb/extract/` 出的是「URL 列表」，本层出的是「磁盘上的字节」。** 混在一起会让
   「抓取失败」与「下载失败」共用一套降级口径，而两者该分开：抓取失败要标
   `fetchError` 让用户重试，封面下载失败不该——那张图不影响这条内容可读。
   这与 `kb/vision/` 与 `kb/asr/` 分开的理由同构。
2. **本层必须能脱离数据库单测。** `layer_rules.toml` 登记了「`kb/media/` 禁
   `import kb.store`」，落库由 worker 经 `kb.store.attachments` 做。
   **这条必须显式登记**——不登记时只剩「写库 SQL」与「直连驱动」两条规则生效，
   `import kb.store` 会静默漏检。
3. **写库 SQL 只能在 `kb/store/`。** 所以下载与落库必然分属两层，
   不存在「合成一个文件更简单」的选项。

## 输入输出

```python
specs: list[MediaSpec] = plan.plan_attachments(media, kind="video")
result: MediaResult   = persist.store_media(specs, item_id=..., media_root=..., ...)
rows: int             = attachments.replace_for_item(conn, item_id, list(result.stored))
```

本层不认识 `kb.store`，也不认识 `kb.extract`：输入是 `list[dict]`
（与 `store.items.apply_extract` 收 `media: list[dict]` 的口径一致），
输出是 `list[StoredBlob]`。换抓取实现时本层一行都不用改。

## 外部库都在方法体内 import

`httpx`（`fetch.py`）与 `PIL`（`encode.py`）一律放方法体内，模块顶层不出现。
这样纯逻辑单测（`layout` / `sniff` / `plan` / `guard`）在没有这些库的环境也能跑。

## 导入顺序警告

`plan` / `sniff` 在顶层被本文件 import 是安全的（它们只依赖标准库）。
`fetch` / `encode` / `persist` **不在这里 import**：`persist` 会在模块级
`from .fetch import ...`，而 `fetch` 顶层没有 httpx（在方法内），所以技术上也安全
——但保持惰性导出能让「只 `import kb.media` 验顶层无硬 import」这条自检更严格。

## 一个必须写下来的事实

媒体下载失败在本轮等于**永久缺失**，没有第二次机会：签名 URL 抓完就丢
（`_outcome_to_kwargs` 只把 `thumbUrl` 传给下载阶段，不存进库），
而 `POST /api/items/{id}/retry` 本轮不存在。这恰好让「不污染 `jobs`」的口径更强
——若因封面 403 就把 job 标 `failed`，条目会永久停在「处理失败」而用户无路可走。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .config import (
    LONG_SIDE,
    MAX_SOURCE_BYTES,
    MIN_FREE_BYTES,
    STAGE_BUDGET_SEC,
    TARGET_BYTES,
    media_root_from_env,
)
from .models import (
    DownloadedBlob,
    MediaResult,
    MediaSpec,
    PinnedTarget,
    SkippedMedia,
    StoredBlob,
)

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查与 IDE 补全
    from .encode import ImageEncoder, PillowEncoder
    from .fetch import Downloader, HttpxDownloader
    from .guard import Resolver
    from .persist import (
        MediaStage,
        clean_incoming,
        production_stage,
        store_media,
        summarize_skipped,
    )

__all__ = [
    "LONG_SIDE",
    "MAX_SOURCE_BYTES",
    "MIN_FREE_BYTES",
    "STAGE_BUDGET_SEC",
    "TARGET_BYTES",
    "DownloadedBlob",
    "Downloader",
    "HttpxDownloader",
    "ImageEncoder",
    "MediaResult",
    "MediaSpec",
    "MediaStage",
    "PillowEncoder",
    "PinnedTarget",
    "Resolver",
    "SkippedMedia",
    "StoredBlob",
    "clean_incoming",
    "media_root_from_env",
    "production_stage",
    "store_media",
    "summarize_skipped",
]

#: 惰性导出的名字 → 定义它的子模块。加新名字只改这张表。
_LAZY: dict[str, str] = {
    "Downloader": ".fetch",
    "HttpxDownloader": ".fetch",
    "ImageEncoder": ".encode",
    "PillowEncoder": ".encode",
    "Resolver": ".guard",
    "MediaStage": ".persist",
    "clean_incoming": ".persist",
    "production_stage": ".persist",
    "store_media": ".persist",
    "summarize_skipped": ".persist",
}


def __getattr__(name: str) -> Any:
    """模块级惰性导出（PEP 562）。`from kb.media import store_media` 时才真正
    import `persist`，于是 `import kb.media` 不触发 `httpx` / `PIL` 的任何加载。

    这不是 monkeypatch：属性访问走这里，`dir(kb.media)` 与 IDE 补全由
    `__all__` 覆盖。写法与 `kb/ai/__init__.py` 一致——那里这么做是为了让
    `kb.worker` 的 import 阶段不拉 httpx，否则 `ensure_ai_available()` 的友好
    提示永远执行不到。本层同理：worker 顶层 import `kb.media` 时不该因为
    缺 Pillow 就整个起不来（缺 Pillow 只影响图片压缩，捕获在 `persist` 里）。
    """
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value  # 缓存，后续访问不再走 __getattr__
    return value

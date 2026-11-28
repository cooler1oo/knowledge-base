"""`storage_key` 的派生与校验、mime → 扩展名映射。**纯函数，不碰磁盘不碰库。**

## 为什么 key 要混入 `item_id`

`attachments_key_uniq` 是 `storage_key` 上的**全局**唯一索引，所以两种朴素方案都不行：

| 方案 | 撞唯一索引 | 重试幂等 | 跨 item 删除 | 结论 |
|---|---|---|---|---|
| 纯内容 sha256 | **会**（两个 item 引用同一张图） | 是 | 共用文件 → 删谁的说不清 | 否 |
| 纯随机 | 不会 | **不会**（重试多一份文件） | 各自独立 | 否 |
| `sha256(item_id + 内容)` | 不会 | 是 | 各自独立 | **采用** |

磁盘上可能存两份同样的字节。在 163 GB 的设备上，为 200 KB 的封面付这点代价，
换掉一整个「跨 item 删除竞态」的问题类别，是划算的。

## 为什么 2 位目录

单目录堆十几万文件时 ext4/xfs 的 `readdir` 明显变慢。2 位十六进制 = 256 个桶，
按每条内容最多 20 个附件、5000 条内容估，单桶约 390 个文件，远低于阈值。
"""

from __future__ import annotations

import hashlib
import re
from typing import Final, Mapping

#: 只允许**小写**十六进制。`..` 里的点不属于 `[0-9a-f]`，无法通过；`/` 只能出现
#: 在第 3 个字符位。这个正则本身就是 `/api/media/{key}` 路径穿越的充分防线。
KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{2}/[0-9a-f]{32}\.[a-z0-9]{1,5}$"
)

#: mime → 扩展名。只登记 `attachments_mime_allowed` 里我们会产出的那几种；
#: 未登记的一律返回 `None`，调用方据此拒绝该 blob。
#:
#: 为什么不允许调用方自己给扩展名：上游 `ext` 是第三方响应里的字符串，
#: 实测出现过空串与不可信值。扩展名只由 mime 决定，这条不给例外。
MIME_EXT: Final[Mapping[str, str]] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/avif": "avif",
}

#: 派生 key 用的哈希长度。34 个 hex = 17 字节，其中 2 位当目录、32 位当文件名。
_KEY_HEX_LEN: Final[int] = 34


def storage_key(item_id: int, content_sha256: str, ext: str) -> str:
    """由 `(item_id, 内容 sha256, ext)` 派生 `storage_key`，形如 `ab/cdef…0123.jpg`。

    混入 `item_id` 的理由见模块 docstring：`attachments_key_uniq` 是全局唯一索引，
    纯内容哈希会让两个 item 的同一张图撞索引，补救只有 `on conflict do nothing`
    ——于是第二个 item 永远没有封面，而字节明明在磁盘上。

    `content_sha256` 不做长度校验也不做格式清洗：它是本层自己算出来的 hex 串，
    传错属编程错误。在这里「宽容」只会把一个 bug 藏起来。
    """
    if not ext:
        raise ValueError("ext 不能为空")
    seed = f"{item_id}:{content_sha256}".encode()
    digest = hashlib.sha256(seed).hexdigest()[:_KEY_HEX_LEN]
    return f"{digest[:2]}/{digest[2:_KEY_HEX_LEN]}.{ext}"


def is_valid_key(key: str) -> bool:
    """只做格式判定，不碰数据库也不碰文件系统。

    `/api/media/{key}` 的第一步调它。格式不符时端点返回 **404 而不是 400**：
    400 会告诉探测者「这个 key 形状对但内容不存在」，等于把端点变成存在性探针。
    """
    return KEY_RE.fullmatch(key) is not None


def ext_for_mime(mime: str) -> str | None:
    """mime → 扩展名。未登记返回 `None`（调用方据此拒绝该 blob）。

    剥掉 `;charset=...` 之后再查表：`sniff` 已经剥过一次，但这里是公开函数，
    不该假设调用方一定传了干净值。
    """
    base = mime.split(";", 1)[0].strip().lower()
    return MIME_EXT.get(base)

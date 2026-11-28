"""图片归一化压缩。`PIL` 在方法体内 import，纯逻辑单测不需要装它。

## 为什么必须真解码再编码

PRD §5.5 要求「封面存储｜统一压缩，单张 ≤ 200KB」。这是**产品要求**，不是优化，
所以必须真解码。Pillow 同时给出宽高，省掉手写 5 种格式的头部解析
（`kb/vision/grab.py` 走 cv2 是因为那里本来就要解码视频帧；这里是纯图片，
Pillow 是唯一同时能做「解码 + 重编码 + 宽高」的选择）。

## 五条口径，每条都有代价

| 项 | 决定 | 代价 |
|---|---|---|
| 只缩不放 | 640×360 的封面不放到 1600 | 放大只会让文件大 10 倍而不多出信息 |
| 不裁剪 | 保留原始比例 | 裁剪会破坏 M7-01 的「不裁切」要求 |
| alpha 铺白底 | 有非透明 alpha 时铺白 | JPEG 不支持 alpha，直接转会把透明区变黑 |
| 动图只取第一帧 | 卡片只需要一张静图 | 保留 GIF 会让单文件体积失控 |
| **不需要处理就原样返回** | 已是 JPEG 且 ≤ 目标字节 且长边 ≤ 1600 → 一个字节都不动 | 见下 |

## 最后一条是幂等的前提，不是省事

如果每次下载都无条件重编码，同一张图两次跑出的字节可能因编码器版本差异而不同，
`sha256` 就变了、`storage_key` 也变了，「重试幂等」这条性质立刻失效——重跑一次
fetch 会多出一份文件、多一行附件。**所以「原样返回」是正确性要求。**

同理，重编码参数必须完全确定（固定质量序列、固定缩放比例、不写时间戳），
不能有任何「按当前时间/随机」的成分。
"""

from __future__ import annotations

from io import BytesIO
from typing import Any, Final, Protocol

from . import config

#: 输出格式。JPEG 是唯一能在「≤200 KiB + 照片类内容」两者间同时成立的格式：
#: PNG 无损存照片会到 2–5 MB。
OUTPUT_FORMAT: Final[str] = "JPEG"


class EncodeError(Exception):
    """这张图处理不了（解码失败、编码失败、压缩 3 轮仍超目标）。

    调用方收成 `SkippedMedia`，**不抛到 worker**：一张解不开的图不该让另外
    19 张一起丢。
    """


class ImageEncoder(Protocol):
    """一个方法。返回 `(可能被重编码的字节, 宽, 高)`。

    不需要处理时原样返回入参——这是幂等的前提（见模块 docstring）。
    宽高在「不需要处理」路径上也必须返回真实值：调用方靠它们写
    `attachments.width/height`，返回 `None` 会让详情页拿不到尺寸。
    """

    def normalize(
        self, data: bytes, *, long_side: int, max_bytes: int
    ) -> tuple[bytes, int | None, int | None]: ...


def _has_visible_alpha(image: Any) -> bool:
    """有没有「真的能被看见」的透明像素。

    只看 `mode in ('RGBA','LA')` 会误判——一张「声明了 alpha 但全不透明」的 PNG
    会被当成需要铺底，于是多做一次无意义的重编码（并且破坏幂等：解码再编码的
    字节与原始字节不同）。所以读 alpha 通道的极值：`(255, 255)` 表示全不透明。

    判不出（异常、模式不支持、`getextrema` 形状不符）时**保守返回 `True`**：
    宁可多做一次铺底，也不要让一张真透明的图变成黑底。
    """
    try:
        extrema = image.getextrema()
    except Exception:  # noqa: BLE001 判不出透明度按「有透明」处理，见 docstring
        return True
    if not isinstance(extrema, tuple) or not extrema:
        return True
    last = extrema[-1]
    if not isinstance(last, tuple) or len(last) != 2:
        return True
    low = last[0]
    return isinstance(low, int) and low < 255


class PillowEncoder:
    """Pillow 的装配。`PIL` 在方法内 import，纯逻辑单测不需要装它。

    无状态，不缓存任何图像对象——`BytesIO` 与 `Image` 都只活在一次调用里。
    缓存会让「同一个 encoder 实例处理 20 张图」时占住 20 张图的内存，
    而 8 GB 平板已经因为并发解码崩过一次（`kb/vision/grab.py` 的 docstring）。
    """

    def normalize(
        self, data: bytes, *, long_side: int, max_bytes: int
    ) -> tuple[bytes, int | None, int | None]:
        """把字节压到「长边 ≤ `long_side` 且 ≤ `max_bytes`」。

        顺序：

        1. 空字节 → `EncodeError`。空响应不可能是图，但公开方法不假设入参。
        2. 解码一次拿尺寸、格式、要不要铺底。
        3. **已是合格 JPEG 且无需铺底** → 原样返回（一个字节都不动）。
        4. 其余解码 → （必要时）铺白底 → 缩到长边上限 → 按质量降档重编。
        """
        if long_side <= 0:
            raise ValueError("long_side 必须是正数")
        if max_bytes <= 0:
            raise ValueError("max_bytes 必须是正数")
        if not data:
            raise EncodeError("空字节，无法编码")

        from PIL import Image

        width, height, fmt, has_alpha = self._probe(data, Image=Image)
        if width <= 0 or height <= 0:
            raise EncodeError(f"图片尺寸不合法（{width}x{height}）")

        if (
            fmt == OUTPUT_FORMAT
            and len(data) <= max_bytes
            and max(width, height) <= long_side
            and not has_alpha
        ):
            return data, width, height

        frame = self._render(data, long_side=long_side, flatten=has_alpha, Image=Image)
        return self._encode(frame, max_bytes=max_bytes)

    def _probe(self, data: bytes, *, Image: Any) -> tuple[int, int, str, bool]:
        """解码一次，取尺寸 / 格式 / 是否要铺底。

        `load()` 必须调：Pillow 是惰性的，`Image.open` 只读文件头，
        损坏的中段要到 `load()` 才暴露——不调就会把一张坏图当成好图放过去。
        """
        try:
            with Image.open(BytesIO(data)) as image:
                image.load()
                width, height = int(image.width), int(image.height)
                fmt = str(image.format or "").upper()
                has_alpha = image.mode in {"RGBA", "LA", "PA"} and _has_visible_alpha(
                    image
                )
        except Exception as exc:  # noqa: BLE001 任何解码失败都是「这张图用不了」
            raise EncodeError(f"图片解码失败：{type(exc).__name__}: {exc}") from exc
        return width, height, fmt, has_alpha

    def _render(self, data: bytes, *, long_side: int, flatten: bool, Image: Any) -> Any:
        """解码 → （必要时）铺白底 → 缩到长边上限。**只缩不放、不裁剪。**

        `thumbnail` 而不是 `resize`：前者只在超限时缩，且保留原始比例
        ——「只缩不放」与「不裁剪」两条口径由这一个方法同时满足。
        """
        try:
            image = Image.open(BytesIO(data))
            image.load()
            image.seek(0)  # 动图只取第一帧；对非动图是空操作
            if flatten:
                image = self._flatten(image, Image=Image)
            image.thumbnail((long_side, long_side), _resampling(Image))
        except EncodeError:
            raise
        except Exception as exc:  # noqa: BLE001 解码或缩放失败都归为「这张图用不了」
            raise EncodeError(f"图片预处理失败：{type(exc).__name__}: {exc}") from exc
        return image

    def _flatten(self, image: Any, *, Image: Any) -> Any:
        """铺白底再转 RGB。

        为什么铺**白**不铺黑：卡片是白底（M7-01），透明区铺白等于「这块没有内容」，
        与卡片背景一致；铺黑会出现一个突兀的黑框。
        """
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background

    def _encode(self, image: Any, *, max_bytes: int) -> tuple[bytes, int, int]:
        """按质量降档重编，仍超目标则缩长边再来一轮，最多 3 轮。

        3 轮的依据（设计 §5.5）：q82 一次、q70 一次、q70 + 长边 ×0.75 一次。
        固定序列而不是自适应搜索，是为了**字节可复现**——自适应会引入
        「上一次跑用了几轮」这种不可见状态，而幂等要求同样的输入必然同样的输出。
        """
        current = image
        for round_index in range(config.MAX_ENCODE_ROUNDS):
            quality = config.JPEG_QUALITIES[
                min(round_index, len(config.JPEG_QUALITIES) - 1)
            ]
            payload = self._save(current, quality=quality)
            if len(payload) <= max_bytes:
                return payload, int(current.width), int(current.height)

            # 最后一轮不再缩：缩完还得再编一次，那就是第 4 轮了
            if round_index == config.MAX_ENCODE_ROUNDS - 1:
                break
            target = max(
                int(max(current.width, current.height) * config.SHRINK_FACTOR), 1
            )
            if target >= max(current.width, current.height):
                # 已缩到 1 像素仍超目标（max_bytes 极小），再缩没有意义
                break
            resized = current.copy()
            resized.thumbnail((target, target), _resampling(_pil_image()))
            current = resized

        raise EncodeError(
            f"压缩 {config.MAX_ENCODE_ROUNDS} 轮后仍超过 {max_bytes} 字节"
            f"（最后 {int(current.width)}x{int(current.height)}）"
        )

    def _save(self, image: Any, *, quality: int) -> bytes:
        """编成 JPEG 字节。

        `optimize=True` 会让 Pillow 多试几组霍夫曼表，体积小 5–10%，
        代价是编码稍慢（200 KB 的图在 ARM 上是毫秒级）。这一项**不引入不确定性**：
        同样的输入与同样的 quality 得到同样的字节，幂等仍然成立。

        不传 `exif` / `comment`：把上游的元数据带进来既增加体积（EXIF 缩略图
        可能比图本身还大），又把第三方可控的字节写进了我们的文件。
        """
        buf = BytesIO()
        try:
            image.save(buf, format=OUTPUT_FORMAT, quality=quality, optimize=True)
        except Exception as exc:  # noqa: BLE001 编码失败一律当作「这张图用不了」
            raise EncodeError(f"JPEG 编码失败：{type(exc).__name__}: {exc}") from exc
        return buf.getvalue()


def _pil_image() -> Any:
    from PIL import Image

    return Image


def _resampling(Image: Any) -> Any:
    """`Image.Resampling.LANCZOS`，Pillow < 9.1 上回落到顶层 `Image.LANCZOS`。

    `LANCZOS` 是缩小的正确插值：`NEAREST` 缩照片会产生锯齿，`BILINEAR`
    会糊掉文字——而封面上常有标题文字。
    """
    resampling = getattr(Image, "Resampling", Image)
    return getattr(resampling, "LANCZOS")

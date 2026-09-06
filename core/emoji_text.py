"""彩色 emoji 混排绘制。

为什么自己实现：pilmoji 2.0.4 只兼容 emoji 1.x，而 AstrBot 运行时自带
emoji>=2。把这两个包写进 requirements.txt 会让 AstrBot 的依赖预检直接
判定冲突并拒绝加载插件（module 'emoji.unicode_codes' has no attribute
'get_emoji_unicode_dict'）。因此这里改为零额外依赖的实现：

1. 用正则把文本切成「emoji 簇 / 普通文本」两类片段（不依赖 emoji 包）；
2. emoji 簇交给系统彩色 emoji 字体，用 Pillow 的 embedded_color 绘制；
3. 没有彩色字体时，直接用正文字体画原字符（可能是黑白或方块，可接受）。

顺带修掉上游两个排版问题：
- 换行按「簇」而不是按码位，ZWJ 组合 emoji（如 👨‍👩‍👧）不会被拆散；
- 测量与绘制走同一套步进逻辑，不再需要给 emoji 两侧补空格来防重叠。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from PIL import Image, ImageDraw, ImageFont, features

#: 绝大多数彩色 emoji 字形所在的码位区间。
#: 刻意排除纯文字性质的符号区（箭头、几何图形等），避免把「→」「■」当 emoji。
_BASE = (
    "\u00a9\u00ae\u203c\u2049\u2122\u2139"
    "\u2194-\u21aa"
    "\u231a-\u231b\u2328\u23cf\u23e9-\u23f3\u23f8-\u23fa"
    "\u24c2"
    "\u25aa-\u25ab\u25b6\u25c0\u25fb-\u25fe"
    "\u2600-\u27bf"
    "\u2934-\u2935"
    "\u2b05-\u2b07\u2b1b-\u2b1c\u2b50\u2b55"
    "\u3030\u303d\u3297\u3299"
    "\U0001f000-\U0001f0ff"
    "\U0001f10d-\U0001f251"
    "\U0001f300-\U0001f5ff"
    "\U0001f600-\U0001f64f"
    "\U0001f680-\U0001f6ff"
    "\U0001f700-\U0001f7ff"
    "\U0001f7e0-\U0001f7eb"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001faff"
)

#: 肤色修饰符
_TONE = "[\U0001f3fb-\U0001f3ff]"
#: 变体选择符（FE0F 请求彩色、FE0E 请求文字形态）
_VS = "[\ufe0e\ufe0f]"
#: 旗帜标签序列（如英格兰旗）
_TAG = "[\U000e0020-\U000e007e]+\U000e007f"

_ATOM = f"[{_BASE}]{_VS}?{_TONE}?(?:{_TAG})?"

#: 一个「emoji 簇」：国旗对 / 数字键帽 / 单个 emoji（可带 ZWJ 连接的后续部分）
EMOJI_PATTERN = re.compile(
    "(?:"
    "[\U0001f1e6-\U0001f1ff]{2}"
    "|"
    "[0-9#*]\ufe0f?\u20e3"
    "|"
    f"{_ATOM}(?:\u200d{_ATOM})*"
    ")"
)


def iter_segments(text: str) -> Iterator[tuple[str, bool]]:
    """把文本切成 (片段, 是否 emoji) 序列，顺序与原文一致。"""
    cursor = 0
    for match in EMOJI_PATTERN.finditer(text):
        if match.start() > cursor:
            yield text[cursor : match.start()], False
        yield match.group(0), True
        cursor = match.end()
    if cursor < len(text):
        yield text[cursor:], False


def iter_clusters(text: str) -> Iterator[tuple[str, bool]]:
    """把文本切成最小不可分单元：emoji 簇整体保留，普通文本按字符拆。"""
    for chunk, is_emoji in iter_segments(text):
        if is_emoji:
            yield chunk, True
        else:
            for char in chunk:
                yield char, False


#: 各平台自带彩色 emoji 字体，按「可任意缩放」优先排序
SYSTEM_EMOJI_FONTS: tuple[str, ...] = (
    # Windows：COLR/CPAL，任意字号都能加载，效果最好
    "C:/Windows/Fonts/seguiemj.ttf",
    # macOS
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    # Linux（Noto 彩色 emoji 是 CBDT 位图，只能按固定字号加载后缩放）
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji-Regular.ttf",
    "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/openmoji/OpenMoji-color-glyf_colr_0.ttf",
)

#: CBDT 位图字体只接受固定像素大小，按常见值依次尝试
_BITMAP_SIZES: tuple[int, ...] = (109, 137, 128, 96, 64, 32)

_MIN_BOX = 8


def _layout_engine() -> Any:
    """有 Raqm（HarfBuzz 整形）时用它，ZWJ 组合 emoji 才能合成单个字形。

    Pillow 的官方 Linux/macOS wheel 一般自带 Raqm；Windows wheel 通常没有，
    此时退回基础排版，👨‍👩‍👧 之类会拆成若干独立 emoji（可读，不影响功能）。
    """
    try:
        if features.check("raqm"):
            return ImageFont.Layout.RAQM
    except Exception:  # noqa: BLE001
        pass
    return ImageFont.Layout.BASIC


class EmojiFont:
    """解析并缓存彩色 emoji 字体，负责把单个 emoji 簇渲染成 RGBA 图块。"""

    def __init__(self, path: Any | None) -> None:
        self.path = str(path) if path else ""
        self._loaded: dict[int, tuple[Any, int]] = {}
        self._cells: dict[tuple[str, int], Image.Image | None] = {}
        self._layout = _layout_engine()
        self._broken = not self.path

    @property
    def available(self) -> bool:
        return not self._broken

    def _load(self, box: int) -> tuple[Any, int] | None:
        """返回 (字体, 实际加载字号)。位图字体会退回固定字号，之后缩放。"""
        if self._broken:
            return None
        cached = self._loaded.get(box)
        if cached is not None:
            return cached
        try:
            entry = (self._truetype(box), box)
        except OSError:
            entry = None
            for native in _BITMAP_SIZES:
                try:
                    entry = (self._truetype(native), native)
                    break
                except OSError:
                    continue
        except Exception:  # noqa: BLE001 - 字体文件损坏等
            self._broken = True
            return None
        if entry is None:
            self._broken = True
            return None
        self._loaded[box] = entry
        return entry

    def _truetype(self, size: int) -> Any:
        return ImageFont.truetype(self.path, size, layout_engine=self._layout)

    def cell(self, cluster: str, box: int) -> Image.Image | None:
        """渲染一个 emoji 簇，返回高度为 box 的 RGBA 图块；不支持则返回 None。

        所有簇都以「绘制原点为左上角、固定高度为一个 em」的方式裁切，
        因此同一行里不同 emoji 的基线天然一致。
        """
        box = max(_MIN_BOX, int(box))
        key = (cluster, box)
        if key in self._cells:
            return self._cells[key]
        image = self._render(cluster, box)
        self._cells[key] = image
        return image

    def _render(self, cluster: str, box: int) -> Image.Image | None:
        entry = self._load(box)
        if entry is None:
            return None
        font, native = entry
        try:
            advance = round(font.getlength(cluster))
        except Exception:  # noqa: BLE001
            advance = 0
        if advance <= 0:
            advance = native
        canvas = Image.new("RGBA", (advance + native, native * 2), (0, 0, 0, 0))
        try:
            ImageDraw.Draw(canvas).text((0, 0), cluster, font=font, embedded_color=True)
        except Exception:  # noqa: BLE001 - 该字体画不出这个簇
            canvas.close()
            return None
        if canvas.getbbox() is None:  # 字体里没有这个字形
            canvas.close()
            return None
        cell = canvas.crop((0, 0, advance, native))
        canvas.close()
        if native != box:
            width = max(1, round(advance * box / native))
            resized = cell.resize((width, box), Image.LANCZOS)
            cell.close()
            cell = resized
        return cell


def _metrics(font: Any) -> tuple[int, int]:
    """返回 (ascent, descent)，兼容没有 getmetrics 的内置位图字体。"""
    try:
        ascent, descent = font.getmetrics()
        return int(ascent), int(descent)
    except Exception:  # noqa: BLE001
        size = int(getattr(font, "size", 0) or 11)
        return size, max(1, size // 4)


def _length(font: Any, text: str) -> int:
    if not text:
        return 0
    try:
        return round(font.getlength(text))
    except Exception:  # noqa: BLE001
        bbox = font.getbbox(text)
        return int(bbox[2] - bbox[0]) if bbox else 0


class TextPainter:
    """emoji 与文字混排的测量 / 换行 / 绘制。测量和绘制共用同一套步进。"""

    #: emoji 图块宽度相对高度留出的间隙比例
    GAP_RATIO = 0.06

    def __init__(self, emoji_font: EmojiFont) -> None:
        self.emoji_font = emoji_font

    # ------------------------------------------------------------------ 测量

    def emoji_box(self, font: Any) -> int:
        ascent, _ = _metrics(font)
        return max(_MIN_BOX, ascent)

    def _emoji_width(self, cluster: str, font: Any) -> int:
        box = self.emoji_box(font)
        cell = self.emoji_font.cell(cluster, box) if self.emoji_font.available else None
        if cell is None:
            return _length(font, cluster) or box
        return cell.width + round(box * self.GAP_RATIO)

    def cluster_widths(self, text: str, font: Any) -> list[tuple[str, bool, int]]:
        result: list[tuple[str, bool, int]] = []
        for cluster, is_emoji in iter_clusters(text):
            width = self._emoji_width(cluster, font) if is_emoji else _length(font, cluster)
            result.append((cluster, is_emoji, width))
        return result

    def measure(self, text: str, font: Any) -> int:
        return sum(width for _, _, width in self.cluster_widths(text, font))

    def wrap(self, text: str, font: Any, max_width: int) -> list[str]:
        """按像素宽度换行。中日韩没有词边界，逐簇断行最稳，且不会切开 emoji。"""
        lines: list[str] = []
        for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if not paragraph:
                lines.append("")
                continue
            current: list[str] = []
            used = 0
            for cluster, _, width in self.cluster_widths(paragraph, font):
                if current and used + width > max_width:
                    lines.append("".join(current))
                    current = [cluster]
                    used = width
                else:
                    current.append(cluster)
                    used += width
            if current:
                lines.append("".join(current))
        return lines

    # ------------------------------------------------------------------ 绘制

    def draw(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        font: Any,
        fill: Any,
    ) -> int:
        """在 canvas 上绘制一行混排文本，返回占用宽度。

        xy 与 Pillow 默认锚点一致，指行的左上角。
        """
        x, y = int(xy[0]), int(xy[1])
        start = x
        box = self.emoji_box(font)
        buffer: list[str] = []

        def flush() -> None:
            nonlocal x
            if not buffer:
                return
            chunk = "".join(buffer)
            buffer.clear()
            draw.text((x, y), chunk, font=font, fill=fill)
            x += _length(font, chunk)

        for cluster, is_emoji in iter_clusters(text):
            if not is_emoji:
                buffer.append(cluster)
                continue
            cell = self.emoji_font.cell(cluster, box) if self.emoji_font.available else None
            if cell is None:
                buffer.append(cluster)
                continue
            flush()
            canvas.paste(cell, (x, y), cell)
            x += cell.width + round(box * self.GAP_RATIO)
        flush()
        return x - start

"""卡片渲染：把结构化内容画成一张有设计感的图片。

AstrBot 自带的 text_to_image 走的是通用 Markdown 模板：固定画布、带框架版本号，
正文只有几行时会渲染出三分之二都是空白的图。这里改成用 Pillow 自绘：

* 画布高度按内容自适应，不留大片空白；
* 配色与插件 logo、管理页皮肤同源（蓝紫渐变），风格统一；
* 提供进度条、统计格、榜单、标签等专用组件，比纯文字好读；
* 纯文本 / 轻量 Markdown 回复交给 from_markdown 自动解析，调用方不用改。

字体与 emoji 混排复用 core.fonts、core.emoji_text，除 Pillow 外没有额外依赖。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageDraw, ImageFilter

from .config import DISPLAY_NAME
from .emoji_text import TextPainter
from .fonts import FontResolver

# ==================================================================== 配色 === #

#: 语义色调，决定文字颜色与衬底颜色
Tone = Literal["normal", "muted", "brand", "ok", "warn", "err"]

BG = (243, 245, 252)
PANEL = (255, 255, 255)
FOOTER_BG = (250, 251, 255)
INSET = (241, 244, 252)
BRAND_1 = (91, 140, 255)
BRAND_2 = (123, 92, 255)
BRAND_INK = (59, 91, 219)
TEXT = (23, 27, 46)
TEXT_2 = (77, 85, 111)
TEXT_3 = (140, 147, 171)
LINE = (226, 230, 242)
OK = (18, 166, 122)
WARN = (203, 120, 5)
ERR = (222, 68, 68)

_TONE_INK: dict[str, tuple[int, int, int]] = {
    "normal": TEXT,
    "muted": TEXT_3,
    "brand": BRAND_INK,
    "ok": OK,
    "warn": WARN,
    "err": ERR,
}

#: 前三名的奖牌配色
_MEDALS: tuple[tuple[int, int, int], ...] = ((235, 175, 40), (154, 165, 189), (198, 137, 74))

# ==================================================================== 尺度 === #

#: 默认卡片宽度
DEFAULT_WIDTH = 900
#: 列数多的表格用这个宽度，免得每一格都换行
WIDE_WIDTH = 1160

_MARGIN = 22  # 画布四周留白
_RADIUS = 22  # 面板圆角
_PAD_X = 30  # 正文左右内边距
_PAD_Y = 22  # 正文上下内边距
_GAP = 13  # 相邻块的间距
_AA = 4  # 圆角蒙版的超采样倍率
_MAX_HEIGHT = 12000  # 高度上限，超出就截断，避免生成超大图

_FS_TITLE = 30
_FS_SUB = 15
_FS_BADGE = 14
_FS_HEAD = 19
_FS_BODY = 17
_FS_SMALL = 13
_FS_STAT = 27
_FS_CELL = 15
_FS_FOOT = 12

#: 行高相对字号的倍率
_LINE_RATIO = 1.62


def _lh(size: int) -> int:
    """字号对应的行高。"""
    return round(size * _LINE_RATIO)


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], ratio: float) -> tuple[int, int, int]:
    """按比例混合两个颜色。"""
    return (
        round(a[0] + (b[0] - a[0]) * ratio),
        round(a[1] + (b[1] - a[1]) * ratio),
        round(a[2] + (b[2] - a[2]) * ratio),
    )


def _ink(tone: str) -> tuple[int, int, int]:
    return _TONE_INK.get(tone, TEXT)


def _soft(tone: str, ratio: float = 0.13) -> tuple[int, int, int]:
    """色调对应的浅色衬底。"""
    return _mix(PANEL, _ink(tone), ratio)


# ================================================================ 绘图工具 === #


@lru_cache(maxsize=48)
def _aa_circle(radius: int) -> Image.Image:
    """抗锯齿圆形，用来贴圆角与画圆点。"""
    size = max(1, radius) * 2
    big = Image.new("L", (size * _AA, size * _AA), 0)
    ImageDraw.Draw(big).ellipse((0, 0, size * _AA - 1, size * _AA - 1), fill=255)
    return big.resize((size, size), Image.LANCZOS)


def _round_mask(
    width: int, height: int, radius: int, corners: tuple[bool, bool, bool, bool] = (True,) * 4
) -> Image.Image:
    """圆角矩形蒙版。

    整块铺满再贴四个抗锯齿圆角，比把整张图超采样省一个数量级的内存。
    corners 依次是左上、右上、右下、左下。
    """
    mask = Image.new("L", (max(1, width), max(1, height)), 255)
    radius = max(0, min(radius, width // 2, height // 2))
    if radius <= 0:
        return mask
    circle = _aa_circle(radius)
    double = radius * 2
    quads = (
        (circle.crop((0, 0, radius, radius)), (0, 0)),
        (circle.crop((radius, 0, double, radius)), (width - radius, 0)),
        (circle.crop((radius, radius, double, double)), (width - radius, height - radius)),
        (circle.crop((0, radius, radius, double)), (0, height - radius)),
    )
    for keep, (tile, pos) in zip(corners, quads, strict=True):
        if keep:
            mask.paste(tile, pos)
    return mask


def _fill_round(
    base: Image.Image,
    box: tuple[int, int, int, int],
    color: tuple[int, int, int],
    radius: int,
    corners: tuple[bool, bool, bool, bool] = (True,) * 4,
) -> None:
    """画一个抗锯齿的圆角矩形色块。"""
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    if width <= 0 or height <= 0:
        return
    base.paste(Image.new("RGB", (width, height), color), (x0, y0), _round_mask(width, height, radius, corners))


def _gradient(
    width: int,
    height: int,
    start: tuple[int, int, int],
    end: tuple[int, int, int],
    weights: tuple[float, float] = (0.5, 0.5),
) -> Image.Image:
    """线性渐变：先画 64x64 小图再放大，足够平滑也足够快。"""
    steps = 64
    span = steps - 1
    pixels: list[tuple[int, int, int]] = []
    weight_x, weight_y = weights
    for y in range(steps):
        base_y = y / span * weight_y
        for x in range(steps):
            pixels.append(_mix(start, end, x / span * weight_x + base_y))
    small = Image.new("RGB", (steps, steps))
    small.putdata(pixels)
    return small.resize((max(1, width), max(1, height)), Image.LANCZOS)


def _fill_gradient(
    base: Image.Image,
    box: tuple[int, int, int, int],
    start: tuple[int, int, int],
    end: tuple[int, int, int],
    radius: int,
    corners: tuple[bool, bool, bool, bool] = (True,) * 4,
    weights: tuple[float, float] = (0.5, 0.5),
) -> None:
    """画一个抗锯齿的圆角渐变色块。"""
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    if width <= 0 or height <= 0:
        return
    layer = _gradient(width, height, start, end, weights)
    base.paste(layer, (x0, y0), _round_mask(width, height, radius, corners))


def _drop_shadow(
    base: Image.Image,
    box: tuple[int, int, int, int],
    radius: int,
    *,
    blur: int = 14,
    offset: int = 7,
    alpha: int = 46,
) -> None:
    """给面板加一层柔和投影，让卡片从背景里浮起来。"""
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    if width <= 0 or height <= 0:
        return
    pad = blur * 3
    layer = Image.new("L", (width + pad * 2, height + pad * 2), 0)
    layer.paste(_round_mask(width, height, radius), (pad, pad))
    layer = layer.filter(ImageFilter.GaussianBlur(blur)).point(lambda value: value * alpha // 255)
    tint = Image.new("RGB", layer.size, (26, 36, 84))
    base.paste(tint, (x0 - pad, y0 - pad + offset), layer)


def _dot(base: Image.Image, center: tuple[int, int], radius: int, color: tuple[int, int, int]) -> None:
    """画一个抗锯齿圆点。"""
    circle = _aa_circle(radius)
    base.paste(
        Image.new("RGB", circle.size, color),
        (center[0] - radius, center[1] - radius),
        circle,
    )


def _glow(
    base: Image.Image,
    center: tuple[int, int],
    radius: int,
    color: tuple[int, int, int],
    alpha: int = 58,
) -> None:
    """背景柔光斑：纯色底太平，加两团光晕才有层次。

    先在 48x48 上画糊再放大，不管半径多大耗时都一样。
    """
    small = Image.new("L", (48, 48), 0)
    ImageDraw.Draw(small).ellipse((7, 7, 40, 40), fill=alpha)
    layer = small.filter(ImageFilter.GaussianBlur(7)).resize(
        (max(2, radius) * 2,) * 2, Image.LANCZOS
    )
    base.paste(Image.new("RGB", layer.size, color), (center[0] - radius, center[1] - radius), layer)


# ==================================================================== 内容 === #


@dataclass(slots=True)
class Text:
    """一段正文。"""

    text: str
    tone: Tone = "normal"
    small: bool = False


@dataclass(slots=True)
class Heading:
    """小节标题，左侧带一道品牌色竖条。"""

    text: str
    note: str = ""


@dataclass(slots=True)
class KeyValue:
    """字段表：左键右值，隔行浅底。"""

    rows: list[tuple[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class Bar:
    """带进度条的用量：ratio 取 0~1，超过阈值自动转警示色。"""

    label: str
    ratio: float
    value: str = ""
    note: str = ""
    tone: Tone | None = None


@dataclass(slots=True)
class Stat:
    """一格统计数字。"""

    label: str
    value: str
    note: str = ""
    tone: Tone = "brand"


@dataclass(slots=True)
class Stats:
    """统计数字网格，columns 为 0 时按个数自动分列。"""

    items: list[Stat] = field(default_factory=list)
    columns: int = 0


@dataclass(slots=True)
class Bullets:
    """列表：无序用圆点，有序用序号。"""

    items: list[str] = field(default_factory=list)
    ordered: bool = False


@dataclass(slots=True)
class RankRow:
    """榜单里的一行；weight 取 0~1，用来画背景比例条。"""

    name: str
    value: str = ""
    note: str = ""
    weight: float = 0.0


@dataclass(slots=True)
class Rank:
    """榜单：前三名有奖牌配色。"""

    rows: list[RankRow] = field(default_factory=list)
    medals: bool = True


@dataclass(slots=True)
class Tags:
    """标签胶囊，用来表示一组开关状态。"""

    items: list[tuple[str, Tone]] = field(default_factory=list)


@dataclass(slots=True)
class Table:
    """表格：表头浅底，行间细线。"""

    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)


@dataclass(slots=True)
class Note:
    """提示条：浅色底 + 左侧色条，用来放建议和警告。"""

    text: str
    tone: Tone = "brand"


@dataclass(slots=True)
class Divider:
    """分隔线。"""


#: 卡片支持的所有块类型
Block = Text | Heading | KeyValue | Bar | Stats | Bullets | Rank | Tags | Table | Note | Divider


@dataclass
class Card:
    """一张卡片的结构化内容。渲染与文字降级都基于它。"""

    title: str = ""
    subtitle: str = ""
    badge: str = ""
    blocks: list[Block] = field(default_factory=list)
    footer: str = ""
    #: 0 表示用 DEFAULT_WIDTH
    width: int = 0

    def add(self, *blocks: Block | None) -> Card:
        """追加块，None 会被忽略，方便条件式拼装。"""
        self.blocks.extend(block for block in blocks if block is not None)
        return self

    def to_text(self) -> str:
        """降级用的纯文本：协议端发不了图、或渲染失败时用这个。"""
        lines: list[str] = []
        head = f"【{self.title}】" if self.title else ""
        if self.subtitle:
            head = f"{head}{self.subtitle}" if head else self.subtitle
        if head:
            lines.append(head)
        for block in self.blocks:
            lines.extend(_block_text(block))
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines)


def _block_text(block: Block) -> list[str]:
    """把一个块摊平成纯文本行。"""
    if isinstance(block, Text):
        return [block.text]
    if isinstance(block, Heading):
        title = f"—— {block.text} ——"
        return ["", f"{title} {block.note}".rstrip()]
    if isinstance(block, KeyValue):
        return [f"{key}：{value}" for key, value in block.rows]
    if isinstance(block, Bar):
        percent = f"（{round(max(0.0, block.ratio) * 100)}%）" if block.ratio > 0 else ""
        head = f"{block.label}：{block.value}{percent}".replace("：（", "（")
        return [head] + ([block.note] if block.note else [])
    if isinstance(block, Stats):
        return [
            f"{item.label} {item.value}" + (f"（{item.note}）" if item.note else "")
            for item in block.items
        ]
    if isinstance(block, Bullets):
        if block.ordered:
            return [f"{index}. {item}" for index, item in enumerate(block.items, 1)]
        return [f"· {item}" for item in block.items]
    if isinstance(block, Rank):
        lines = []
        for index, row in enumerate(block.rows, 1):
            tail = f" {row.value}" if row.value else ""
            note = f"（{row.note}）" if row.note else ""
            lines.append(f"{index}. {row.name}{tail}{note}")
        return lines
    if isinstance(block, Tags):
        return [" / ".join(label for label, _ in block.items)] if block.items else []
    if isinstance(block, Table):
        lines = []
        if block.headers:
            lines.append(" | ".join(block.headers))
        lines.extend(" | ".join(cell for cell in row) for row in block.rows)
        return lines
    if isinstance(block, Note):
        return [block.text]
    return [""]


# ============================================================ Markdown 解析 === #

_TITLE_RE = re.compile(r"^#\s+(.+)$")
_HEADING_RE = re.compile(r"^#{2,6}\s+(.+)$")
#: 形如「—— 当前管理策略 ——」的小节标题
_FENCE_RE = re.compile(r"^\s*(?:—{2,}|={3,}|-{3,})\s*(.+?)\s*(?:—{2,}|={3,}|-{3,})\s*$")
_RULE_RE = re.compile(r"^\s*(?:—{3,}|={3,}|-{3,}|\*{3,})\s*$")
_BRACKET_RE = re.compile(r"^【(.{1,24}?)】\s*(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*•・]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\s*(\d{1,3})[.、)）]\s+(.+)$")
_KV_RE = re.compile(r"^([^：:\s][^：:]{0,15})[：:]\s*(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s*(.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|(.*)\|\s*$")
_TABLE_SEP_RE = re.compile(r"^[\s|:\-—]+$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
#: 行内代码；\x60 就是反引号，写成转义避免和正则里的引号混淆
_CODE_RE = re.compile(r"\x60([^\x60]+)\x60")

#: 以这些符号开头的行按「补充说明」处理（小字灰色）
_MUTED_PREFIX = ("……", "…", "...", "*")

#: 表格列数超过这个值就换用宽画布
_WIDE_COLUMNS = 4


def _clean_inline(text: str) -> str:
    """去掉行内 Markdown 标记，只留文字。"""
    text = _BOLD_RE.sub(r"\1", text)
    text = _CODE_RE.sub(r"\1", text)
    text = _ITALIC_RE.sub(r"\1", text)
    return text.strip()


class _Parser:
    """把轻量 Markdown 逐行归并成卡片块。

    只覆盖插件自己会产出的语法子集：小节标题、列表、键值行、表格、引用。
    同类连续行会合并成一个块，所以要先按行归类再收尾。
    """

    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self._kind = ""
        self._buffer: list[Any] = []

    # -------------------------------------------------------------- 对外
    def feed(self, raw: str) -> None:
        line = raw.rstrip()
        if not line.strip():
            self.flush()
            return

        if match := _TABLE_ROW_RE.match(line):
            if not _TABLE_SEP_RE.fullmatch(match.group(1)):
                cells = [_clean_inline(cell) for cell in match.group(1).split("|")]
                self._push("table", cells)
            return

        if _RULE_RE.fullmatch(line):
            self.flush()
            self.blocks.append(Divider())
            return

        if match := (_HEADING_RE.match(line) or _FENCE_RE.match(line)):
            self.flush()
            self.blocks.append(Heading(_clean_inline(match.group(1))))
            return

        if match := _QUOTE_RE.match(line):
            self.flush()
            self.blocks.append(Note(_clean_inline(match.group(1))))
            return

        if match := _BULLET_RE.match(line):
            self._push("bullet", _clean_inline(match.group(1)))
            return

        if match := _ORDERED_RE.match(line):
            self._push("ordered", _clean_inline(match.group(2)))
            return

        text = _clean_inline(line)
        if text.startswith(_MUTED_PREFIX):
            self._push("muted", text.lstrip("*").strip())
            return

        if match := _KV_RE.match(text):
            key, value = match.group(1).strip(), match.group(2).strip()
            if value:
                self._push("kv", (key, value))
                return

        self._push("text", text)

    def finish(self) -> list[Block]:
        self.flush()
        return self.blocks

    # -------------------------------------------------------------- 内部
    def _push(self, kind: str, item: Any) -> None:
        """换了类型就先收尾上一批，保证块的先后顺序和原文一致。"""
        if self._kind != kind:
            self.flush()
            self._kind = kind
        self._buffer.append(item)

    def flush(self) -> None:
        kind, items = self._kind, self._buffer
        self._kind, self._buffer = "", []
        if not items:
            return
        if kind == "text":
            self.blocks.append(Text("\n".join(items)))
        elif kind == "muted":
            self.blocks.append(Text("\n".join(items), tone="muted", small=True))
        elif kind == "kv":
            self.blocks.append(KeyValue(list(items)))
        elif kind == "bullet":
            self.blocks.append(Bullets(list(items)))
        elif kind == "ordered":
            self.blocks.append(Bullets(list(items), ordered=True))
        elif kind == "table":
            headers = list(items[0]) if len(items) > 1 else []
            rows = [list(row) for row in items[1:]] if len(items) > 1 else [list(items[0])]
            self.blocks.append(Table(headers=headers, rows=rows))


def _needs_wide(blocks: list[Block]) -> bool:
    """列数多的表格挤在 900px 里每格都要换行，改用宽画布。"""
    for block in blocks:
        if isinstance(block, Table):
            columns = max([len(block.headers)] + [len(row) for row in block.rows] or [0])
            if columns >= _WIDE_COLUMNS:
                return True
    return False


def from_markdown(
    text: str,
    *,
    title: str = "",
    subtitle: str = "",
    badge: str = "",
    footer: str = "",
) -> Card:
    """把纯文本 / 轻量 Markdown 解析成卡片。

    首行是「# 标题」或「【标题】附言」时会被提成卡片标题，其余按块归类。
    """
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    card = Card(title=title, subtitle=subtitle, badge=badge, footer=footer)

    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start < len(lines) and not card.title:
        head = lines[start].strip()
        if match := _TITLE_RE.match(head):
            card.title = _clean_inline(match.group(1))
            start += 1
        elif match := _BRACKET_RE.match(head):
            card.title = _clean_inline(match.group(1))
            rest = _clean_inline(match.group(2))
            start += 1
            if rest and not card.subtitle:
                card.subtitle = rest

    parser = _Parser()
    for line in lines[start:]:
        parser.feed(line)
    card.blocks = parser.finish()

    if not card.title:
        card.title = DISPLAY_NAME
    if _needs_wide(card.blocks):
        card.width = WIDE_WIDTH
    return card


# ================================================================== 渲染器 === #

#: 各组件的固定尺寸
_HEAD_TOP = 7  # 小节标题上方额外留白
_KV_PAD = 6  # 字段表行内上下留白
_CELL_GAP = 12  # 统计格 / 标签之间的间距
_STAT_PAD = 13  # 统计格内边距
_BULLET_INDENT = 22
_BULLET_GAP = 4
_RANK_ROW = 40
_RANK_GAP = 6
_TAG_H = 28
_TAG_PAD = 14
_TAG_GAP = 8
_TABLE_PAD_X = 12
_TABLE_PAD_Y = 8
_NOTE_PAD = 11
_TRACK_H = 12  # 进度条轨道高度
_ICON = 44  # 标题左侧徽标边长
_FOOTER_H = 42

#: 进度条自动转警示色的阈值
_BAR_DANGER = 0.9
_BAR_WARN = 0.7


@dataclass(slots=True)
class _Piece:
    """一个块的预排版结果。换行这类昂贵操作只在量高度时做一次，绘制阶段直接用。"""

    block: Block
    height: int
    data: Any = None


def _sweep_cards(dest_dir: Path, keep: int = 40, ttl: int = 600) -> None:
    """清掉旧卡片文件：超时或超量就删，免得数据目录无限膨胀。

    ttl 留得比较宽，避免协议端还没读完文件就被删掉。
    """
    try:
        files = sorted(
            dest_dir.glob("card-*.png"), key=lambda item: item.stat().st_mtime, reverse=True
        )
    except OSError:
        return
    now = time.time()
    for index, path in enumerate(files):
        try:
            if index >= keep or now - path.stat().st_mtime > ttl:
                path.unlink()
        except OSError:
            continue


class CardRenderer:
    """把 Card 画成图片。

    两趟布局：先给每个块量高度（顺手把换行结果存进 _Piece.data），再按定位绘制。
    这样画布高度能精确贴合内容，也不会把文字测量做两遍。
    """

    def __init__(self, fonts: FontResolver) -> None:
        self.fonts = fonts
        self._painter: TextPainter | None = None
        # 块类型 -> (量高度, 画出来)
        self._handlers: dict[type, tuple[Any, Any]] = {
            Text: (self._prep_text, self._draw_text),
            Heading: (self._prep_heading, self._draw_heading),
            KeyValue: (self._prep_kv, self._draw_kv),
            Bar: (self._prep_bar, self._draw_bar),
            Stats: (self._prep_stats, self._draw_stats),
            Bullets: (self._prep_bullets, self._draw_bullets),
            Rank: (self._prep_rank, self._draw_rank),
            Tags: (self._prep_tags, self._draw_tags),
            Table: (self._prep_table, self._draw_table),
            Note: (self._prep_note, self._draw_note),
            Divider: (self._prep_divider, self._draw_divider),
        }

    # ------------------------------------------------------------ 字体与文字

    @property
    def painter(self) -> TextPainter:
        """emoji 字体可能被换掉（比如刚下载完），换了就重建 painter。"""
        emoji = self.fonts.emoji_font()
        if self._painter is None or self._painter.emoji_font is not emoji:
            self._painter = TextPainter(emoji)
        return self._painter

    def _font(self, size: int, bold: bool = False) -> Any:
        return self.fonts.load(size, bold)

    def _measure(self, text: str, size: int, bold: bool = False) -> int:
        return self.painter.measure(text, self._font(size, bold))

    def _wrap(self, text: str, size: int, width: int, bold: bool = False) -> list[str]:
        return self.painter.wrap(text, self._font(size, bold), max(8, width))

    def _fit(self, text: str, size: int, width: int, bold: bool = False) -> str:
        """压成单行：放不下就砍掉尾部补省略号。标题、表格单元格这类不该换行的用它。"""
        text = " ".join(text.split())
        if not text or width <= 0:
            return ""
        font = self._font(size, bold)
        painter = self.painter
        if painter.measure(text, font) <= width:
            return text
        limit = width - painter.measure("…", font)
        kept: list[str] = []
        used = 0
        for cluster, _, cluster_width in painter.cluster_widths(text, font):
            if used + cluster_width > limit:
                break
            kept.append(cluster)
            used += cluster_width
        return "".join(kept).rstrip() + "…"

    def _line(
        self,
        base: Image.Image,
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        size: int,
        color: tuple[int, int, int],
        *,
        bold: bool = False,
        align: str = "left",
    ) -> None:
        """在行盒里写一行字。

        xy 指行盒左上角，文字在行高里垂直居中；align 为 right / center 时，
        x 分别表示右边界与中线。
        """
        if not text:
            return
        font = self._font(size, bold)
        x, y = xy
        if align != "left":
            span = self.painter.measure(text, font)
            x -= span if align == "right" else span // 2
        self.painter.draw(base, draw, (x, y + round(size * 0.11)), text, font, color)

    # ---------------------------------------------------------------- 分发

    def _prepare(self, block: Block, width: int) -> _Piece:
        handler = self._handlers.get(type(block))
        if handler is None:  # 兜底：认不出的块按正文画，绝不抛异常
            return self._prep_text(Text(str(block)), width)
        return handler[0](block, width)

    def _draw(
        self,
        base: Image.Image,
        draw: ImageDraw.ImageDraw,
        piece: _Piece,
        x: int,
        y: int,
        width: int,
    ) -> None:
        handler = self._handlers.get(type(piece.block))
        if handler is not None:
            handler[1](base, draw, piece, x, y, width)

    # ------------------------------------------------------------ 对外接口

    def render(self, card: Card) -> Image.Image:
        """画出整张卡片，高度按内容自适应。"""
        width = max(480, card.width or DEFAULT_WIDTH)
        panel_w = width - _MARGIN * 2
        inner = panel_w - _PAD_X * 2
        head_h = self._header_height(card)

        pieces = [
            piece
            for piece in (self._prepare(block, inner) for block in card.blocks)
            if piece.height > 0
        ]
        body_h = _PAD_Y * 2 + sum(piece.height for piece in pieces)
        body_h += _GAP * max(0, len(pieces) - 1)
        total = _MARGIN * 2 + head_h + body_h + _FOOTER_H

        overflow = False
        while total > _MAX_HEIGHT and len(pieces) > 1:
            total -= pieces.pop().height + _GAP
            overflow = True
        if overflow:
            tail = self._prepare(Note("内容太长，后面的条目已省略", tone="warn"), inner)
            pieces.append(tail)
            total += tail.height + _GAP

        base = Image.new("RGB", (width, total), BG)
        _glow(base, (0, round(total * 0.05)), round(width * 0.46), BRAND_1)
        _glow(base, (width, total - round(total * 0.08)), round(width * 0.40), BRAND_2)
        draw = ImageDraw.Draw(base)

        panel = (_MARGIN, _MARGIN, _MARGIN + panel_w, total - _MARGIN)
        _drop_shadow(base, panel, _RADIUS)
        _fill_round(base, panel, PANEL, _RADIUS)
        self._draw_header(base, draw, card, panel, head_h)
        self._draw_footer(base, draw, card, panel)

        x = _MARGIN + _PAD_X
        y = _MARGIN + head_h + _PAD_Y
        for piece in pieces:
            self._draw(base, draw, piece, x, y, inner)
            y += piece.height + _GAP
        return base

    def save(self, card: Card, dest_dir: Path) -> Path:
        """渲染并写成 PNG，返回文件路径。

        同步方法，里头有图像编码和磁盘写入，调用方请用 asyncio.to_thread 包一层。
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        _sweep_cards(dest_dir)
        path = dest_dir / f"card-{time.time_ns() // 1000}.png"
        image = self.render(card)
        try:
            image.save(path, "PNG", optimize=True)
        finally:
            image.close()
        return path

    # ------------------------------------------------------------ 卡片骨架

    def _header_height(self, card: Card) -> int:
        height = _PAD_Y * 2 + _lh(_FS_TITLE)
        if card.subtitle:
            height += _lh(_FS_SUB)
        return max(height, _ICON + _PAD_Y * 2)

    def _draw_header(
        self,
        base: Image.Image,
        draw: ImageDraw.ImageDraw,
        card: Card,
        panel: tuple[int, int, int, int],
        head_h: int,
    ) -> None:
        """标题条：品牌渐变底 + 徽标 + 标题 / 副标题 / 右侧胶囊。"""
        x0, y0, x1, _ = panel
        _fill_gradient(
            base,
            (x0, y0, x1, y0 + head_h),
            BRAND_1,
            BRAND_2,
            _RADIUS,
            (True, True, False, False),
            (0.82, 0.18),
        )

        icon_x, icon_y = x0 + _PAD_X, y0 + (head_h - _ICON) // 2
        self._draw_icon(base, draw, icon_x, icon_y)

        text_x = icon_x + _ICON + 16
        right = x1 - _PAD_X
        title_y = y0 + _PAD_Y if card.subtitle else y0 + (head_h - _lh(_FS_TITLE)) // 2

        if card.badge:
            badge_w = self._measure(card.badge, _FS_BADGE, bold=True) + 26
            badge_y = title_y + (_lh(_FS_TITLE) - 28) // 2
            _fill_round(
                base, (right - badge_w, badge_y, right, badge_y + 28), _mix(BRAND_2, PANEL, 0.3), 14
            )
            self._line(
                base,
                draw,
                (right - badge_w // 2, badge_y + (28 - _lh(_FS_BADGE)) // 2),
                card.badge,
                _FS_BADGE,
                PANEL,
                bold=True,
                align="center",
            )
            right -= badge_w + 14

        title = self._fit(card.title, _FS_TITLE, right - text_x, bold=True)
        self._line(base, draw, (text_x, title_y), title, _FS_TITLE, PANEL, bold=True)
        if card.subtitle:
            subtitle = self._fit(card.subtitle, _FS_SUB, x1 - _PAD_X - text_x)
            self._line(
                base,
                draw,
                (text_x, title_y + _lh(_FS_TITLE)),
                subtitle,
                _FS_SUB,
                _mix(BRAND_2, PANEL, 0.76),
            )

    def _draw_icon(self, base: Image.Image, draw: ImageDraw.ImageDraw, x: int, y: int) -> None:
        """白色圆角方块 + 「群」字，和插件 logo 用同一套视觉。"""
        _fill_round(base, (x, y, x + _ICON, y + _ICON), PANEL, 14)
        if self.fonts.resolve(False) is not None or self.fonts.resolve(True) is not None:
            self._line(
                base,
                draw,
                (x + _ICON // 2, y + (_ICON - _lh(26)) // 2),
                "群",
                26,
                BRAND_INK,
                bold=True,
                align="center",
            )
            return
        # 没有中文字体时退化成三点「成员」图形，和 logo 脚本的降级方案一致
        center = (x + _ICON // 2, y + _ICON // 2)
        for dx, dy in ((0, -8), (-9, 6), (9, 6)):
            _dot(base, (center[0] + dx, center[1] + dy), 4, BRAND_INK)

    def _draw_footer(
        self,
        base: Image.Image,
        draw: ImageDraw.ImageDraw,
        card: Card,
        panel: tuple[int, int, int, int],
    ) -> None:
        """页脚：左边放数据说明，右边固定署名。"""
        x0, _, x1, y1 = panel
        top = y1 - _FOOTER_H
        _fill_round(base, (x0, top, x1, y1), FOOTER_BG, _RADIUS, (False, False, True, True))
        draw.line((x0 + 1, top, x1 - 2, top), fill=LINE)

        text_y = top + (_FOOTER_H - _lh(_FS_FOOT)) // 2
        name_w = self._measure(DISPLAY_NAME, _FS_FOOT, bold=True)
        if card.footer:
            limit = x1 - _PAD_X * 2 - name_w - 24 - x0
            self._line(
                base,
                draw,
                (x0 + _PAD_X, text_y),
                self._fit(card.footer, _FS_FOOT, limit),
                _FS_FOOT,
                TEXT_3,
            )
        self._line(
            base,
            draw,
            (x1 - _PAD_X, text_y),
            DISPLAY_NAME,
            _FS_FOOT,
            _mix(TEXT_3, BRAND_INK, 0.55),
            bold=True,
            align="right",
        )
        _dot(base, (x1 - _PAD_X - name_w - 10, top + _FOOTER_H // 2), 3, BRAND_1)

    # ------------------------------------------------------------ 正文 / 标题

    def _prep_text(self, block: Text, width: int) -> _Piece:
        if not block.text.strip():
            return _Piece(block, 0)
        size = _FS_SMALL if block.small else _FS_BODY
        lines = self._wrap(block.text, size, width)
        return _Piece(block, len(lines) * _lh(size), (size, lines))

    def _draw_text(
        self,
        base: Image.Image,
        draw: ImageDraw.ImageDraw,
        piece: _Piece,
        x: int,
        y: int,
        width: int,
    ) -> None:
        size, lines = piece.data
        color = _ink(piece.block.tone)
        for index, line in enumerate(lines):
            self._line(base, draw, (x, y + index * _lh(size)), line, size, color)

    def _prep_heading(self, block: Heading, width: int) -> _Piece:
        if not block.text.strip():
            return _Piece(block, 0)
        note = self._fit(block.note, _FS_SMALL, round(width * 0.4))
        note_px = self._measure(note, _FS_SMALL) + 12 if note else 0
        text = self._fit(block.text, _FS_HEAD, width - 14 - note_px, bold=True)
        return _Piece(block, _HEAD_TOP + _lh(_FS_HEAD), (text, note))

    def _draw_heading(
        self,
        base: Image.Image,
        draw: ImageDraw.ImageDraw,
        piece: _Piece,
        x: int,
        y: int,
        width: int,
    ) -> None:
        text, note = piece.data
        top = y + _HEAD_TOP
        _fill_gradient(
            base, (x, top + 4, x + 4, top + _lh(_FS_HEAD) - 4), BRAND_1, BRAND_2, 2, weights=(0.0, 1.0)
        )
        self._line(base, draw, (x + 14, top), text, _FS_HEAD, TEXT, bold=True)
        if note:
            offset = (_lh(_FS_HEAD) - _lh(_FS_SMALL)) // 2
            self._line(base, draw, (x + width, top + offset), note, _FS_SMALL, TEXT_3, align="right")

    # ---------------------------------------------------------------- 字段表

    def _prep_kv(self, block: KeyValue, width: int) -> _Piece:
        rows = [(str(key), str(value)) for key, value in block.rows if str(key).strip()]
        if not rows:
            return _Piece(block, 0)
        key_w = min(200, max(110, round(width * 0.34)))
        value_w = width - key_w - 24
        prepared: list[tuple[str, list[str], int]] = []
        height = 0
        for key, value in rows:
            lines = self._wrap(value, _FS_BODY, value_w)
            row_h = len(lines) * _lh(_FS_BODY) + _KV_PAD * 2
            prepared.append((self._fit(key, _FS_BODY, key_w - 12), lines, row_h))
            height += row_h
        return _Piece(block, height, (key_w, prepared))

    def _draw_kv(
        self,
        base: Image.Image,
        draw: ImageDraw.ImageDraw,
        piece: _Piece,
        x: int,
        y: int,
        width: int,
    ) -> None:
        key_w, prepared = piece.data
        top = y
        for index, (key, lines, row_h) in enumerate(prepared):
            if index % 2 == 0:  # 隔行浅底，长表格才看得清哪个值对哪个键
                _fill_round(base, (x, top, x + width, top + row_h), INSET, 10)
            self._line(base, draw, (x + 12, top + _KV_PAD), key, _FS_BODY, TEXT_2)
            for offset, line in enumerate(lines):
                self._line(
                    base,
                    draw,
                    (x + key_w + 12, top + _KV_PAD + offset * _lh(_FS_BODY)),
                    line,
                    _FS_BODY,
                    TEXT,
                )
            top += row_h

    # ---------------------------------------------------------------- 进度条

    @staticmethod
    def _bar_tone(block: Bar) -> str:
        """没指定色调时按占用比例自动转警示色。"""
        if block.tone:
            return block.tone
        if block.ratio >= _BAR_DANGER:
            return "err"
        return "warn" if block.ratio >= _BAR_WARN else "brand"

    def _prep_bar(self, block: Bar, width: int) -> _Piece:
        ratio = min(1.0, max(0.0, block.ratio))
        value = block.value or f"{round(ratio * 100)}%"
        label = self._fit(block.label, _FS_BODY, width - self._measure(value, _FS_BODY, True) - 16)
        notes = self._wrap(block.note, _FS_SMALL, width) if block.note.strip() else []
        height = _lh(_FS_BODY) + 4 + _TRACK_H
        if notes:
            height += 6 + len(notes) * _lh(_FS_SMALL)
        return _Piece(block, height, (label, value, notes, ratio))

    def _draw_bar(
        self,
        base: Image.Image,
        draw: ImageDraw.ImageDraw,
        piece: _Piece,
        x: int,
        y: int,
        width: int,
    ) -> None:
        label, value, notes, ratio = piece.data
        tone = self._bar_tone(piece.block)
        radius = _TRACK_H // 2
        self._line(base, draw, (x, y), label, _FS_BODY, TEXT)
        self._line(base, draw, (x + width, y), value, _FS_BODY, _ink(tone), bold=True, align="right")

        track_y = y + _lh(_FS_BODY) + 4
        _fill_round(base, (x, track_y, x + width, track_y + _TRACK_H), LINE, radius)
        if ratio > 0:
            box = (x, track_y, x + max(_TRACK_H, round(width * ratio)), track_y + _TRACK_H)
            if tone == "brand":
                _fill_gradient(base, box, BRAND_1, BRAND_2, radius, weights=(1.0, 0.0))
            else:
                _fill_round(base, box, _ink(tone), radius)

        note_y = track_y + _TRACK_H + 6
        for index, line in enumerate(notes):
            self._line(base, draw, (x, note_y + index * _lh(_FS_SMALL)), line, _FS_SMALL, TEXT_3)

    # ---------------------------------------------------------------- 统计格

    def _prep_stats(self, block: Stats, width: int) -> _Piece:
        items = list(block.items)
        if not items:
            return _Piece(block, 0)
        count = len(items)
        columns = block.columns or (count if count <= 3 else 2 if count == 4 else 3)
        columns = max(1, min(columns, count))
        cell_w = (width - _CELL_GAP * (columns - 1)) // columns
        inner = cell_w - _STAT_PAD * 2
        cells = [
            (
                self._fit(item.label, _FS_SMALL, inner),
                self._fit(item.value, _FS_STAT, inner, bold=True),
                self._fit(item.note, _FS_SMALL, inner),
                item.tone,
            )
            for item in items
        ]
        cell_h = _STAT_PAD * 2 + _lh(_FS_SMALL) + _lh(_FS_STAT)
        if any(cell[2] for cell in cells):
            cell_h += _lh(_FS_SMALL)
        rows = -(-count // columns)
        height = rows * cell_h + (rows - 1) * _CELL_GAP
        return _Piece(block, height, (columns, cell_w, cell_h, cells))

    def _draw_stats(
        self,
        base: Image.Image,
        draw: ImageDraw.ImageDraw,
        piece: _Piece,
        x: int,
        y: int,
        width: int,
    ) -> None:
        columns, cell_w, cell_h, cells = piece.data
        for index, (label, value, note, tone) in enumerate(cells):
            column = index % columns
            left = x + column * (cell_w + _CELL_GAP)
            # 最后一列直接顶到右边，把整除留下的零头补掉
            right = x + width if column == columns - 1 else left + cell_w
            top = y + (index // columns) * (cell_h + _CELL_GAP)
            _fill_round(base, (left, top, right, top + cell_h), INSET, 14)

            center = (left + right) // 2
            line_y = top + _STAT_PAD
            self._line(base, draw, (center, line_y), label, _FS_SMALL, TEXT_3, align="center")
            line_y += _lh(_FS_SMALL)
            self._line(
                base, draw, (center, line_y), value, _FS_STAT, _ink(tone), bold=True, align="center"
            )
            if note:
                line_y += _lh(_FS_STAT)
                self._line(base, draw, (center, line_y), note, _FS_SMALL, TEXT_3, align="center")

    # ------------------------------------------------------------------ 列表

    def _prep_bullets(self, block: Bullets, width: int) -> _Piece:
        items = [str(item) for item in block.items if str(item).strip()]
        if not items:
            return _Piece(block, 0)
        wrapped = [self._wrap(item, _FS_BODY, width - _BULLET_INDENT) for item in items]
        height = sum(len(lines) for lines in wrapped) * _lh(_FS_BODY)
        height += _BULLET_GAP * (len(wrapped) - 1)
        return _Piece(block, height, wrapped)

    def _draw_bullets(
        self,
        base: Image.Image,
        draw: ImageDraw.ImageDraw,
        piece: _Piece,
        x: int,
        y: int,
        width: int,
    ) -> None:
        ordered = piece.block.ordered
        top = y
        for index, lines in enumerate(piece.data, 1):
            if ordered:
                offset = (_lh(_FS_BODY) - _lh(_FS_SMALL)) // 2
                self._line(
                    base,
                    draw,
                    (x + 17, top + offset),
                    f"{index}.",
                    _FS_SMALL,
                    BRAND_INK,
                    bold=True,
                    align="right",
                )
            else:
                _dot(base, (x + 6, top + _lh(_FS_BODY) // 2), 3, BRAND_1)
            for order, line in enumerate(lines):
                self._line(
                    base,
                    draw,
                    (x + _BULLET_INDENT, top + order * _lh(_FS_BODY)),
                    line,
                    _FS_BODY,
                    TEXT,
                )
            top += len(lines) * _lh(_FS_BODY) + _BULLET_GAP

    # ------------------------------------------------------------------ 榜单

    def _prep_rank(self, block: Rank, width: int) -> _Piece:
        rows = list(block.rows)
        if not rows:
            return _Piece(block, 0)
        value_w = max([0, *(self._measure(row.value, _FS_BODY, True) for row in rows)])
        prepared: list[tuple[str, str, str, float]] = []
        for row in rows:
            note = self._fit(row.note, _FS_SMALL, round(width * 0.34))
            note_px = self._measure(note, _FS_SMALL) + 14 if note else 0
            limit = width - 56 - value_w - note_px
            name = self._fit(row.name, _FS_BODY, limit, bold=True)
            prepared.append((name, note, row.value, min(1.0, max(0.0, row.weight))))
        height = len(rows) * _RANK_ROW + (len(rows) - 1) * _RANK_GAP
        return _Piece(block, height, (value_w, prepared))

    def _draw_rank(
        self,
        base: Image.Image,
        draw: ImageDraw.ImageDraw,
        piece: _Piece,
        x: int,
        y: int,
        width: int,
    ) -> None:
        value_w, prepared = piece.data
        medals = piece.block.medals
        for index, (name, note, value, weight) in enumerate(prepared):
            top = y + index * (_RANK_ROW + _RANK_GAP)
            _fill_round(base, (x, top, x + width, top + _RANK_ROW), INSET, 13)
            if weight > 0:  # 背景比例条，一眼看出差距
                bar_w = max(_RANK_ROW, round(width * weight))
                _fill_round(base, (x, top, x + bar_w, top + _RANK_ROW), _mix(PANEL, BRAND_1, 0.14), 13)

            color = _MEDALS[index] if medals and index < len(_MEDALS) else _mix(PANEL, BRAND_1, 0.45)
            _dot(base, (x + 22, top + _RANK_ROW // 2), 13, color)
            self._line(
                base,
                draw,
                (x + 22, top + (_RANK_ROW - _lh(_FS_SMALL)) // 2),
                str(index + 1),
                _FS_SMALL,
                PANEL,
                bold=True,
                align="center",
            )
            body_y = top + (_RANK_ROW - _lh(_FS_BODY)) // 2
            self._line(base, draw, (x + 42, body_y), name, _FS_BODY, TEXT, bold=True)

            right = x + width - 14
            if value:
                self._line(base, draw, (right, body_y), value, _FS_BODY, BRAND_INK, bold=True, align="right")
                right -= value_w + 14
            if note:
                small_y = top + (_RANK_ROW - _lh(_FS_SMALL)) // 2
                self._line(base, draw, (right, small_y), note, _FS_SMALL, TEXT_3, align="right")

    # ------------------------------------------------------------------ 标签

    def _prep_tags(self, block: Tags, width: int) -> _Piece:
        items = [(str(label), tone) for label, tone in block.items if str(label).strip()]
        if not items:
            return _Piece(block, 0)
        placed: list[tuple[str, str, int, int, int]] = []
        cursor = 0
        row = 0
        for label, tone in items:
            text = self._fit(label, _FS_BADGE, width - _TAG_PAD * 2)
            span = self._measure(text, _FS_BADGE) + _TAG_PAD * 2
            if cursor and cursor + span > width:
                cursor, row = 0, row + 1
            placed.append((text, tone, cursor, row, span))
            cursor += span + _TAG_GAP
        height = (row + 1) * _TAG_H + row * _TAG_GAP
        return _Piece(block, height, placed)

    def _draw_tags(
        self,
        base: Image.Image,
        draw: ImageDraw.ImageDraw,
        piece: _Piece,
        x: int,
        y: int,
        width: int,
    ) -> None:
        for text, tone, left, row, span in piece.data:
            top = y + row * (_TAG_H + _TAG_GAP)
            _fill_round(
                base, (x + left, top, x + left + span, top + _TAG_H), _soft(tone, 0.16), _TAG_H // 2
            )
            self._line(
                base,
                draw,
                (x + left + span // 2, top + (_TAG_H - _lh(_FS_BADGE)) // 2),
                text,
                _FS_BADGE,
                _ink(tone),
                bold=True,
                align="center",
            )

    # ------------------------------------------------------------------ 表格

    def _prep_table(self, block: Table, width: int) -> _Piece:
        headers = [str(cell) for cell in block.headers]
        rows = [[str(cell) for cell in row] for row in block.rows]
        columns = max([len(headers), *(len(row) for row in rows)])
        if columns <= 0:
            return _Piece(block, 0)

        natural = [1] * columns
        for index in range(columns):
            if index < len(headers):
                natural[index] = max(natural[index], self._measure(headers[index], _FS_SMALL, True))
            for row in rows:
                if index < len(row):
                    natural[index] = max(natural[index], self._measure(row[index], _FS_CELL))

        avail = max(columns * 40, width - columns * _TABLE_PAD_X * 2)
        total = sum(natural)
        if total <= avail:  # 有余量就按比例撑满，别在右边留一条空白
            widths = [value + round((avail - total) * value / total) for value in natural]
        else:
            widths = [max(36, round(value * avail / total)) for value in natural]
            excess = sum(widths) - avail
            while excess > 0:  # 收缩最宽的列，把超出的宽度还回去
                biggest = widths.index(max(widths))
                take = min(excess, widths[biggest] - 36)
                if take <= 0:
                    break
                widths[biggest] -= take
                excess -= take

        head_h = _lh(_FS_SMALL) + _TABLE_PAD_Y * 2 if headers else 0
        body: list[tuple[list[list[str]], int]] = []
        for row in rows:
            cells = [
                self._wrap(row[index] if index < len(row) else "", _FS_CELL, widths[index])
                for index in range(columns)
            ]
            lines = max(len(cell) for cell in cells)
            body.append((cells, lines * _lh(_FS_CELL) + _TABLE_PAD_Y * 2))

        titles = [
            self._fit(label, _FS_SMALL, cell_w, bold=True)
            for label, cell_w in zip(headers, widths, strict=False)
        ]
        height = head_h + sum(row_h for _, row_h in body)
        return _Piece(block, height, (widths, titles, head_h, body))

    def _draw_table(
        self,
        base: Image.Image,
        draw: ImageDraw.ImageDraw,
        piece: _Piece,
        x: int,
        y: int,
        width: int,
    ) -> None:
        widths, titles, head_h, body = piece.data
        step = _TABLE_PAD_X * 2

        if head_h:
            _fill_round(base, (x, y, x + width, y + head_h), INSET, 10, (True, True, False, False))
            left = x
            for index, label in enumerate(titles):
                self._line(
                    base, draw, (left + _TABLE_PAD_X, y + _TABLE_PAD_Y), label, _FS_SMALL, TEXT_2, bold=True
                )
                left += widths[index] + step

        top = y + head_h
        for order, (cells, row_h) in enumerate(body):
            if order:
                draw.line((x + 6, top, x + width - 6, top), fill=LINE)
            left = x
            for index, lines in enumerate(cells):
                color = TEXT if index == 0 else TEXT_2
                for offset, line in enumerate(lines):
                    self._line(
                        base,
                        draw,
                        (left + _TABLE_PAD_X, top + _TABLE_PAD_Y + offset * _lh(_FS_CELL)),
                        line,
                        _FS_CELL,
                        color,
                    )
                left += widths[index] + step
            top += row_h

    # -------------------------------------------------------------- 提示条等

    def _prep_note(self, block: Note, width: int) -> _Piece:
        if not block.text.strip():
            return _Piece(block, 0)
        lines = self._wrap(block.text, _FS_BODY, width - 18 - _NOTE_PAD)
        return _Piece(block, len(lines) * _lh(_FS_BODY) + _NOTE_PAD * 2, lines)

    def _draw_note(
        self,
        base: Image.Image,
        draw: ImageDraw.ImageDraw,
        piece: _Piece,
        x: int,
        y: int,
        width: int,
    ) -> None:
        tone = piece.block.tone
        bottom = y + piece.height
        _fill_round(base, (x, y, x + width, bottom), _soft(tone, 0.15), 12)
        _fill_round(base, (x, y + 8, x + 4, bottom - 8), _ink(tone), 2)
        for index, line in enumerate(piece.data):
            self._line(
                base,
                draw,
                (x + 18, y + _NOTE_PAD + index * _lh(_FS_BODY)),
                line,
                _FS_BODY,
                _ink(tone),
            )

    def _prep_divider(self, block: Divider, width: int) -> _Piece:
        return _Piece(block, 9)

    def _draw_divider(
        self,
        base: Image.Image,
        draw: ImageDraw.ImageDraw,
        piece: _Piece,
        x: int,
        y: int,
        width: int,
    ) -> None:
        draw.line((x, y + 4, x + width, y + 4), fill=_mix(LINE, TEXT_3, 0.35))

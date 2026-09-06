"""生成插件 logo（logo.png）。

依赖 Pillow：python tools/make_logo.py
输出 512x512 圆角渐变徽标，内含盾牌与「群」字，用于 AstrBot 插件市场与管理页展示。

盾牌轮廓、渐变、柔光都取自 core.shapes，和卡片徽标共用同一套原语 —— 改一处两边一起变，
不会出现「logo 是一个形状、卡片里又是另一个形状」。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.shapes import gradient, round_mask, shield_points, soft_light

SIZE = 512
SS = 4  # 超采样倍率，保证边缘平滑
CANVAS = SIZE * SS

TOP_COLOR = (74, 125, 255)
BOTTOM_COLOR = (123, 92, 255)
GLYPH_COLOR = (59, 91, 219)

FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]


def load_font(size: int) -> ImageFont.FreeTypeFont | None:
    for candidate in FONT_CANDIDATES:
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return None


def draw_glyph(base: Image.Image, cx: float, cy: float, box: float) -> None:
    font = load_font(int(box))
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    if font is not None:
        draw.text((cx, cy), "群", font=font, fill=(*GLYPH_COLOR, 255), anchor="mm")
    else:
        # 没有可用中文字体时退化为三点「成员」图形，保证脚本永远能跑通
        r = box * 0.16
        for dx, dy in ((0, -box * 0.22), (-box * 0.26, box * 0.18), (box * 0.26, box * 0.18)):
            draw.ellipse(
                (cx + dx - r, cy + dy - r, cx + dx + r, cy + dy + r),
                fill=(*GLYPH_COLOR, 255),
            )
    base.alpha_composite(layer)


def build() -> Image.Image:
    card = gradient(CANVAS, CANVAS, TOP_COLOR, BOTTOM_COLOR, (0.35, 0.65))
    # 左上柔光，让纯渐变不至于太平
    soft_light(card, (-CANVAS * 0.60, -CANVAS * 0.95, CANVAS * 1.10, CANVAS * 0.28), 40, CANVAS * 0.06)
    card = card.convert("RGBA")

    shield_w = CANVAS * 0.54
    shield_h = CANVAS * 0.64
    top = CANVAS * 0.17
    cx = CANVAS / 2

    shadow = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon(
        shield_points(cx, top + CANVAS * 0.018, shield_w, shield_h), fill=(20, 30, 70, 90)
    )
    card.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(CANVAS * 0.014)))

    shield = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    ImageDraw.Draw(shield).polygon(
        shield_points(cx, top, shield_w, shield_h), fill=(255, 255, 255, 250)
    )
    card.alpha_composite(shield)

    draw_glyph(card, cx, top + shield_h * 0.365, shield_w * 0.54)

    # 盾内三点：既呼应「群成员」，也让下半部分不空
    dots = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dots)
    dot_r = CANVAS * 0.0205
    dot_y = top + shield_h * 0.655
    for offset in (-1, 0, 1):
        dot_cx = cx + offset * dot_r * 3.1
        dd.ellipse(
            (dot_cx - dot_r, dot_y - dot_r, dot_cx + dot_r, dot_y + dot_r),
            fill=(*GLYPH_COLOR, 140),
        )
    card.alpha_composite(dots)

    card.putalpha(round_mask(CANVAS, CANVAS, int(CANVAS * 0.225)))
    return card.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> None:
    target = Path(__file__).resolve().parent.parent / "logo.png"
    build().save(target, "PNG")
    print(f"logo written: {target}")


if __name__ == "__main__":
    main()

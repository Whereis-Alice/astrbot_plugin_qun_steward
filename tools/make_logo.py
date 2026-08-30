"""生成插件 logo（logo.png）。

依赖 Pillow：python tools/make_logo.py
输出 512x512 圆角渐变徽标，内含盾牌与「群」字，用于 AstrBot 插件市场与管理页展示。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

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


def gradient(width: int, height: int) -> Image.Image:
    """对角线性渐变：先画小图再放大，既快又平滑。"""
    small = Image.new("RGB", (64, 64))
    pixels = []
    for y in range(64):
        for x in range(64):
            ratio = (x / 63 * 0.35) + (y / 63 * 0.65)
            pixels.append(
                tuple(
                    round(TOP_COLOR[i] + (BOTTOM_COLOR[i] - TOP_COLOR[i]) * ratio)
                    for i in range(3)
                )
            )
    small.putdata(pixels)
    return small.resize((width, height), Image.LANCZOS)


def rounded_mask(width: int, height: int, radius: int) -> Image.Image:
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius, fill=255)
    return mask


def qbezier(p0, p1, p2, steps: int = 48):
    out = []
    for i in range(steps + 1):
        t = i / steps
        inv = 1 - t
        out.append(
            (
                inv * inv * p0[0] + 2 * inv * t * p1[0] + t * t * p2[0],
                inv * inv * p0[1] + 2 * inv * t * p1[1] + t * t * p2[1],
            )
        )
    return out


def shield_polygon(cx: float, top: float, width: float, height: float) -> list[tuple[float, float]]:
    """经典盾牌轮廓：平顶圆角 + 下方收拢到尖角。"""
    half = width / 2
    left, right = cx - half, cx + half
    radius = width * 0.20
    shoulder = top + height * 0.46
    bottom = top + height

    points: list[tuple[float, float]] = []
    points += qbezier((left + radius, top), (left, top), (left, top + radius), 18)
    points.append((left, shoulder))
    points += qbezier((left, shoulder), (left + width * 0.015, bottom - height * 0.20), (cx, bottom), 48)
    points += qbezier((cx, bottom), (right - width * 0.015, bottom - height * 0.20), (right, shoulder), 48)
    points.append((right, top + radius))
    points += qbezier((right, top + radius), (right, top), (right - radius, top), 18)
    return points


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
    card = gradient(CANVAS, CANVAS).convert("RGBA")

    # 左上柔光，让纯渐变不至于太平
    glow = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        (-CANVAS * 0.60, -CANVAS * 0.95, CANVAS * 1.10, CANVAS * 0.28),
        fill=(255, 255, 255, 40),
    )
    card.alpha_composite(glow.filter(ImageFilter.GaussianBlur(CANVAS * 0.06)))

    shield_w = CANVAS * 0.54
    shield_h = CANVAS * 0.64
    top = CANVAS * 0.17
    cx = CANVAS / 2

    shadow = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon(
        shield_polygon(cx, top + CANVAS * 0.018, shield_w, shield_h), fill=(20, 30, 70, 90)
    )
    card.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(CANVAS * 0.014)))

    shield = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    ImageDraw.Draw(shield).polygon(shield_polygon(cx, top, shield_w, shield_h), fill=(255, 255, 255, 250))
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

    card.putalpha(rounded_mask(CANVAS, CANVAS, int(CANVAS * 0.225)))
    return card.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> None:
    target = Path(__file__).resolve().parent.parent / "logo.png"
    build().save(target, "PNG")
    print(f"logo written: {target}")


if __name__ == "__main__":
    main()

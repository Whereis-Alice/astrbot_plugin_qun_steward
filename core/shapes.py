"""绘图原语：混色、圆角、渐变、投影、盾牌轮廓。

只依赖 Pillow，不引用插件里的任何业务模块，所以 core.card 和 tools/make_logo.py
能共用同一套形状 —— logo 与卡片徽标才是同一条轮廓，视觉不会各画一套。
"""

from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter

RGB = tuple[int, int, int]
Box = tuple[int, int, int, int]
Corners = tuple[bool, bool, bool, bool]

#: 圆角 / 盾牌蒙版的超采样倍率
AA = 4
#: 盾牌高宽比，与 logo 里的 0.64 : 0.54 一致
SHIELD_RATIO = 1.185

ALL_CORNERS: Corners = (True, True, True, True)


def mix(a: RGB, b: RGB, ratio: float) -> RGB:
    """按比例混合两个颜色。"""
    return (
        round(a[0] + (b[0] - a[0]) * ratio),
        round(a[1] + (b[1] - a[1]) * ratio),
        round(a[2] + (b[2] - a[2]) * ratio),
    )


# ------------------------------------------------------------------ 圆角


@lru_cache(maxsize=48)
def aa_circle(radius: int) -> Image.Image:
    """抗锯齿圆形，用来贴圆角与画圆点。"""
    size = max(1, radius) * 2
    big = Image.new("L", (size * AA, size * AA), 0)
    ImageDraw.Draw(big).ellipse((0, 0, size * AA - 1, size * AA - 1), fill=255)
    return big.resize((size, size), Image.LANCZOS)


def round_mask(width: int, height: int, radius: int, corners: Corners = ALL_CORNERS) -> Image.Image:
    """圆角矩形蒙版。

    整块铺满再贴四个抗锯齿圆角，比把整张图超采样省一个数量级的内存。
    corners 依次是左上、右上、右下、左下。
    """
    mask = Image.new("L", (max(1, width), max(1, height)), 255)
    radius = max(0, min(radius, width // 2, height // 2))
    if radius <= 0:
        return mask
    circle = aa_circle(radius)
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


def fill_round(
    base: Image.Image, box: Box, color: RGB, radius: int, corners: Corners = ALL_CORNERS
) -> None:
    """画一个抗锯齿的圆角矩形色块。"""
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    if width <= 0 or height <= 0:
        return
    base.paste(
        Image.new("RGB", (width, height), color), (x0, y0), round_mask(width, height, radius, corners)
    )


def stroke_round(
    base: Image.Image,
    box: Box,
    color: RGB,
    radius: int,
    width: int = 1,
    corners: Corners = ALL_CORNERS,
) -> None:
    """画一圈抗锯齿的圆角描边。

    外圈蒙版减内圈蒙版得到一条细边，比 Pillow 的 outline 平滑，也能配合圆角。
    """
    x0, y0, x1, y1 = box
    span_x, span_y = x1 - x0, y1 - y0
    if span_x <= width * 2 or span_y <= width * 2:
        return
    outer = round_mask(span_x, span_y, radius, corners)
    inner = Image.new("L", (span_x, span_y), 0)
    inner.paste(
        round_mask(span_x - width * 2, span_y - width * 2, max(0, radius - width), corners),
        (width, width),
    )
    base.paste(Image.new("RGB", (span_x, span_y), color), (x0, y0), ImageChops.subtract(outer, inner))


# ------------------------------------------------------------------ 渐变


def gradient(
    width: int, height: int, start: RGB, end: RGB, weights: tuple[float, float] = (0.5, 0.5)
) -> Image.Image:
    """线性渐变：先画 64x64 小图再放大，足够平滑也足够快。

    weights 是横向 / 纵向的权重，(1,0) 是纯横向，(0,1) 是纯纵向。
    """
    steps = 64
    span = steps - 1
    pixels: list[RGB] = []
    weight_x, weight_y = weights
    for y in range(steps):
        base_y = y / span * weight_y
        for x in range(steps):
            pixels.append(mix(start, end, x / span * weight_x + base_y))
    small = Image.new("RGB", (steps, steps))
    small.putdata(pixels)
    return small.resize((max(1, width), max(1, height)), Image.LANCZOS)


def fill_gradient(
    base: Image.Image,
    box: Box,
    start: RGB,
    end: RGB,
    radius: int,
    corners: Corners = ALL_CORNERS,
    weights: tuple[float, float] = (0.5, 0.5),
) -> None:
    """画一个抗锯齿的圆角渐变色块。"""
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    if width <= 0 or height <= 0:
        return
    base.paste(gradient(width, height, start, end, weights), (x0, y0), round_mask(width, height, radius, corners))


def soft_light(
    layer: Image.Image, box: tuple[float, float, float, float], alpha: int, blur: float
) -> None:
    """在图层上叠一团柔光，让纯渐变有体积感。

    椭圆按图层自身尺寸定位，之后整层会被调用方的蒙版裁掉，所以不会露出硬边。
    """
    width, height = layer.size
    glow = Image.new("L", (width, height), 0)
    ImageDraw.Draw(glow).ellipse(box, fill=max(0, min(255, alpha)))
    layer.paste(
        Image.new("RGB", (width, height), (255, 255, 255)),
        (0, 0),
        glow.filter(ImageFilter.GaussianBlur(blur)),
    )


def drop_shadow(
    base: Image.Image, box: Box, radius: int, *, blur: int = 14, offset: int = 7, alpha: int = 46
) -> None:
    """给面板加一层柔和投影，让卡片从背景里浮起来。"""
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    if width <= 0 or height <= 0:
        return
    pad = blur * 3
    layer = Image.new("L", (width + pad * 2, height + pad * 2), 0)
    layer.paste(round_mask(width, height, radius), (pad, pad))
    layer = layer.filter(ImageFilter.GaussianBlur(blur)).point(lambda value: value * alpha // 255)
    tint = Image.new("RGB", layer.size, (26, 36, 84))
    base.paste(tint, (x0 - pad, y0 - pad + offset), layer)


def dot(base: Image.Image, center: tuple[int, int], radius: int, color: RGB) -> None:
    """画一个抗锯齿圆点。"""
    circle = aa_circle(radius)
    base.paste(Image.new("RGB", circle.size, color), (center[0] - radius, center[1] - radius), circle)


# ------------------------------------------------------------------ 盾牌


def qbezier(
    p0: tuple[float, float], p1: tuple[float, float], p2: tuple[float, float], steps: int = 24
) -> list[tuple[float, float]]:
    """二次贝塞尔曲线采样点。"""
    points: list[tuple[float, float]] = []
    for index in range(steps + 1):
        t = index / steps
        inv = 1 - t
        points.append(
            (
                inv * inv * p0[0] + 2 * inv * t * p1[0] + t * t * p2[0],
                inv * inv * p0[1] + 2 * inv * t * p1[1] + t * t * p2[1],
            )
        )
    return points


def shield_points(cx: float, top: float, width: float, height: float) -> list[tuple[float, float]]:
    """经典盾牌轮廓：平顶圆角 + 下方收拢到尖角。"""
    half = width / 2
    left, right = cx - half, cx + half
    radius = width * 0.20
    shoulder = top + height * 0.46
    bottom = top + height

    points = qbezier((left + radius, top), (left, top), (left, top + radius), 18)
    points.append((left, shoulder))
    points += qbezier((left, shoulder), (left + width * 0.015, bottom - height * 0.20), (cx, bottom), 48)
    points += qbezier((cx, bottom), (right - width * 0.015, bottom - height * 0.20), (right, shoulder), 48)
    points.append((right, top + radius))
    points += qbezier((right, top + radius), (right, top), (right - radius, top), 18)
    return points


@lru_cache(maxsize=8)
def shield_mask(width: int) -> Image.Image:
    """盾牌蒙版，尺寸为 width x round(width * SHIELD_RATIO)。

    先在 4 倍画布上画多边形再缩小，边缘才不会有锯齿。徽标和页脚小标都用它。
    """
    width = max(4, width)
    height = round(width * SHIELD_RATIO)
    big = Image.new("L", (width * AA, height * AA), 0)
    ImageDraw.Draw(big).polygon(
        shield_points(width * AA / 2, 0, width * AA - 1, height * AA - 1), fill=255
    )
    return big.resize((width, height), Image.LANCZOS)

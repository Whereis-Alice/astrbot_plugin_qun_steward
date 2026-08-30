"""「我朋友说」风格图片渲染。

从上游 astrbot_plugin_qun_album 移植，保留原有排版参数，改为通过
FontResolver 取字体、通过配置目录取气泡素材，不再依赖模块级全局状态。
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

from astrbot.api import logger
from PIL import Image, ImageDraw

from ...core.config import LOG_TAG
from .fonts import FontResolver

# --------------------------------------------------------------------------- #
# emoji 库兼容层：emoji>=2 移除了 1.x 的若干 API，而 pilmoji 2.0.4 仍在用。
# 这里按需补齐，任何异常都直接忽略（最多退化成不渲染彩色 emoji）。
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - 依赖第三方库版本
    import emoji
    from emoji import unicode_codes

    if not hasattr(unicode_codes, "get_emoji_unicode_dict"):

        def _get_emoji_unicode_dict(lang: str) -> dict[str, str]:
            return {data[lang]: char for char, data in emoji.EMOJI_DATA.items() if lang in data}

        unicode_codes.get_emoji_unicode_dict = _get_emoji_unicode_dict

    if not hasattr(unicode_codes, "EMOJI_UNICODE"):
        unicode_codes.EMOJI_UNICODE = {"en": unicode_codes.get_emoji_unicode_dict("en")}

    if not hasattr(emoji, "get_emoji_regexp"):
        _emoji_regexp: re.Pattern[str] | None = None

        def _get_emoji_regexp() -> re.Pattern[str]:
            global _emoji_regexp
            if _emoji_regexp is None:
                items = sorted(emoji.EMOJI_DATA.keys(), key=len, reverse=True)
                _emoji_regexp = re.compile("|".join(re.escape(item) for item in items))
            return _emoji_regexp

        emoji.get_emoji_regexp = _get_emoji_regexp
except Exception:  # noqa: BLE001
    emoji = None  # type: ignore[assignment]

try:  # pragma: no cover - 可选依赖
    from pilmoji import Pilmoji
except Exception:  # noqa: BLE001
    Pilmoji = None  # type: ignore[assignment]


#: 角色 -> 徽章底色
ROLE_COLORS = {"owner": "#fdd93f", "admin": "#3fe3d8"}
DEFAULT_BADGE_COLOR = "#9db2e0"
CUSTOM_TITLE_COLOR = "#d38ffe"
CANVAS_COLOR = "#eaedf4"
NAME_COLOR = "#868894"

#: 等级 -> 段位名（无自定义头衔时用）
LEVEL_TITLES: tuple[tuple[int, int, str], ...] = (
    (1, 10, "青铜"),
    (11, 20, "白银"),
    (21, 40, "黄金"),
    (41, 60, "铂金"),
    (61, 80, "钻石"),
    (81, 10**6, "王者"),
)

AVATAR_SIZE = 135
BUBBLE_X = 165
BADGE_X = 195
BUBBLE_FONT_SIZE = 55
BUBBLE_MAX_TEXT_WIDTH = 900


def _text_size(font: Any, text: str) -> tuple[int, int]:
    """用 getbbox 量文本尺寸，兼容返回 None 的字体实现。"""
    bbox = font.getbbox(text)
    if not bbox:
        return 0, 0
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def wrap_text(text: str, font: Any, max_width: int) -> list[str]:
    """按像素宽度逐字符换行；中日韩文本没有词边界，逐字符最稳。"""
    lines: list[str] = []
    for paragraph in text.replace("\r\n", "\n").split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for char in paragraph:
            candidate = current + char
            width, _ = _text_size(font, candidate)
            if width <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = char
        if current:
            lines.append(current)
    return lines


def pad_emojis(text: str) -> str:
    """给 emoji 两侧补空格，避免 pilmoji 贴图压住相邻文字。"""
    if emoji is None:
        return text
    try:
        pattern = emoji.get_emoji_regexp()
        return re.sub(pattern, lambda m: " " + m.group(0) + " ", text)
    except Exception:  # noqa: BLE001
        return text


def make_italic(image: Image.Image, skew: float = 0.1) -> Image.Image:
    """错切变换模拟斜体。"""
    width, height = image.size
    new_width = width + int(height * abs(skew))
    return image.transform(
        (new_width, height), Image.AFFINE, (1, skew, 0, 0, 1, 0), resample=Image.BICUBIC
    )


def level_title(level: int) -> str:
    for low, high, name in LEVEL_TITLES:
        if low <= level <= high:
            return name
    return ""


class MemeRenderer:
    """渲染单条 / 多条「我朋友说」图片。"""

    def __init__(self, fonts: FontResolver, resource_dir: Path) -> None:
        self.fonts = fonts
        self.resource_dir = resource_dir
        self._corners: list[Image.Image] | None = None
        self._corner_missing = False

    # ------------------------------------------------------------- 气泡素材

    def _load_corners(self) -> list[Image.Image] | None:
        """加载四个圆角素材并缓存；缺失时只告警一次。"""
        if self._corner_missing:
            return None
        if self._corners is not None:
            return self._corners
        images: list[Image.Image] = []
        for index in (1, 2, 3, 4):
            path = self.resource_dir / f"corner{index}.png"
            try:
                images.append(Image.open(path).convert("RGBA"))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{LOG_TAG} 气泡素材缺失 {path}: {exc}，改用纯色圆角矩形")
                for opened in images:
                    opened.close()
                self._corner_missing = True
                return None
        self._corners = images
        return images

    def make_dialog_box(self, text: str, name_w: int = 0) -> Image.Image:
        font = self.fonts.load(BUBBLE_FONT_SIZE)
        lines = wrap_text(pad_emojis(text), font, BUBBLE_MAX_TEXT_WIDTH)

        line_spacing = 4
        ascent, descent = font.getmetrics()
        line_height = ascent + descent

        text_width = 0
        text_height = 0
        for line in lines:
            width, _ = _text_size(font, line)
            text_width = max(text_width, width)
            text_height += line_height + line_spacing
        if lines:
            text_height -= line_spacing

        box_w = int(max(text_width, name_w) + 130)
        box_h = int(max(text_height + 103, 150))
        box = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))

        corners = self._load_corners()
        if corners is None:
            ImageDraw.Draw(box).rounded_rectangle((0, 0, box_w, box_h), radius=20, fill="white")
            return box

        box.paste(corners[0], (0, 0))
        box.paste(corners[1], (0, box_h - 75))
        box.paste(corners[2], (box_w - 70, 0))
        box.paste(corners[3], (box_w - 70, box_h - 75))

        draw = ImageDraw.Draw(box)
        draw.rectangle((65, 20, box_w - 65, box_h - 20), fill="white")
        draw.rectangle((26, 75, box_w - 26, box_h - 75), fill="white")

        start_x = 65
        start_y = 17 + (box_h - 40 - text_height) // 2
        self._draw_lines(box, draw, lines, font, (start_x, start_y), line_height + line_spacing, descent)
        return box

    def _draw_lines(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        lines: list[str],
        font: Any,
        origin: tuple[int, int],
        step: int,
        descent: int,
    ) -> None:
        x, y = origin
        if Pilmoji is not None:
            offset = (0, max(1, int(descent * 0.9)))
            try:
                with Pilmoji(canvas, emoji_position_offset=offset) as pilmoji:
                    for line in lines:
                        pilmoji.text((x, y), line, font=font, fill="black")
                        y += step
                return
            except Exception as exc:  # noqa: BLE001 - emoji 源不可用时退回纯文本
                logger.debug(f"{LOG_TAG} Pilmoji 渲染失败，退回纯文本: {exc}")
                y = origin[1]
        for line in lines:
            draw.text((x, y), line, font=font, fill="black")
            y += step

    def _draw_text_image(self, font: Any, text: str, fill: str) -> Image.Image:
        """把一段文字画到刚好裁切的透明图上（用于头衔标签）。"""
        bbox = font.getbbox(text) or (0, 0, 0, 0)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        image = Image.new("RGBA", (width + 20, height + 20), (0, 0, 0, 0))
        position = (-bbox[0] + 10, -bbox[1] + 10)
        drawn = False
        if Pilmoji is not None:
            try:
                _, descent = font.getmetrics()
                with Pilmoji(image, emoji_position_offset=(0, max(1, int(descent * 0.6)))) as pilmoji:
                    pilmoji.text(position, text, font=font, fill=fill)
                drawn = True
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"{LOG_TAG} Pilmoji 渲染头衔失败: {exc}")
        if not drawn:
            ImageDraw.Draw(image).text(position, text, font=font, fill=fill)
        cropped = image.getbbox()
        return image.crop(cropped) if cropped else image

    # ------------------------------------------------------------- 等级徽章

    def _make_level_badge(self, level: int) -> Image.Image:
        """LV + 数字的斜体组合。"""
        num_font = self.fonts.load(32, bold=True)
        prefix_font = self.fonts.load(28, bold=True)
        prefix, number = "LV", str(level)

        p_bbox = prefix_font.getbbox(prefix) or (0, 0, 0, 0)
        n_bbox = num_font.getbbox(number) or (0, 0, 0, 0)
        p_w, p_h = p_bbox[2] - p_bbox[0], p_bbox[3] - p_bbox[1]
        n_w, n_h = n_bbox[2] - n_bbox[0], n_bbox[3] - n_bbox[1]

        lv_w = p_w + n_w + 4
        lv_h = max(p_h, n_h)
        buffer = 40
        image = Image.new("RGBA", (lv_w + buffer, lv_h + buffer), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        n_top = (lv_h + buffer - n_h) // 2
        p_top = n_top + n_h - p_h
        draw.text((buffer // 2 - p_bbox[0], p_top - p_bbox[1]), prefix, font=prefix_font, fill="white")
        draw.text(
            (buffer // 2 + p_w + 4 - n_bbox[0], n_top - n_bbox[1]),
            number,
            font=num_font,
            fill="white",
        )

        italic = make_italic(image, 0.1)
        cropped = italic.getbbox()
        return italic.crop(cropped) if cropped else italic

    def _make_badge(self, role: str, title: str, level: int) -> Image.Image:
        """完整徽章：底色圆角 + 等级 + 头衔/段位。"""
        label_font = self.fonts.load(32)
        has_custom_title = bool(title)
        color = ROLE_COLORS.get(role, DEFAULT_BADGE_COLOR)
        if role == "member" and has_custom_title:
            color = CUSTOM_TITLE_COLOR

        final_title = title
        if not final_title:
            if role == "owner":
                final_title = "群主"
            elif role == "admin":
                final_title = "管理员"
            else:
                final_title = level_title(level)

        lv_img = self._make_level_badge(level)
        title_img = self._draw_text_image(label_font, final_title, "white") if final_title else None

        spacing = int(label_font.getlength(" ") * 1.5)
        content_w = lv_img.width
        content_h = lv_img.height
        if title_img is not None:
            content_w += spacing + title_img.width
            content_h = max(content_h, title_img.height)

        label_w = content_w + 28
        label_h = content_h + 20
        badge = Image.new("RGBA", (int(label_w), int(label_h)), (0, 0, 0, 0))
        ImageDraw.Draw(badge).rounded_rectangle((0, 0, label_w, label_h), radius=12, fill=color)

        cursor = (label_w - content_w) / 2
        badge.paste(lv_img, (int(cursor), int((label_h - lv_img.height) / 2)), mask=lv_img)
        cursor += lv_img.width
        if title_img is not None:
            cursor += spacing
            badge.paste(title_img, (int(cursor), int((label_h - title_img.height) / 2)), mask=title_img)
        return badge

    # ------------------------------------------------------------- 主渲染

    def render(
        self,
        *,
        name: str,
        avatar_bytes: bytes | None,
        text: str,
        role: str = "member",
        title: str = "",
        level: int = 0,
        show_title: bool = True,
    ) -> bytes:
        """渲染一张图片并返回 JPEG 字节。"""
        try:
            avatar = Image.open(io.BytesIO(avatar_bytes or b"")).convert("RGBA")
        except Exception:  # noqa: BLE001 - 头像拿不到就用灰底占位
            avatar = Image.new("RGBA", (AVATAR_SIZE, AVATAR_SIZE), "gray")
        avatar = avatar.resize((AVATAR_SIZE, AVATAR_SIZE))
        mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, AVATAR_SIZE, AVATAR_SIZE), fill=255)
        avatar.putalpha(mask)

        name_font = self.fonts.load(35)
        name_w, name_h = _text_size(name_font, name)

        badge = self._make_badge(role, title, level) if show_title else None
        box = self.make_dialog_box(text)

        name_x = BADGE_X + badge.width + 10 if badge is not None else BADGE_X
        canvas_w = int(max(name_x + name_w, BUBBLE_X + box.width) + 50)
        canvas_h = int(box.height + 110)
        canvas = Image.new("RGBA", (canvas_w, canvas_h), CANVAS_COLOR)

        canvas.paste(avatar, (20, 20), mask=avatar)
        canvas.paste(box, (BUBBLE_X, 82), mask=box)
        if badge is not None:
            canvas.paste(badge, (BADGE_X, 25), mask=badge)

        name_y = 20 + (35 - name_h) // 2
        drawn = False
        if Pilmoji is not None:
            try:
                _, descent = name_font.getmetrics()
                with Pilmoji(canvas, emoji_position_offset=(0, max(1, int(descent * 0.6)))) as pilmoji:
                    pilmoji.text((name_x, name_y), name, font=name_font, fill=NAME_COLOR)
                drawn = True
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"{LOG_TAG} Pilmoji 渲染昵称失败: {exc}")
        if not drawn:
            ImageDraw.Draw(canvas).text((name_x, name_y), name, font=name_font, fill=NAME_COLOR)

        output = io.BytesIO()
        canvas.convert("RGB").save(output, format="JPEG", quality=90)
        return output.getvalue()

    def stitch(self, images: list[bytes]) -> bytes | None:
        """把多张图片纵向拼接成一张 PNG。"""
        opened: list[Image.Image] = []
        try:
            for raw in images:
                try:
                    opened.append(Image.open(io.BytesIO(raw)))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"{LOG_TAG} 拼接时跳过损坏图片: {exc}")
            if not opened:
                return None
            width = max(img.width for img in opened)
            height = sum(img.height for img in opened)
            with Image.new("RGB", (width, height), CANVAS_COLOR) as canvas:
                offset = 0
                for img in opened:
                    canvas.paste(img, (0, offset))
                    offset += img.height
                output = io.BytesIO()
                canvas.save(output, format="PNG")
                return output.getvalue()
        finally:
            for img in opened:
                img.close()


def detect_image_ext(data: bytes, fallback: str = "png") -> str:
    """按图片实际格式推断扩展名。"""
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").lower()
    except Exception:  # noqa: BLE001
        return fallback
    if not fmt:
        return fallback
    return {"jpeg": "jpg", "tiff": "tif"}.get(fmt, fmt)

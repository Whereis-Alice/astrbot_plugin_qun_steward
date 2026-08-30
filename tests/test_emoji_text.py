"""emoji 混排切分 / 测量 / 换行 / 绘制的单元测试。

彩色 emoji 字体在 CI 上可能不存在，因此所有必跑用例都不依赖它；
只有明确检测到字体时才额外验证贴图路径。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from astrbot_plugin_qun_steward.features.album.emoji_text import (
    SYSTEM_EMOJI_FONTS,
    EmojiFont,
    TextPainter,
    iter_clusters,
    iter_segments,
)
from PIL import Image, ImageDraw, ImageFont

FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
FLAG_CN = "\U0001f1e8\U0001f1f3"
KEYCAP = "1\ufe0f\u20e3"
PARTY = "\U0001f389"


def _font(size: int = 24):
    return ImageFont.load_default(size)


# ------------------------------------------------------------------ 切分


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("abc", [("abc", False)]),
        (PARTY, [(PARTY, True)]),
        ("hi" + PARTY, [("hi", False), (PARTY, True)]),
        (PARTY + "hi", [(PARTY, True), ("hi", False)]),
        ("a" + FAMILY + "b", [("a", False), (FAMILY, True), ("b", False)]),
        (FLAG_CN, [(FLAG_CN, True)]),
        (KEYCAP, [(KEYCAP, True)]),
    ],
)
def test_iter_segments(text: str, expected: list[tuple[str, bool]]) -> None:
    assert list(iter_segments(text)) == expected


def test_iter_segments_is_lossless() -> None:
    text = "\u4f60\u597d" + PARTY + " world " + FAMILY + FLAG_CN + "\uff01"
    assert "".join(chunk for chunk, _ in iter_segments(text)) == text


def test_zwj_sequence_is_one_cluster() -> None:
    """ZWJ 组合 emoji 必须整体保留，否则换行会把一家人拆散。"""
    clusters = list(iter_clusters("a" + FAMILY + "b"))
    assert clusters == [("a", False), (FAMILY, True), ("b", False)]


def test_flag_pair_and_keycap_are_atomic() -> None:
    assert list(iter_clusters(FLAG_CN + KEYCAP)) == [(FLAG_CN, True), (KEYCAP, True)]


def test_plain_text_splits_per_character() -> None:
    assert list(iter_clusters("\u4e2d\u6587ab")) == [
        ("\u4e2d", False),
        ("\u6587", False),
        ("a", False),
        ("b", False),
    ]


def test_skin_tone_and_variation_selector_stay_attached() -> None:
    waving = "\U0001f44b\U0001f3fd"
    heart = "\u2764\ufe0f"
    assert list(iter_clusters(waving + heart)) == [(waving, True), (heart, True)]


@pytest.mark.parametrize("text", ["\u2192", "\u25a0", "\uff01", "3", "#"])
def test_plain_symbols_are_not_treated_as_emoji(text: str) -> None:
    """箭头、几何图形、全角标点、裸数字都不该被当成 emoji。"""
    assert list(iter_segments(text)) == [(text, False)]


# ------------------------------------------------------------------ 字体


def test_missing_emoji_font_is_unavailable() -> None:
    font = EmojiFont(None)
    assert font.available is False
    assert font.cell(PARTY, 32) is None


def test_broken_emoji_font_path_degrades_quietly(tmp_path) -> None:
    bad = tmp_path / "not-a-font.ttf"
    bad.write_bytes(b"definitely not a font")
    font = EmojiFont(bad)
    assert font.cell(PARTY, 32) is None
    assert font.available is False


# ------------------------------------------------------------------ 测量 / 换行


def test_measure_without_emoji_font_matches_plain_text() -> None:
    painter = TextPainter(EmojiFont(None))
    font = _font()
    assert painter.measure("hello", font) == sum(
        round(font.getlength(char)) for char in "hello"
    )


def test_wrap_keeps_paragraph_breaks() -> None:
    painter = TextPainter(EmojiFont(None))
    assert painter.wrap("a\n\nb", _font(), 10_000) == ["a", "", "b"]
    assert painter.wrap("a\r\nb", _font(), 10_000) == ["a", "b"]


def test_wrap_breaks_by_pixel_width() -> None:
    painter = TextPainter(EmojiFont(None))
    font = _font()
    lines = painter.wrap("\u6c49" * 20, font, painter.measure("\u6c49" * 5, font))
    assert len(lines) > 1
    assert "".join(lines) == "\u6c49" * 20


def test_wrap_never_splits_an_emoji_cluster() -> None:
    painter = TextPainter(EmojiFont(None))
    font = _font()
    text = (FAMILY + "\u6c49") * 6
    lines = painter.wrap(text, font, painter.measure(FAMILY, font))
    assert "".join(lines) == text
    for line in lines:
        assert "\u200d" not in line or FAMILY in line


def test_wrap_allows_a_single_overlong_cluster() -> None:
    """单个簇比整行还宽时也不能丢字。"""
    painter = TextPainter(EmojiFont(None))
    assert painter.wrap("\u6c49\u6c49", _font(), 1) == ["\u6c49", "\u6c49"]


# ------------------------------------------------------------------ 绘制


def test_draw_returns_consumed_width_and_matches_measure() -> None:
    painter = TextPainter(EmojiFont(None))
    font = _font()
    canvas = Image.new("RGBA", (400, 80), (0, 0, 0, 0))
    width = painter.draw(canvas, ImageDraw.Draw(canvas), (0, 0), "hello", font, "black")
    assert width == painter.measure("hello", font)
    assert canvas.getbbox() is not None


def test_draw_without_emoji_font_still_paints_something() -> None:
    painter = TextPainter(EmojiFont(None))
    canvas = Image.new("RGBA", (400, 120), (0, 0, 0, 0))
    painter.draw(canvas, ImageDraw.Draw(canvas), (4, 4), "hi" + PARTY, _font(32), "black")
    assert canvas.getbbox() is not None


def test_emoji_box_follows_font_ascent() -> None:
    painter = TextPainter(EmojiFont(None))
    small = painter.emoji_box(_font(16))
    large = painter.emoji_box(_font(64))
    assert 0 < small < large


# ------------------------------------------------------------------ 有彩色字体时的额外验证


def _system_emoji_font() -> EmojiFont | None:
    for candidate in SYSTEM_EMOJI_FONTS:
        path = Path(candidate)
        if path.is_file():
            font = EmojiFont(path)
            if font.cell(PARTY, 32) is not None:
                return font
    return None


def test_color_emoji_cell_has_requested_height() -> None:
    font = _system_emoji_font()
    if font is None:
        pytest.skip("\u672c\u673a\u6ca1\u6709\u53ef\u7528\u7684\u5f69\u8272 emoji \u5b57\u4f53")
    for box in (24, 48, 96):
        cell = font.cell(PARTY, box)
        assert cell is not None
        assert cell.height == box
        assert cell.width > 0
        assert cell.mode == "RGBA"


def test_color_emoji_cells_are_cached() -> None:
    font = _system_emoji_font()
    if font is None:
        pytest.skip("\u672c\u673a\u6ca1\u6709\u53ef\u7528\u7684\u5f69\u8272 emoji \u5b57\u4f53")
    assert font.cell(PARTY, 40) is font.cell(PARTY, 40)


def test_color_emoji_is_wider_than_bare_fallback() -> None:
    font = _system_emoji_font()
    if font is None:
        pytest.skip("\u672c\u673a\u6ca1\u6709\u53ef\u7528\u7684\u5f69\u8272 emoji \u5b57\u4f53")
    painter = TextPainter(font)
    main = _font(32)
    assert painter.measure(PARTY, main) >= painter.emoji_box(main)

"""绘图原语的单元测试：混色、圆角蒙版、描边、渐变、柔光、投影、盾牌轮廓。

这些函数被卡片渲染和 logo 脚本共用，行为一变两边同时受影响，所以逐个钉住关键性质：
尺寸、模式、哪里该有色、哪里必须留白。像素级的具体数值不做断言，Pillow 换版本会漂。
"""

from __future__ import annotations

import pytest
from astrbot_plugin_qun_steward.core.shapes import (
    SHIELD_RATIO,
    aa_circle,
    dot,
    drop_shadow,
    fill_gradient,
    fill_round,
    gradient,
    mix,
    qbezier,
    round_mask,
    shield_mask,
    shield_points,
    soft_light,
    stroke_round,
)
from PIL import Image

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
RED = (255, 0, 0)


def _canvas(width: int = 60, height: int = 40, color: tuple[int, int, int] = WHITE) -> Image.Image:
    return Image.new("RGB", (width, height), color)


# ==================================================================== 混色


class TestMix:
    def test_endpoints(self) -> None:
        assert mix(BLACK, WHITE, 0.0) == BLACK
        assert mix(BLACK, WHITE, 1.0) == WHITE

    def test_midpoint_is_rounded(self) -> None:
        assert mix(BLACK, WHITE, 0.5) == (128, 128, 128)

    def test_each_channel_is_independent(self) -> None:
        assert mix((0, 100, 200), (100, 100, 0), 0.5) == (50, 100, 100)


# ================================================================ 圆角蒙版


class TestAaCircle:
    def test_size_and_mode(self) -> None:
        circle = aa_circle(8)
        assert (circle.mode, circle.size) == ("L", (16, 16))

    def test_center_is_solid_and_corner_is_empty(self) -> None:
        circle = aa_circle(8)
        assert circle.getpixel((8, 8)) == 255
        assert circle.getpixel((0, 0)) == 0

    def test_result_is_cached(self) -> None:
        assert aa_circle(9) is aa_circle(9)

    def test_zero_radius_still_returns_an_image(self) -> None:
        assert aa_circle(0).size == (2, 2)


class TestRoundMask:
    def test_size_and_mode(self) -> None:
        mask = round_mask(30, 20, 8)
        assert (mask.mode, mask.size) == ("L", (30, 20))

    def test_corner_is_carved_and_center_is_solid(self) -> None:
        mask = round_mask(30, 20, 8)
        assert mask.getpixel((0, 0)) < 128
        assert mask.getpixel((15, 10)) == 255

    def test_zero_radius_is_solid(self) -> None:
        assert round_mask(6, 6, 0).getextrema() == (255, 255)

    def test_radius_is_capped_by_size(self) -> None:
        assert round_mask(10, 4, 40).size == (10, 4)

    def test_dropped_corners_stay_square(self) -> None:
        mask = round_mask(20, 20, 6, corners=(False, True, True, True))
        assert mask.getpixel((0, 0)) == 255
        assert mask.getpixel((19, 0)) < 128

    def test_degenerate_size_is_clamped_to_one_pixel(self) -> None:
        assert round_mask(0, 0, 4).size == (1, 1)


class TestFillRound:
    def test_inside_is_painted_and_corner_keeps_background(self) -> None:
        base = _canvas()
        fill_round(base, (10, 5, 50, 35), RED, 10)
        assert base.getpixel((30, 20)) == RED
        assert base.getpixel((10, 5)) != RED

    def test_outside_the_box_is_untouched(self) -> None:
        base = _canvas()
        fill_round(base, (10, 5, 50, 35), RED, 6)
        assert base.getpixel((2, 2)) == WHITE

    @pytest.mark.parametrize("box", [(10, 10, 10, 20), (10, 10, 20, 10), (30, 30, 10, 10)])
    def test_empty_box_is_a_noop(self, box: tuple[int, int, int, int]) -> None:
        base = _canvas()
        fill_round(base, box, RED, 4)
        assert base.getextrema() == ((255, 255), (255, 255), (255, 255))


class TestStrokeRound:
    def test_edge_is_painted_and_center_stays_clean(self) -> None:
        base = _canvas()
        stroke_round(base, (10, 5, 50, 35), RED, 8, 2)
        assert base.getpixel((30, 5)) == RED  # 上边
        assert base.getpixel((30, 20)) == WHITE  # 中心不填

    def test_thicker_stroke_covers_more_rows(self) -> None:
        thin, thick = _canvas(), _canvas()
        stroke_round(thin, (10, 5, 50, 35), RED, 8, 1)
        stroke_round(thick, (10, 5, 50, 35), RED, 8, 3)
        assert thin.getpixel((30, 7)) == WHITE
        assert thick.getpixel((30, 7)) == RED

    def test_box_thinner_than_the_stroke_is_a_noop(self) -> None:
        base = _canvas()
        stroke_round(base, (10, 10, 14, 20), RED, 2, 3)
        assert base.getextrema() == ((255, 255), (255, 255), (255, 255))


# ==================================================================== 渐变


class TestGradient:
    def test_size_and_endpoints(self) -> None:
        image = gradient(40, 40, BLACK, WHITE, (0.5, 0.5))
        assert image.size == (40, 40)
        assert sum(image.getpixel((0, 0))) < sum(image.getpixel((39, 39)))

    def test_horizontal_weight_ignores_rows(self) -> None:
        image = gradient(40, 40, BLACK, WHITE, (1.0, 0.0))
        assert image.getpixel((20, 2)) == image.getpixel((20, 37))
        assert sum(image.getpixel((2, 20))) < sum(image.getpixel((37, 20)))

    def test_vertical_weight_ignores_columns(self) -> None:
        image = gradient(40, 40, BLACK, WHITE, (0.0, 1.0))
        assert image.getpixel((2, 20)) == image.getpixel((37, 20))

    def test_degenerate_size_is_clamped_to_one_pixel(self) -> None:
        assert gradient(0, 0, BLACK, WHITE).size == (1, 1)

    def test_fill_gradient_paints_inside_only(self) -> None:
        base = _canvas()
        fill_gradient(base, (10, 5, 50, 35), BLACK, BLACK, 8)
        assert base.getpixel((30, 20)) == BLACK
        assert base.getpixel((2, 2)) == WHITE

    def test_fill_gradient_empty_box_is_a_noop(self) -> None:
        base = _canvas()
        fill_gradient(base, (10, 10, 10, 10), BLACK, BLACK, 4)
        assert base.getextrema() == ((255, 255), (255, 255), (255, 255))


class TestSoftLight:
    def test_covered_area_gets_brighter(self) -> None:
        layer = _canvas(60, 60, (100, 100, 100))
        soft_light(layer, (-10, -10, 30, 30), 90, 6)
        assert sum(layer.getpixel((5, 5))) > 300

    def test_far_corner_is_almost_untouched(self) -> None:
        layer = _canvas(60, 60, (100, 100, 100))
        soft_light(layer, (-10, -10, 20, 20), 90, 4)
        assert layer.getpixel((59, 59)) == (100, 100, 100)

    def test_alpha_is_clamped(self) -> None:
        layer = _canvas(30, 30, (100, 100, 100))
        soft_light(layer, (0, 0, 29, 29), 9999, 0)
        assert layer.getpixel((15, 15)) == WHITE


# ============================================================ 投影与圆点


class TestDropShadow:
    def test_shadow_appears_below_the_box(self) -> None:
        base = _canvas(80, 80)
        drop_shadow(base, (20, 20, 60, 50), 10)
        assert sum(base.getpixel((40, 54))) < sum(WHITE)

    def test_top_left_corner_stays_clean(self) -> None:
        base = _canvas(140, 140)
        drop_shadow(base, (60, 60, 120, 120), 10, blur=6, offset=4)
        assert base.getpixel((0, 0)) == WHITE

    def test_empty_box_is_a_noop(self) -> None:
        base = _canvas()
        drop_shadow(base, (10, 10, 10, 10), 4)
        assert base.getextrema() == ((255, 255), (255, 255), (255, 255))


class TestDot:
    def test_center_is_painted(self) -> None:
        base = _canvas()
        dot(base, (30, 20), 6, RED)
        assert base.getpixel((30, 20)) == RED

    def test_outside_the_radius_is_untouched(self) -> None:
        base = _canvas()
        dot(base, (30, 20), 6, RED)
        assert base.getpixel((30, 5)) == WHITE


# ==================================================================== 盾牌


class TestShield:
    def test_bezier_hits_both_ends(self) -> None:
        points = qbezier((0.0, 0.0), (5.0, 10.0), (10.0, 0.0), steps=4)
        assert len(points) == 5
        assert points[0] == (0.0, 0.0)
        assert points[-1] == (10.0, 0.0)

    def test_outline_stays_inside_its_box(self) -> None:
        points = shield_points(50, 10, 40, 48)
        assert all(30 - 1e-6 <= x <= 70 + 1e-6 for x, _ in points)
        assert all(10 - 1e-6 <= y <= 58 + 1e-6 for _, y in points)

    def test_outline_has_a_bottom_tip_on_the_axis(self) -> None:
        points = shield_points(50, 10, 40, 48)
        lowest = max(points, key=lambda point: point[1])
        assert lowest[0] == pytest.approx(50)
        assert lowest[1] == pytest.approx(58)

    def test_mask_height_follows_the_shield_ratio(self) -> None:
        mask = shield_mask(40)
        assert (mask.mode, mask.size) == ("L", (40, round(40 * SHIELD_RATIO)))

    def test_shoulder_is_solid_and_bottom_corner_is_empty(self) -> None:
        mask = shield_mask(40)
        assert mask.getpixel((20, 4)) == 255
        assert mask.getpixel((1, mask.height - 2)) == 0

    def test_tiny_width_is_floored(self) -> None:
        assert shield_mask(1).size[0] == 4

    def test_result_is_cached(self) -> None:
        assert shield_mask(24) is shield_mask(24)

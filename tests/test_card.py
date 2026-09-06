"""卡片渲染的单元测试：Markdown 解析、文字降级、布局自适应、旧图清理。

渲染用例只断言尺寸、类型、是否抛异常这类稳定属性。CI 上大概率没有中文字体，
Pillow 会回落到内置位图字体，逐像素比对没有意义。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest
from astrbot_plugin_qun_steward.core import card as card_module
from astrbot_plugin_qun_steward.core.card import (
    DEFAULT_WIDTH,
    WIDE_WIDTH,
    Bar,
    Bullets,
    Card,
    CardRenderer,
    Divider,
    Heading,
    KeyValue,
    Note,
    Rank,
    RankRow,
    Stat,
    Stats,
    Table,
    Tags,
    Text,
    from_markdown,
)
from astrbot_plugin_qun_steward.core.config import DISPLAY_NAME
from astrbot_plugin_qun_steward.core.fonts import FontResolver
from PIL import Image


@pytest.fixture
def renderer(config: Any) -> CardRenderer:
    """真实渲染器：找不到字体时 FontResolver 会退回 Pillow 内置字体，不影响断言。"""
    return CardRenderer(FontResolver(config))


def _touch(path: Path, age: float = 0.0) -> Path:
    """造一个卡片文件，age 是「多少秒前」写的。"""
    path.write_bytes(b"x")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


# ============================================================ Markdown 解析


class TestFromMarkdown:
    def test_hash_line_becomes_title(self) -> None:
        card = from_markdown("# 群务帮助\n随便一行正文")
        assert card.title == "群务帮助"
        assert card.blocks == [Text("随便一行正文")]

    def test_bracket_line_becomes_title_and_subtitle(self) -> None:
        card = from_markdown("【群信息】群号 10001\n正文")
        assert (card.title, card.subtitle) == ("群信息", "群号 10001")

    def test_given_title_is_kept(self) -> None:
        card = from_markdown("正文", title="外部标题", badge="标记", footer="脚注")
        assert (card.title, card.badge, card.footer) == ("外部标题", "标记", "脚注")

    def test_missing_title_falls_back_to_display_name(self) -> None:
        assert from_markdown("只有正文").title == DISPLAY_NAME

    def test_empty_text_yields_empty_card(self) -> None:
        card = from_markdown("")
        assert card.title == DISPLAY_NAME
        assert card.blocks == []

    @pytest.mark.parametrize("line", ["## 小节", "#### 小节", "—— 小节 ——", "=== 小节 ==="])
    def test_section_headings(self, line: str) -> None:
        assert from_markdown("# 标题\n" + line).blocks == [Heading("小节")]

    def test_bullets_are_merged_into_one_block(self) -> None:
        card = from_markdown("# 标题\n- 甲\n- 乙\n* 丙")
        assert card.blocks == [Bullets(["甲", "乙", "丙"])]

    def test_ordered_list_keeps_order_flag(self) -> None:
        card = from_markdown("# 标题\n1. 甲\n2. 乙")
        assert card.blocks == [Bullets(["甲", "乙"], ordered=True)]

    def test_key_value_lines_are_merged(self) -> None:
        card = from_markdown("# 标题\n群号：10001\n成员: 233")
        assert card.blocks == [KeyValue([("群号", "10001"), ("成员", "233")])]

    def test_key_without_value_stays_text(self) -> None:
        assert from_markdown("# 标题\n群介绍：").blocks == [Text("群介绍：")]

    def test_quote_becomes_note(self) -> None:
        assert from_markdown("# 标题\n> 只有管理员能用").blocks == [Note("只有管理员能用")]

    def test_rule_becomes_divider(self) -> None:
        card = from_markdown("# 标题\n上半\n\n---\n\n下半")
        assert card.blocks == [Text("上半"), Divider(), Text("下半")]

    def test_table_first_row_is_header(self) -> None:
        card = from_markdown("# 标题\n| 文件 | 大小 |\n| --- | --- |\n| a.zip | 1 MB |")
        assert card.blocks == [Table(headers=["文件", "大小"], rows=[["a.zip", "1 MB"]])]

    def test_single_table_row_has_no_header(self) -> None:
        card = from_markdown("# 标题\n| 只有一行 | 数据 |")
        assert card.blocks == [Table(headers=[], rows=[["只有一行", "数据"]])]

    def test_inline_marks_are_stripped(self) -> None:
        card = from_markdown("# 标题\n**加粗** 与 " + "\x60代码\x60" + " 与 *斜体*")
        assert card.blocks == [Text("加粗 与 代码 与 斜体")]

    def test_muted_prefix_becomes_small_gray_text(self) -> None:
        card = from_markdown("# 标题\n……仅显示前 5 条")
        assert card.blocks == [Text("……仅显示前 5 条", tone="muted", small=True)]

    def test_blank_line_splits_paragraphs(self) -> None:
        card = from_markdown("# 标题\n甲\n乙\n\n丙")
        assert card.blocks == [Text("甲\n乙"), Text("丙")]

    def test_wide_canvas_for_many_columns(self) -> None:
        card = from_markdown("# 标题\n| a | b | c | d |\n| - | - | - | - |\n| 1 | 2 | 3 | 4 |")
        assert card.width == WIDE_WIDTH

    def test_narrow_table_keeps_default_width(self) -> None:
        card = from_markdown("# 标题\n| a | b |\n| - | - |\n| 1 | 2 |")
        assert card.width == 0


# ================================================================ 文字降级


class TestToText:
    def test_head_merges_title_and_subtitle(self) -> None:
        card = Card(title="群信息", subtitle="群号 10001")
        assert card.to_text() == "【群信息】群号 10001"

    def test_subtitle_alone_is_kept(self) -> None:
        assert Card(subtitle="仅副标题").to_text() == "仅副标题"

    def test_every_block_type_degrades_to_text(self) -> None:
        card = Card(title="标题")
        card.add(
            Text("正文"),
            Heading("小节", note="共 2 条"),
            KeyValue([("群号", "10001")]),
            Bar(label="容量", ratio=0.5, value="1 GB", note="剩 1 GB"),
            Stats([Stat(label="成员", value="233", note="上限 500")]),
            Bullets(["甲", "乙"]),
            Bullets(["甲"], ordered=True),
            Rank([RankRow(name="张三", value="3 天", note="龙王")]),
            Tags([("开", "ok"), ("关", "muted")]),
            Table(headers=["名", "值"], rows=[["a", "1"]]),
            Note("提示"),
        )
        text = card.to_text()
        assert text.startswith("【标题】")
        for expect in (
            "正文",
            "—— 小节 —— 共 2 条",
            "群号：10001",
            "容量：1 GB（50%）",
            "剩 1 GB",
            "成员 233（上限 500）",
            "· 甲",
            "1. 甲",
            "1. 张三 3 天（龙王）",
            "开 / 关",
            "名 | 值",
            "a | 1",
            "提示",
        ):
            assert expect in text

    def test_trailing_blank_lines_are_trimmed(self) -> None:
        card = Card(title="标题", blocks=[Text("正文"), Divider()])
        assert card.to_text() == "【标题】\n正文"

    def test_markdown_round_trip_keeps_content(self) -> None:
        source = "# 群信息\n群号：10001\n\n- 甲\n- 乙"
        text = from_markdown(source).to_text()
        assert "【群信息】" in text
        assert "群号：10001" in text
        assert "· 甲" in text


class TestCardAdd:
    def test_none_is_ignored_and_self_is_returned(self) -> None:
        card = Card(title="标题")
        assert card.add(Text("正文"), None, Divider()) is card
        assert len(card.blocks) == 2


# ================================================================== 渲染


class TestRender:
    def test_basic_card_is_rgb_and_default_width(self, renderer: CardRenderer) -> None:
        image = renderer.render(Card(title="标题", subtitle="副标题", badge="群务", blocks=[Text("正文")]))
        assert image.mode == "RGB"
        assert image.width == DEFAULT_WIDTH
        assert image.height > 0

    def test_height_grows_with_content(self, renderer: CardRenderer) -> None:
        short = renderer.render(Card(title="标题", blocks=[Text("一行")]))
        tall = renderer.render(Card(title="标题", blocks=[Text("一行")] * 8))
        assert tall.height > short.height

    def test_empty_blocks_take_no_space(self, renderer: CardRenderer) -> None:
        bare = renderer.render(Card(title="标题"))
        padded = renderer.render(
            Card(
                title="标题",
                blocks=[
                    Text("   "),
                    Heading(""),
                    KeyValue(),
                    Stats(),
                    Bullets([]),
                    Rank([]),
                    Tags([]),
                    Table(),
                    Note(" "),
                ],
            )
        )
        assert padded.height == bare.height

    def test_all_block_types_render(self, renderer: CardRenderer) -> None:
        card = Card(title="全量", subtitle="覆盖每种块", badge="测试", footer="脚注")
        card.add(
            Text("正文"),
            Text("小字", tone="muted", small=True),
            Heading("小节", note="附注"),
            KeyValue([("键", "值"), ("很长的键名占位", "很长的值" * 20)]),
            Bar(label="容量", ratio=0.95, value="19 GB"),
            Stats([Stat(label=f"格{index}", value=str(index)) for index in range(5)]),
            Bullets(["甲", "乙"]),
            Bullets(["甲", "乙"], ordered=True),
            Rank([RankRow(name=f"第{index}", value="9", weight=index / 4) for index in range(5)]),
            Tags([("开", "ok"), ("警", "warn"), ("错", "err"), ("灰", "muted")]),
            Table(headers=["甲", "乙"], rows=[["1", "2"], ["3", "4"]]),
            Note("提示", tone="warn"),
            Divider(),
        )
        image = renderer.render(card)
        assert image.height > 400

    def test_wide_card_uses_wide_width(self, renderer: CardRenderer) -> None:
        card = Card(title="标题", blocks=[Text("正文")], width=WIDE_WIDTH)
        assert renderer.render(card).width == WIDE_WIDTH

    def test_tiny_width_is_floored(self, renderer: CardRenderer) -> None:
        card = Card(title="标题", blocks=[Text("正文")], width=100)
        assert renderer.render(card).width == 480

    def test_overflow_is_truncated(
        self, renderer: CardRenderer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        card = Card(title="超长", blocks=[Text(f"第 {index} 行") for index in range(40)])
        full = renderer.render(card).height
        monkeypatch.setattr(card_module, "_MAX_HEIGHT", 420)
        assert renderer.render(card).height < full

    def test_unknown_block_falls_back_to_text(self, renderer: CardRenderer) -> None:
        piece = renderer._prepare(object(), 600)  # type: ignore[arg-type]
        assert isinstance(piece.block, Text)
        assert piece.height > 0

    def test_card_with_unknown_block_still_renders(self, renderer: CardRenderer) -> None:
        card = Card(title="标题")
        card.blocks.append(object())  # type: ignore[arg-type]
        assert renderer.render(card).height > 0

    def test_painter_is_cached(self, renderer: CardRenderer) -> None:
        assert renderer.painter is renderer.painter

    def test_painter_rebuilds_after_font_change(self, renderer: CardRenderer) -> None:
        first = renderer.painter
        renderer.fonts.invalidate()
        assert renderer.painter is not first


# ============================================================ 组件布局细节


class TestStatsColumns:
    @pytest.mark.parametrize(
        ("count", "expected"),
        [(1, 1), (2, 2), (3, 3), (4, 2), (5, 3), (6, 3), (7, 3)],
    )
    def test_auto_columns(self, renderer: CardRenderer, count: int, expected: int) -> None:
        block = Stats([Stat(label="标签", value="1") for _ in range(count)])
        assert renderer._prep_stats(block, 800).data[0] == expected

    def test_explicit_columns_win(self, renderer: CardRenderer) -> None:
        block = Stats([Stat(label="标签", value="1") for _ in range(6)], columns=2)
        assert renderer._prep_stats(block, 800).data[0] == 2

    def test_columns_never_exceed_item_count(self, renderer: CardRenderer) -> None:
        block = Stats([Stat(label="标签", value="1")], columns=4)
        assert renderer._prep_stats(block, 800).data[0] == 1

    def test_empty_stats_have_no_height(self, renderer: CardRenderer) -> None:
        assert renderer._prep_stats(Stats(), 800).height == 0


class TestBar:
    @pytest.mark.parametrize(
        ("ratio", "expected"),
        [(0.0, "brand"), (0.69, "brand"), (0.7, "warn"), (0.89, "warn"), (0.9, "err"), (1.5, "err")],
    )
    def test_tone_follows_ratio(self, ratio: float, expected: str) -> None:
        assert CardRenderer._bar_tone(Bar(label="容量", ratio=ratio)) == expected

    def test_explicit_tone_wins(self) -> None:
        assert CardRenderer._bar_tone(Bar(label="容量", ratio=0.99, tone="ok")) == "ok"

    @pytest.mark.parametrize(("ratio", "clamped"), [(-1.0, 0.0), (0.4, 0.4), (9.0, 1.0)])
    def test_ratio_is_clamped(self, renderer: CardRenderer, ratio: float, clamped: float) -> None:
        assert renderer._prep_bar(Bar(label="容量", ratio=ratio), 600).data[3] == clamped

    def test_value_defaults_to_percent(self, renderer: CardRenderer) -> None:
        assert renderer._prep_bar(Bar(label="容量", ratio=0.42), 600).data[1] == "42%"


# ============================================================ 落盘与清理


class TestSave:
    def test_save_writes_readable_png(self, renderer: CardRenderer, tmp_path: Path) -> None:
        path = renderer.save(Card(title="标题", blocks=[Text("正文")]), tmp_path / "cards")
        assert path.name.startswith("card-") and path.suffix == ".png"
        with Image.open(path) as image:
            assert image.format == "PNG"
            assert image.width == DEFAULT_WIDTH

    def test_save_creates_missing_directory(self, renderer: CardRenderer, tmp_path: Path) -> None:
        dest = tmp_path / "a" / "b"
        assert renderer.save(Card(title="标题"), dest).parent == dest

    def test_save_sweeps_stale_files(self, renderer: CardRenderer, tmp_path: Path) -> None:
        stale = _touch(tmp_path / "card-1.png", age=7200)
        renderer.save(Card(title="标题"), tmp_path)
        assert not stale.exists()


class TestSweep:
    def test_keeps_only_the_newest(self, tmp_path: Path) -> None:
        for index in range(6):
            _touch(tmp_path / f"card-{index}.png", age=6 - index)
        card_module._sweep_cards(tmp_path, keep=2, ttl=600)
        assert sorted(item.name for item in tmp_path.glob("card-*.png")) == [
            "card-4.png",
            "card-5.png",
        ]

    def test_removes_expired_even_within_quota(self, tmp_path: Path) -> None:
        fresh = _touch(tmp_path / "card-new.png")
        old = _touch(tmp_path / "card-old.png", age=1200)
        card_module._sweep_cards(tmp_path, keep=40, ttl=600)
        assert fresh.exists() and not old.exists()

    def test_other_files_are_left_alone(self, tmp_path: Path) -> None:
        keep = _touch(tmp_path / "note.png", age=99999)
        card_module._sweep_cards(tmp_path, keep=1, ttl=1)
        assert keep.exists()

    def test_missing_directory_is_ignored(self, tmp_path: Path) -> None:
        card_module._sweep_cards(tmp_path / "nope")


# ================================================================ 绘图工具


class TestHelpers:
    def test_line_height_follows_ratio(self) -> None:
        assert card_module._lh(20) == round(20 * card_module._LINE_RATIO)

    def test_unknown_tone_falls_back_to_body_color(self) -> None:
        assert card_module._ink("不存在的色调") == card_module.TEXT

    def test_soft_backdrop_stays_close_to_panel(self) -> None:
        soft = card_module._soft("err")
        assert all(channel > 200 for channel in soft)
        assert soft != card_module.PANEL

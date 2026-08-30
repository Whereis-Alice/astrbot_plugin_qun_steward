"""core.utils 里的纯函数。"""

from __future__ import annotations

import time

import pytest
from astrbot_plugin_qun_steward.core.utils import (
    apply_delta,
    format_date,
    format_datetime,
    format_duration,
    format_size,
    list_text,
    parse_bool,
    parse_int,
    parse_time_range,
    sanitize_filename,
    split_tokens,
    switch_text,
)


class TestParseBool:
    @pytest.mark.parametrize("raw", ["开", "开启", "启用", "on", "TRUE", "yes", "1", "是"])
    def test_true_words(self, raw: str) -> None:
        assert parse_bool(raw) is True

    @pytest.mark.parametrize("raw", ["关", "关闭", "禁用", "off", "False", "no", "0", "否"])
    def test_false_words(self, raw: str) -> None:
        assert parse_bool(raw) is False

    def test_bool_passthrough(self) -> None:
        assert parse_bool(True) is True
        assert parse_bool(False) is False

    def test_unrecognized_returns_default(self) -> None:
        # 返回 None 让调用方把「没给参数」和「设为关」区分开
        assert parse_bool("随便写点什么") is None
        assert parse_bool(None) is None
        assert parse_bool("") is None
        assert parse_bool("", default=True) is True


class TestParseInt:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("12", 12), ("+3", 3), ("-4", -4), ("  7  ", 7), (9, 9), (True, 1)],
    )
    def test_valid(self, raw: object, expected: int) -> None:
        assert parse_int(raw) == expected

    @pytest.mark.parametrize("raw", ["3.5", "十", "", None, "1e3"])
    def test_invalid_returns_default(self, raw: object) -> None:
        assert parse_int(raw) is None
        assert parse_int(raw, 5) == 5


def test_switch_text() -> None:
    assert switch_text(True) == "开"
    assert switch_text(False) == "关"
    assert switch_text("包含匹配") == "包含匹配"


def test_list_text() -> None:
    assert list_text([]) == "（空）"
    assert list_text(None, empty="无") == "无"
    assert list_text(["a", "b"]) == "a、b"
    assert list_text(("a",)) == "a"
    assert list_text("已经是文本") == "已经是文本"


def test_format_date_and_datetime() -> None:
    now = int(time.time())
    assert format_date(now) == time.strftime("%Y-%m-%d", time.localtime(now))
    assert format_datetime(now) == time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    assert format_date("坏数据") == "未知"
    assert format_datetime(None) == "未知"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0秒"),
        (-10, "0秒"),
        (59, "59秒"),
        (60, "1分钟"),
        (3720, "1小时2分钟"),
        (90061, "1天1小时1分钟1秒"),
    ],
)
def test_format_duration(seconds: int, expected: str) -> None:
    assert format_duration(seconds) == expected


@pytest.mark.parametrize(
    ("num_bytes", "expected"),
    [(0, "0 B"), (512, "512 B"), (2048, "2.00 KB"), (1048576, "1.00 MB")],
)
def test_format_size(num_bytes: int, expected: str) -> None:
    assert format_size(num_bytes) == expected


def test_sanitize_filename() -> None:
    assert sanitize_filename("日常/2026:03") == "日常_2026_03"
    assert sanitize_filename("") == "default"
    assert sanitize_filename("   ", fallback="兜底") == "兜底"
    assert sanitize_filename("正常名字") == "正常名字"


class TestParseTimeRange:
    def test_basic(self) -> None:
        assert parse_time_range("30~300") == (30, 300)

    def test_full_width_tilde_and_dash(self) -> None:
        assert parse_time_range("30～60") == (30, 60)
        assert parse_time_range("5-10") == (5, 10)

    def test_swaps_reversed_bounds(self) -> None:
        assert parse_time_range("300~30") == (30, 300)

    def test_clamps_to_legal_range(self) -> None:
        # 0 会被抬到 1，超过 30 天会被压到 30 天（QQ 单次禁言上限）
        assert parse_time_range("0~99999999") == (1, 2592000)

    @pytest.mark.parametrize("raw", ["", None, "乱写", "30~", "~30"])
    def test_invalid_returns_fallback(self, raw: object) -> None:
        assert parse_time_range(raw) == (30, 300)
        assert parse_time_range(raw, fallback=(1, 2)) == (1, 2)


def test_split_tokens() -> None:
    assert split_tokens("禁言 10 秒") == ["禁言", "10", "秒"]
    assert split_tokens("全角\u3000空格  也切") == ["全角", "空格", "也切"]
    assert split_tokens("   ") == []
    assert split_tokens("") == []


class TestApplyDelta:
    def test_plain_tokens_overwrite_and_dedupe(self) -> None:
        assert apply_delta(["旧词"], ["新词", "另一个", "新词"]) == (["新词", "另一个"], [], [])

    def test_delta_add_and_remove(self) -> None:
        result, added, removed = apply_delta(["a", "b"], ["+c", "-a"])
        assert result == ["b", "c"]
        assert added == ["c"]
        assert removed == ["a"]

    def test_adding_existing_is_noop(self) -> None:
        assert apply_delta(["a"], ["+a"]) == (["a"], [], [])

    def test_removing_missing_is_noop(self) -> None:
        assert apply_delta(["a"], ["-z"]) == (["a"], [], [])

    def test_empty_tokens_keep_current(self) -> None:
        assert apply_delta(["a", "a", "b"], []) == (["a", "b"], [], [])

    def test_bare_sign_is_ignored(self) -> None:
        assert apply_delta(["a"], ["+", "-"]) == (["a"], [], [])

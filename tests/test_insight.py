"""群情报卡片里的协议字段归一化。"""

from __future__ import annotations

import pytest
from astrbot_plugin_qun_steward.features.insight import _search_enabled, _whole_ban_enabled


class TestWholeBanEnabled:
    @pytest.mark.parametrize("value", [-1, "-1", True, 1, "1", "true", "开"])
    def test_enabled_values(self, value: object) -> None:
        assert _whole_ban_enabled(value) is True

    @pytest.mark.parametrize("value", [0, "0", False, "false", "关"])
    def test_disabled_values(self, value: object) -> None:
        assert _whole_ban_enabled(value) is False

    @pytest.mark.parametrize("value", [None, "", -2, "unknown", 2])
    def test_unknown_values(self, value: object) -> None:
        assert _whole_ban_enabled(value) is None


class TestSearchEnabled:
    def test_direct_field_wins(self) -> None:
        assert _search_enabled({"group_search": False, "no_finger_open": 0}) is False

    @pytest.mark.parametrize(
        ("settings", "expected"),
        [
            ({"no_finger_open": 0}, True),
            ({"no_finger_open": 1}, False),
            ({"no_code_finger_open": "0"}, True),
            ({"no_code_finger_open": "1"}, False),
            ({"no_finger_open": False}, True),
            ({"no_finger_open": True}, False),
        ],
    )
    def test_reverse_fields(self, settings: dict[str, object], expected: bool) -> None:
        assert _search_enabled(settings) is expected

    def test_missing_fields_are_unknown(self) -> None:
        assert _search_enabled({}) is None

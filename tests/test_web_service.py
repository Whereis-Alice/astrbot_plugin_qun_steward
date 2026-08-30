"""WebUI 数据服务层里的值规整逻辑。"""

from __future__ import annotations

from typing import Any

import pytest
from astrbot_plugin_qun_steward.web.service import (
    SETTING_SCALARS,
    SETTING_SECTIONS,
    _coerce,
)


class TestCoerceBool:
    @pytest.mark.parametrize("raw", [True, "true", "on", "开", "1", 1])
    def test_truthy(self, raw: Any) -> None:
        assert _coerce({"type": "bool"}, raw) is True

    @pytest.mark.parametrize("raw", [False, "false", "off", "关", "0", None, "乱写"])
    def test_falsy_or_unparsable(self, raw: Any) -> None:
        # 前端传来任何看不懂的值都收敛成 False，绝不让开关处于半开状态
        assert _coerce({"type": "bool"}, raw) is False


class TestCoerceNumbers:
    @pytest.mark.parametrize(("raw", "expected"), [("60", 60), (60, 60), ("-5", -5)])
    def test_int(self, raw: Any, expected: int) -> None:
        assert _coerce({"type": "int"}, raw) == expected

    @pytest.mark.parametrize("raw", ["abc", None, "", "1.5"])
    def test_int_invalid_becomes_zero(self, raw: Any) -> None:
        assert _coerce({"type": "int"}, raw) == 0

    @pytest.mark.parametrize(("raw", "expected"), [("0.5", 0.5), (2, 2.0), ("-1.25", -1.25)])
    def test_float(self, raw: Any, expected: float) -> None:
        assert _coerce({"type": "float"}, raw) == expected

    @pytest.mark.parametrize("raw", ["abc", None, ""])
    def test_float_invalid_becomes_zero(self, raw: Any) -> None:
        assert _coerce({"type": "float"}, raw) == 0.0


class TestCoerceList:
    def test_list_input_is_trimmed(self) -> None:
        assert _coerce({"type": "list"}, [" a ", "b", "", "  "]) == ["a", "b"]

    @pytest.mark.parametrize(
        "raw",
        ["a b", "a,b", "a，b", " a  b ", "a, b"],
    )
    def test_text_input_is_split(self, raw: str) -> None:
        assert _coerce({"type": "list"}, raw) == ["a", "b"]

    @pytest.mark.parametrize("raw", [None, 0, {}])
    def test_other_types_become_empty(self, raw: Any) -> None:
        assert _coerce({"type": "list"}, raw) == []


class TestCoerceTemplateList:
    def test_keeps_list_of_objects(self) -> None:
        rows = [{"group_id": "1", "album_name": "日常"}]
        assert _coerce({"type": "template_list"}, rows) == rows

    def test_non_list_becomes_empty(self) -> None:
        assert _coerce({"type": "template_list"}, "坏数据") == []


class TestCoerceString:
    def test_default_type_is_string(self) -> None:
        assert _coerce({}, 123) == "123"

    def test_none_becomes_empty_string(self) -> None:
        assert _coerce({"type": "string"}, None) == ""

    def test_unknown_type_is_treated_as_string(self) -> None:
        assert _coerce({"type": "没见过的类型"}, "值") == "值"


class TestSettingMaps:
    def test_sections_exist_in_schema(self, plugin_dir: Any) -> None:
        import json

        schema = json.loads((plugin_dir / "_conf_schema.json").read_text(encoding="utf-8"))
        for key in SETTING_SECTIONS:
            assert key in schema, "WebUI 声明的配置分区在模板里不存在：" + key
            assert schema[key].get("type") == "object"

    def test_scalars_exist_in_schema(self, plugin_dir: Any) -> None:
        import json

        schema = json.loads((plugin_dir / "_conf_schema.json").read_text(encoding="utf-8"))
        for key in SETTING_SCALARS:
            assert key in schema, "WebUI 声明的顶层配置在模板里不存在：" + key

    def test_titles_are_non_empty(self) -> None:
        assert all(SETTING_SECTIONS.values())
        assert all(SETTING_SCALARS.values())

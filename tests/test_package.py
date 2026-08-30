"""仓库完整性校验：元数据、配置模板、logo、WebUI 页面与 i18n 文案。"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path
from typing import Any

import pytest
import yaml
from astrbot_plugin_qun_steward.core.config import DISPLAY_NAME, PLUGIN_NAME
from astrbot_plugin_qun_steward.core.permission import PERM_OPTIONS
from astrbot_plugin_qun_steward.core.store import FIELD_LABELS

# 这两项不属于「分群可覆写」的配置模板 items，只在插件全局配置里出现。
_NON_TEMPLATE_FIELDS = {"admin_audit", "level_threshold"}


@pytest.fixture
def metadata(plugin_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((plugin_dir / "metadata.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def schema(plugin_dir: Path) -> dict[str, Any]:
    return json.loads((plugin_dir / "_conf_schema.json").read_text(encoding="utf-8"))


class TestMetadata:
    def test_required_fields_present(self, metadata: dict[str, Any]) -> None:
        for key in ("name", "display_name", "desc", "help", "version", "author", "repo"):
            assert metadata.get(key), f"metadata.yaml 缺少字段 {key}"

    def test_name_matches_code(self, metadata: dict[str, Any]) -> None:
        assert metadata["name"] == PLUGIN_NAME
        assert metadata["display_name"] == DISPLAY_NAME

    def test_repo_matches_plugin_name(self, metadata: dict[str, Any]) -> None:
        assert str(metadata["repo"]).rstrip("/").endswith(PLUGIN_NAME)

    def test_version_format(self, metadata: dict[str, Any]) -> None:
        assert re.fullmatch(r"v\d+\.\d+\.\d+", str(metadata["version"]))

    def test_platform_and_tags(self, metadata: dict[str, Any]) -> None:
        assert metadata["support_platforms"] == ["aiocqhttp"]
        assert isinstance(metadata["tags"], list) and metadata["tags"]

    def test_directory_name_matches(self, plugin_dir: Path) -> None:
        assert plugin_dir.name == PLUGIN_NAME


class TestRequirements:
    """依赖清单的硬约束。

    AstrBot 4.27+ 在加载插件前会真的 import 插件私有 site-packages 里的
    依赖模块做预检；一旦某个包和运行时版本不兼容就直接拒绝加载整个插件。
    pilmoji 2.0.4 只兼容 emoji 1.x，而 AstrBot 运行时带 emoji>=2，
    两者同时出现会让插件装不上，因此彩色 emoji 改为自研实现。
    """

    #: 绝不能重新出现在 requirements.txt 里的包
    FORBIDDEN = ("pilmoji", "emoji")

    @pytest.fixture
    def requirements(self, plugin_dir: Path) -> list[str]:
        raw = (plugin_dir / "requirements.txt").read_text(encoding="utf-8")
        return [line.strip() for line in raw.splitlines() if line.strip() and not line.startswith("#")]

    def test_no_conflicting_packages(self, requirements: list[str]) -> None:
        for line in requirements:
            name = re.split(r"[<>=!~[; ]", line, maxsplit=1)[0].strip().lower()
            assert name not in self.FORBIDDEN, (
                f"{name} 会触发 AstrBot 依赖预检冲突，导致插件无法安装：{line}"
            )

    def test_expected_packages_present(self, requirements: list[str]) -> None:
        names = {re.split(r"[<>=!~[; ]", line, maxsplit=1)[0].strip().lower() for line in requirements}
        assert {"aiosqlite", "apscheduler", "aiohttp", "pillow"} <= names

    def test_no_pinned_upper_bound_on_pillow(self, requirements: list[str]) -> None:
        """Pillow 用下限而非固定版本，避免和宿主环境已装的版本互斥。"""
        pillow = next(line for line in requirements if line.lower().startswith("pillow"))
        assert "==" not in pillow


class TestSourceHygiene:
    def test_no_pilmoji_import_left_in_source(self, plugin_dir: Path) -> None:
        offenders = []
        for path in sorted(plugin_dir.rglob("*.py")):
            if "tests" in path.parts or path.name.startswith("_probe"):
                continue
            text = path.read_text(encoding="utf-8")
            if "pilmoji" in text.lower() and "import" in text.lower():
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith(("import ", "from ")) and "pilmoji" in stripped.lower():
                        offenders.append(f"{path.name}: {stripped}")
        assert not offenders, f"仍有 pilmoji 导入：{offenders}"


class TestConfSchema:
    def test_group_default_items_match_field_labels(self, schema: dict[str, Any]) -> None:
        items = set(schema["default"]["items"])
        assert items == set(FIELD_LABELS) - _NON_TEMPLATE_FIELDS

    def test_every_item_has_type_and_hint(self, schema: dict[str, Any]) -> None:
        for name, meta in schema["default"]["items"].items():
            assert meta.get("type"), f"{name} 缺少 type"
            assert meta.get("description"), f"{name} 缺少 description"

    def test_perm_defaults_are_valid_options(self, schema: dict[str, Any]) -> None:
        perms = schema["perms"]["items"]
        assert len(perms) >= 30
        for key, meta in perms.items():
            assert meta["type"] == "string", f"{key} 权限项应为 string"
            assert meta["default"] in PERM_OPTIONS, f"{key} 默认权限非法：{meta['default']}"
            assert meta.get("options") == PERM_OPTIONS, f"{key} 缺少或错误的 options"

    def test_top_level_keys_are_stable(self, schema: dict[str, Any]) -> None:
        # admins_id / timezone 来自 AstrBot 全局配置，不在插件模板里重复声明。
        assert set(schema) == {
            "default",
            "admin_audit",
            "random_ban_time",
            "level_threshold",
            "vote_ban",
            "safety",
            "audit",
            "album",
            "fonts",
            "llm_get_msg_count",
            "enable_llm_tools",
            "perms",
        }

    def test_scalar_entries_have_defaults(self, schema: dict[str, Any]) -> None:
        for key in ("admin_audit", "random_ban_time", "level_threshold", "enable_llm_tools"):
            assert "default" in schema[key], f"{key} 缺少默认值"
            assert schema[key].get("description"), f"{key} 缺少说明"

    def test_sections_are_objects(self, schema: dict[str, Any]) -> None:
        for key in ("default", "perms", "vote_ban", "safety", "audit", "album", "fonts"):
            assert schema[key]["type"] == "object"
            assert schema[key].get("items"), f"{key} 分区为空"


class TestAssets:
    def test_logo_is_square_png(self, plugin_dir: Path) -> None:
        raw = (plugin_dir / "logo.png").read_bytes()
        assert raw[:8] == b"\x89PNG\r\n\x1a\n", "logo.png 不是合法 PNG"
        width, height = struct.unpack(">II", raw[16:24])
        assert width == height == 512, f"logo 尺寸应为 512x512，实际 {width}x{height}"

    def test_requirements_not_empty(self, plugin_dir: Path) -> None:
        lines = [
            line.strip()
            for line in (plugin_dir / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        assert lines

    def test_license_is_gplv3(self, plugin_dir: Path) -> None:
        text = (plugin_dir / "LICENSE").read_text(encoding="utf-8")
        assert "GNU GENERAL PUBLIC LICENSE" in text
        assert "Version 3" in text

    def test_readme_mentions_upstream_credit(self, plugin_dir: Path) -> None:
        text = (plugin_dir / "README.md").read_text(encoding="utf-8")
        assert "astrbot_plugin_qqadmin" in text
        assert "astrbot_plugin_qun_album" in text


def _flatten(node: Any, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                keys |= _flatten(value, path)
            else:
                keys.add(path)
    return keys


class TestWebUi:
    def test_page_json_valid(self, plugin_dir: Path) -> None:
        page = json.loads((plugin_dir / "pages/dashboard/_page.json").read_text(encoding="utf-8"))
        assert page["title"]["i18n_key"]
        assert page["description"]["i18n_key"]

    def test_index_html_loads_app_js(self, plugin_dir: Path) -> None:
        html = (plugin_dir / "pages/dashboard/index.html").read_text(encoding="utf-8")
        assert "app.js" in html
        assert "style.css" in html

    def test_index_html_declares_ids_used_by_app_js(self, plugin_dir: Path) -> None:
        """app.js 通过 getElementById 拿外壳节点，缺一个就是整页白屏。"""
        page_dir = plugin_dir / "pages/dashboard"
        app_js = page_dir.joinpath("app.js").read_text(encoding="utf-8")
        html = page_dir.joinpath("index.html").read_text(encoding="utf-8")
        wanted = set(re.findall(r'getElementById\("([^"]+)"\)', app_js))
        assert wanted, "未能从 app.js 提取到任何 element id"
        missing = sorted(name for name in wanted if f'id="{name}"' not in html)
        assert not missing, f"index.html 缺少 app.js 需要的 id：{missing}"

    def test_style_css_covers_classes_used_by_app_js(self, plugin_dir: Path) -> None:
        """避免 app.js 改了结构而皮肤没跟上，出现没有样式的裸元素。"""
        page_dir = plugin_dir / "pages/dashboard"
        app_js = page_dir.joinpath("app.js").read_text(encoding="utf-8")
        css = page_dir.joinpath("style.css").read_text(encoding="utf-8")
        tokens: set[str] = set()
        for pattern in (r'class:\s*"([^"]*)"', r'\bcls\s*=\s*"([^"]*)"'):
            for chunk in re.findall(pattern, app_js):
                tokens.update(part for part in chunk.split() if part)
        # 三元里拼接的片段，例如 " active" / " overridden"
        tokens.update(re.findall(r'"\s+([a-z][a-z0-9-]*)"', app_js))
        assert len(tokens) > 30, "未能从 app.js 提取到足够的 class"
        missing = sorted(
            name for name in tokens if not re.search(r"\." + re.escape(name) + r"(?![\w-])", css)
        )
        assert not missing, f"style.css 缺少这些 class 的样式：{missing}"

    def test_index_html_uses_relative_assets(self, plugin_dir: Path) -> None:
        """AstrBot 会重写相对路径并补 asset_token，写死绝对路径会 404。"""
        html = (plugin_dir / "pages/dashboard/index.html").read_text(encoding="utf-8")
        assert './style.css' in html
        assert './app.js' in html

    @pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
    def test_i18n_covers_all_app_keys(self, plugin_dir: Path, locale: str) -> None:
        app_js = (plugin_dir / "pages/dashboard/app.js").read_text(encoding="utf-8")
        used = set(re.findall(r't\("([^"]+)",\s*"[^"]*"\)', app_js))
        assert len(used) > 100, "未能从 app.js 提取到足够的 i18n key"

        page = json.loads((plugin_dir / "pages/dashboard/_page.json").read_text(encoding="utf-8"))
        used.add(page["title"]["i18n_key"])
        used.add(page["description"]["i18n_key"])

        data = json.loads(
            (plugin_dir / ".astrbot-plugin/i18n" / f"{locale}.json").read_text(encoding="utf-8")
        )
        available = _flatten(data)
        missing = sorted(used - available)
        assert not missing, f"{locale} 缺少文案：{missing}"

    def test_i18n_locales_have_same_shape(self, plugin_dir: Path) -> None:
        i18n_dir = plugin_dir / ".astrbot-plugin/i18n"
        zh = _flatten(json.loads((i18n_dir / "zh-CN.json").read_text(encoding="utf-8")))
        en = _flatten(json.loads((i18n_dir / "en-US.json").read_text(encoding="utf-8")))
        assert zh == en, f"两种语言 key 不一致：{sorted(zh ^ en)}"

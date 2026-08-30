"""协议端探测与相册响应归一化。"""

from __future__ import annotations

from typing import Any

import pytest
from astrbot_plugin_qun_steward.core.protocol import (
    LLBOT,
    NAPCAT,
    SNOWLUMA,
    album_name_of,
    backend_label,
    clear_backend_cache,
    detect_backend,
    normalize_album_list,
)


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    clear_backend_cache()
    yield
    clear_backend_cache()


class _FakeApi:
    def __init__(self, app_name: str | None, raise_error: bool = False) -> None:
        self._app_name = app_name
        self._raise = raise_error
        self.calls: list[str] = []

    async def call_action(self, action: str, **_kwargs: Any) -> Any:
        self.calls.append(action)
        if self._raise:
            raise RuntimeError("协议端没响应")
        if self._app_name is None:
            return None
        return {"data": {"app_name": self._app_name}}


class _FakeClient:
    def __init__(self, app_name: str | None, raise_error: bool = False) -> None:
        self.api = _FakeApi(app_name, raise_error)


class TestBackendLabel:
    def test_known_backends(self) -> None:
        assert backend_label(NAPCAT) == "NapCat"
        assert backend_label(LLBOT) == "LLOneBot / llbot"
        assert backend_label(SNOWLUMA) == "SnowLuma"

    def test_unknown_backend_is_shown_as_is(self) -> None:
        assert backend_label("某个新实现") == "某个新实现"


class TestDetectBackend:
    @pytest.mark.parametrize(
        ("app_name", "expected"),
        [
            ("NapCat.Onebot", NAPCAT),
            ("LLOneBot", LLBOT),
            ("llonebot", LLBOT),
            ("SnowLuma", SNOWLUMA),
            ("snowluma-onebot", SNOWLUMA),
        ],
    )
    async def test_recognizes_app_name(self, app_name: str, expected: str) -> None:
        assert await detect_backend(_FakeClient(app_name)) == expected

    async def test_unknown_app_name_falls_back_to_napcat(self) -> None:
        # NapCat 的接口兼容性最好，识别失败时按它处理最安全
        assert await detect_backend(_FakeClient("SomeOtherBot")) == NAPCAT

    async def test_api_error_falls_back_to_napcat(self) -> None:
        assert await detect_backend(_FakeClient("", raise_error=True)) == NAPCAT

    async def test_missing_client_falls_back_to_napcat(self) -> None:
        assert await detect_backend(None) == NAPCAT

    async def test_result_is_cached_per_client(self) -> None:
        client = _FakeClient("LLOneBot")
        assert await detect_backend(client) == LLBOT
        assert await detect_backend(client) == LLBOT
        # 只应该打一次 get_version_info
        assert client.api.calls == ["get_version_info"]

    async def test_clear_cache_forces_redetect(self) -> None:
        client = _FakeClient("LLOneBot")
        await detect_backend(client)
        clear_backend_cache()
        await detect_backend(client)
        assert client.api.calls == ["get_version_info", "get_version_info"]


class TestNormalizeAlbumList:
    def test_plain_list(self) -> None:
        assert normalize_album_list([{"album_id": "1", "name": "日常"}]) == [
            {"album_id": "1", "name": "日常"}
        ]

    @pytest.mark.parametrize("key", ["data", "album_list", "list", "albums"])
    def test_wrapped_in_common_keys(self, key: str) -> None:
        payload = {key: [{"album_id": "1", "name": "日常"}]}
        assert normalize_album_list(payload)[0]["name"] == "日常"

    def test_double_wrapped_payload(self) -> None:
        payload = {"data": {"album_list": [{"id": 7, "album_name": "旅行"}]}}
        albums = normalize_album_list(payload)
        assert albums == [{"id": 7, "album_name": "旅行", "album_id": "7", "name": "旅行"}]

    @pytest.mark.parametrize("alias", ["id", "albumId", "album_no"])
    def test_album_id_aliases(self, alias: str) -> None:
        albums = normalize_album_list([{alias: 42, "name": "x"}])
        assert albums[0]["album_id"] == "42"

    def test_album_name_aliases(self) -> None:
        albums = normalize_album_list([{"album_id": "1", "album_name": "旅行"}])
        assert albums[0]["name"] == "旅行"

    def test_missing_fields_become_empty_strings(self) -> None:
        albums = normalize_album_list([{}])
        assert albums == [{"album_id": "", "name": ""}]

    @pytest.mark.parametrize("payload", [None, "", 0, {"unexpected": 1}, {"data": "文本"}])
    def test_unusable_payload_returns_empty(self, payload: Any) -> None:
        assert normalize_album_list(payload) == []

    def test_non_dict_items_are_skipped(self) -> None:
        assert normalize_album_list([{"album_id": "1"}, "坏数据", None]) == [
            {"album_id": "1", "name": ""}
        ]


def test_album_name_of() -> None:
    assert album_name_of({"name": "日常"}) == "日常"
    assert album_name_of({"album_name": "旅行"}) == "旅行"
    assert album_name_of({}) == ""

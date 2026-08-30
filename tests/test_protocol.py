"""协议端探测与相册响应归一化。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from astrbot_plugin_qun_steward.core.protocol import (
    LLBOT,
    NAPCAT,
    SNOWLUMA,
    album_name_of,
    backend_label,
    clear_backend_cache,
    create_album,
    detect_backend,
    extract_failure,
    find_album,
    list_albums,
    normalize_album_list,
    supports_album_create,
    upload_album_image,
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


# --------------------------------------------------------------- 三端相册适配

_MISSING = object()


class _ScriptedApi:
    """按动作名返回预设响应；未预设的动作直接抛错，模拟协议端没有该接口。"""

    def __init__(self, app_name: str, responses: dict[str, Any]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses: dict[str, Any] = {
            "get_version_info": {"data": {"app_name": app_name}},
            **responses,
        }

    async def call_action(self, action: str, **kwargs: Any) -> Any:
        self.calls.append((action, kwargs))
        value = self._responses.get(action, _MISSING)
        if value is _MISSING:
            raise RuntimeError(f"协议端不支持动作 {action}")
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(kwargs)
        return value

    @property
    def actions(self) -> list[str]:
        return [name for name, _ in self.calls if name != "get_version_info"]


class _ScriptedEvent:
    """list_albums / upload_album_image 只用到 event.bot。"""

    def __init__(self, app_name: str, responses: dict[str, Any]) -> None:
        self.bot = type("_Bot", (), {"api": _ScriptedApi(app_name, responses)})()

    @property
    def api(self) -> _ScriptedApi:
        return self.bot.api


def _ok(data: Any) -> dict[str, Any]:
    return {"status": "ok", "retcode": 0, "data": data}


#: llbot 风格：蛇形字段
_LLBOT_ALBUMS = _ok([{"album_id": "a1", "name": "日常", "upload_number": 3}])
#: SnowLuma 风格：驼峰字段，主键叫 id
_SNOWLUMA_ALBUMS = _ok([{"id": "s1", "name": "日常", "picNum": 3}])


@pytest.fixture
def image(tmp_path: Path) -> Path:
    target = tmp_path / "pic.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    return target


class TestExtractFailure:
    def test_normal_success(self) -> None:
        assert extract_failure(_ok({"success_count": 1, "fail_count": 0})) == ""

    def test_non_dict_payload_is_success(self) -> None:
        assert extract_failure(None) == ""
        assert extract_failure([1, 2]) == ""

    def test_retcode_failure(self) -> None:
        assert "retcode" in extract_failure({"status": "ok", "retcode": 1200})

    @pytest.mark.parametrize("key", ["message", "wording", "msg"])
    def test_failed_status_uses_message(self, key: str) -> None:
        assert extract_failure({"status": "failed", key: "没权限"}) == "没权限"

    def test_llbot_fake_success_is_detected(self) -> None:
        # llbot 上传失败时依然是 status=ok / retcode=0
        payload = _ok({"success_count": 0, "fail_count": 1, "fail_indexes": [0]})
        assert "fail_count=1" in extract_failure(payload)

    def test_fail_indexes_only(self) -> None:
        assert "fail_indexes" in extract_failure(_ok({"fail_indexes": [0, 2]}))

    def test_zero_success_with_fail_count_field(self) -> None:
        assert extract_failure(_ok({"success_count": 0, "fail_count": 0})) != ""


class TestListAlbums:
    async def test_llbot_uses_group_album_list(self) -> None:
        event = _ScriptedEvent("LLOneBot", {"get_group_album_list": _LLBOT_ALBUMS})
        albums = await list_albums(event, "123")  # type: ignore[arg-type]
        assert [a["album_id"] for a in albums] == ["a1"]
        assert event.api.actions == ["get_group_album_list"]

    async def test_snowluma_prefers_qun_album_list(self) -> None:
        # SnowLuma 两个动作都有，get_qun_album_list 才是与 NapCat 对齐的那个
        event = _ScriptedEvent(
            "SnowLuma",
            {"get_qun_album_list": _SNOWLUMA_ALBUMS, "get_group_album_list": _LLBOT_ALBUMS},
        )
        albums = await list_albums(event, 123)  # type: ignore[arg-type]
        assert event.api.actions == ["get_qun_album_list"]
        assert albums[0]["album_id"] == "s1"
        assert albums[0]["name"] == "日常"

    async def test_falls_back_to_next_action(self) -> None:
        event = _ScriptedEvent("SnowLuma", {"get_group_album_list": _LLBOT_ALBUMS})
        albums = await list_albums(event, 123)  # type: ignore[arg-type]
        assert event.api.actions == ["get_qun_album_list", "get_group_album_list"]
        assert albums[0]["album_id"] == "a1"

    async def test_napcat_uses_qun_album_list(self) -> None:
        event = _ScriptedEvent("NapCat.Onebot", {"get_qun_album_list": _SNOWLUMA_ALBUMS})
        await list_albums(event, 123)  # type: ignore[arg-type]
        assert event.api.actions == ["get_qun_album_list"]

    async def test_all_actions_failing_returns_empty(self) -> None:
        event = _ScriptedEvent("NapCat", {})
        assert await list_albums(event, 123) == []  # type: ignore[arg-type]

    async def test_failure_payload_is_skipped(self) -> None:
        event = _ScriptedEvent(
            "SnowLuma",
            {
                "get_qun_album_list": {"status": "failed", "message": "群相册未开通"},
                "get_group_album_list": _LLBOT_ALBUMS,
            },
        )
        albums = await list_albums(event, 123)  # type: ignore[arg-type]
        assert albums[0]["album_id"] == "a1"

    async def test_find_album_falls_back_to_contains(self) -> None:
        event = _ScriptedEvent(
            "LLOneBot",
            {"get_group_album_list": _ok([{"album_id": "a1", "name": "2026 日常"}])},
        )
        album = await find_album(event, 123, "日常")  # type: ignore[arg-type]
        assert album is not None
        assert album["album_id"] == "a1"


class TestUploadAlbumImage:
    async def test_llbot_sends_file_uri_first_in_array(self, image: Path) -> None:
        event = _ScriptedEvent(
            "LLOneBot", {"upload_group_album": _ok({"success_count": 1, "fail_count": 0})}
        )
        assert await upload_album_image(event, 123, "a1", "日常", image) == LLBOT  # type: ignore[arg-type]
        action, params = event.api.calls[-1]
        assert action == "upload_group_album"
        assert params["files"] == [image.resolve().as_uri()]
        assert "album_name" not in params  # llbot 没有这个参数

    async def test_llbot_fake_success_raises(self, image: Path) -> None:
        failure = _ok({"success_count": 0, "fail_count": 1, "fail_indexes": [0]})
        event = _ScriptedEvent("LLOneBot", {"upload_group_album": failure})
        with pytest.raises(RuntimeError, match="fail_count=1"):
            await upload_album_image(event, 123, "a1", "日常", image)  # type: ignore[arg-type]
        # 三种 file 形式都试过才放弃
        assert event.api.actions == ["upload_group_album"] * 3

    async def test_retries_next_candidate_after_rejection(self, image: Path) -> None:
        seen: list[str] = []

        def handler(kwargs: dict[str, Any]) -> Any:
            seen.append(kwargs["files"][0])
            if len(seen) == 1:
                return _ok({"success_count": 0, "fail_count": 1})
            return _ok({"success_count": 1, "fail_count": 0})

        event = _ScriptedEvent("LLOneBot", {"upload_group_album": handler})
        assert await upload_album_image(event, 123, "a1", "日常", image) == LLBOT  # type: ignore[arg-type]
        assert seen[0].startswith("file://")
        assert seen[1].startswith("base64://")

    async def test_snowluma_sends_album_name(self, image: Path) -> None:
        event = _ScriptedEvent("SnowLuma", {"upload_image_to_qun_album": _ok({})})
        assert await upload_album_image(event, 123, "s1", "日常", image) == SNOWLUMA  # type: ignore[arg-type]
        action, params = event.api.calls[-1]
        assert action == "upload_image_to_qun_album"
        assert params["album_name"] == "日常"
        assert params["file"] == str(image.resolve())

    async def test_unsupported_action_raises(self, image: Path) -> None:
        event = _ScriptedEvent("NapCat", {})
        with pytest.raises(RuntimeError, match="不支持动作"):
            await upload_album_image(event, 123, "a1", "日常", image)  # type: ignore[arg-type]


class TestCreateAlbum:
    def test_only_llbot_supports_create(self) -> None:
        assert supports_album_create(LLBOT) is True
        assert supports_album_create(SNOWLUMA) is False
        assert supports_album_create(NAPCAT) is False

    async def test_creates_then_requeries(self) -> None:
        event = _ScriptedEvent(
            "LLOneBot",
            {
                "create_group_album": _ok({"album_id": "a9"}),
                "get_group_album_list": _ok([{"album_id": "a9", "name": "日常"}]),
            },
        )
        album = await create_album(event, 123, "日常")  # type: ignore[arg-type]
        assert album is not None
        assert album["album_id"] == "a9"
        create_call = next(c for c in event.api.calls if c[0] == "create_group_album")
        assert create_call[1] == {"group_id": 123, "name": "日常", "desc": ""}

    async def test_returns_none_on_unsupported_backend(self) -> None:
        event = _ScriptedEvent("SnowLuma", {})
        assert await create_album(event, 123, "日常") is None  # type: ignore[arg-type]
        assert event.api.actions == []

    async def test_returns_none_when_rejected(self) -> None:
        event = _ScriptedEvent(
            "LLOneBot", {"create_group_album": {"status": "failed", "message": "没权限"}}
        )
        assert await create_album(event, 123, "日常") is None  # type: ignore[arg-type]

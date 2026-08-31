"""v1.1.0 新增能力的单元测试。

覆盖：进群申请对账的响应归一化、AI 声聊音色解析、群文件整理规则解析、
群相册云端缓存与失效。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from astrbot_plugin_qun_steward.core import protocol
from astrbot_plugin_qun_steward.features.album import cloud as cloud_mod
from astrbot_plugin_qun_steward.features.album.cloud import AlbumCloud, PickedImage
from astrbot_plugin_qun_steward.features.base import FeatureContext
from astrbot_plugin_qun_steward.features.files import FilesFeature
from astrbot_plugin_qun_steward.features.join import _normalize_requests
from astrbot_plugin_qun_steward.features.voice import VoiceFeature, _flatten_characters

GID = "10001"


@pytest.fixture(autouse=True)
def _clean_caches() -> Any:
    protocol.clear_backend_cache()
    protocol.clear_action_cache()
    yield
    protocol.clear_backend_cache()
    protocol.clear_action_cache()


class _FakeApi:
    """只认 get_version_info 和一个业务动作的最小协议端。"""

    def __init__(self, action: str, payload: Any) -> None:
        self._action = action
        self._payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_action(self, action: str, **params: Any) -> Any:
        self.calls.append((action, params))
        if action == "get_version_info":
            return {"data": {"app_name": "NapCat.Onebot"}}
        if action != self._action:
            raise RuntimeError("unsupported action")
        return {"status": "ok", "retcode": 0, "data": self._payload}


class _FakeEvent:
    def __init__(self, action: str = "", payload: Any = None, group_id: str = GID) -> None:
        self.api = _FakeApi(action, payload)
        self.bot = SimpleNamespace(api=self.api)
        self._group_id = group_id

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_id(self) -> str:
        return "20002"

    def get_sender_name(self) -> str:
        return "阿狸"


# --------------------------------------------------------------- 进群对账


class TestNormalizeRequests:
    def test_napcat_dict_of_lists(self) -> None:
        payload = {
            "join_requests": [
                {"flag": "f1", "requester_uin": "1", "requester_nick": "甲", "message": "求进"}
            ],
            "InvitedRequest": [
                {"request_id": "f2", "requester_uin": "2", "invitor_uin": "9"}
            ],
        }
        items = _normalize_requests(payload)
        assert [item["flag"] for item in items] == ["f1", "f2"]
        assert items[0]["nickname"] == "甲"
        assert items[0]["comment"] == "求进"
        # 邀请入群会在备注里标出邀请人
        assert "邀请入群" in items[1]["comment"]

    def test_plain_list_with_camel_case_fields(self) -> None:
        payload = [{"requestId": "f3", "requesterUin": "3", "requesterNick": "丙"}]
        items = _normalize_requests(payload)
        assert items == [
            {"flag": "f3", "group_id": "", "user_id": "3", "nickname": "丙", "comment": ""}
        ]

    def test_onebot_wrapper_is_unwrapped(self) -> None:
        payload = {
            "status": "ok",
            "retcode": 0,
            "data": [{"flag": "f4", "user_id": 4, "comment": "hi"}],
        }
        items = _normalize_requests(payload)
        assert items[0]["flag"] == "f4"
        assert items[0]["user_id"] == "4"

    def test_handled_entries_are_dropped(self) -> None:
        payload = [
            {"flag": "f5", "user_id": "5", "checked": True},
            {"flag": "f6", "user_id": "6", "checked": False},
        ]
        assert [item["flag"] for item in _normalize_requests(payload)] == ["f6"]

    def test_entries_without_flag_or_user_are_dropped(self) -> None:
        payload = [{"user_id": "7"}, {"flag": "f8"}, "垃圾数据"]
        assert _normalize_requests(payload) == []

    def test_nickname_falls_back_to_user_id(self) -> None:
        items = _normalize_requests([{"flag": "f9", "user_id": "9"}])
        assert items[0]["nickname"] == "9"

    def test_zero_invitor_is_ignored(self) -> None:
        items = _normalize_requests(
            [{"flag": "fa", "user_id": "10", "invitor_uin": "0", "message": "你好"}]
        )
        assert items[0]["comment"] == "你好"


# --------------------------------------------------------------- AI 声聊


class TestFlattenCharacters:
    def test_nested_structure(self) -> None:
        payload = [
            {
                "type": "推荐",
                "characters": [
                    {"character_id": "lucy", "character_name": "露西"},
                    {"character_id": "tom", "character_name": "汤姆"},
                ],
            }
        ]
        assert _flatten_characters(payload) == [("露西", "lucy"), ("汤姆", "tom")]

    def test_flat_structure_and_camel_case(self) -> None:
        payload = [{"characterId": "a", "characterName": "甲"}, {"id": "b", "name": "乙"}]
        assert _flatten_characters(payload) == [("甲", "a"), ("乙", "b")]

    def test_duplicates_are_removed_and_name_defaults_to_id(self) -> None:
        payload = [
            {"characters": [{"character_id": "x"}]},
            {"characters": [{"character_id": "x", "character_name": "重复"}]},
        ]
        assert _flatten_characters(payload) == [("x", "x")]

    def test_garbage_payload_is_empty(self) -> None:
        assert _flatten_characters(None) == []
        assert _flatten_characters({"foo": "bar"}) == []


def _voice(make_config: Any, **overrides: Any) -> VoiceFeature:
    recorded: list[dict[str, Any]] = []

    async def record(**kwargs: Any) -> None:
        recorded.append(kwargs)

    ctx = FeatureContext(
        context=None,  # type: ignore[arg-type]
        config=make_config(**overrides),
        store=None,  # type: ignore[arg-type]
        permissions=None,  # type: ignore[arg-type]
        audit=SimpleNamespace(record=record),  # type: ignore[arg-type]
        undo=None,  # type: ignore[arg-type]
        groups=None,  # type: ignore[arg-type]
        db=None,  # type: ignore[arg-type]
        to_image=None,  # type: ignore[arg-type]
    )
    feature = VoiceFeature(ctx)
    feature.recorded = recorded  # type: ignore[attr-defined]
    return feature


_CHARACTERS = [
    {
        "type": "推荐",
        "characters": [
            {"character_id": "lucy", "character_name": "露西"},
            {"character_id": "tom", "character_name": "汤姆"},
        ],
    }
]


def _voice_event() -> _FakeEvent:
    return _FakeEvent("get_ai_characters", _CHARACTERS)


class TestVoiceResolve:
    async def test_default_uses_first_character(self, make_config: Any) -> None:
        voice = _voice(make_config)
        character, label, text = await voice._resolve(_voice_event(), "今天天气不错")
        assert (character, label, text) == ("lucy", "露西", "今天天气不错")

    async def test_leading_index_switches_character(self, make_config: Any) -> None:
        voice = _voice(make_config)
        character, label, text = await voice._resolve(_voice_event(), "2 念台词")
        assert (character, label, text) == ("tom", "汤姆", "念台词")

    async def test_leading_name_switches_character(self, make_config: Any) -> None:
        voice = _voice(make_config)
        character, _, text = await voice._resolve(_voice_event(), "汤姆 念台词")
        assert (character, text) == ("tom", "念台词")

    async def test_out_of_range_index_is_treated_as_text(self, make_config: Any) -> None:
        voice = _voice(make_config)
        character, _, text = await voice._resolve(_voice_event(), "5 念台词")
        assert (character, text) == ("lucy", "5 念台词")

    async def test_configured_default_character_wins(self, make_config: Any) -> None:
        voice = _voice(make_config, voice={"default_character": "汤姆"})
        character, label, _ = await voice._resolve(_voice_event(), "念台词")
        assert (character, label) == ("tom", "汤姆")

    async def test_single_word_is_text_not_character(self, make_config: Any) -> None:
        # 只有一个词时不该被当成音色名，否则台词就没了
        voice = _voice(make_config)
        _, _, text = await voice._resolve(_voice_event(), "汤姆")
        assert text == "汤姆"


class TestVoiceSpeak:
    async def test_private_chat_is_rejected(self, make_config: Any) -> None:
        voice = _voice(make_config)
        event = _FakeEvent("get_ai_characters", _CHARACTERS, group_id="")
        assert "只能在群里" in await voice.speak(event, "念点什么")

    async def test_empty_text_is_rejected(self, make_config: Any) -> None:
        voice = _voice(make_config)
        assert "请给出要念的台词" in await voice.speak(_voice_event(), "   ")

    async def test_too_long_text_is_rejected(self, make_config: Any) -> None:
        voice = _voice(make_config)
        assert "台词太长" in await voice.speak(_voice_event(), "啊" * 400)

    async def test_no_character_available(self, make_config: Any) -> None:
        voice = _voice(make_config)
        event = _FakeEvent("get_ai_characters", [])
        assert "没有可用音色" in await voice.speak(event, "念点什么")

    async def test_unsupported_backend_reports_failure(self, make_config: Any) -> None:
        voice = _voice(make_config, voice={"default_character": "lucy"})
        event = _FakeEvent("get_ai_characters", _CHARACTERS)
        reply = await voice.speak(event, "念点什么")
        # 音色能拿到，但 send_group_ai_record 这个动作没实现
        assert reply.startswith("声聊失败")
        assert voice.recorded[-1]["success"] is False

    async def test_success_is_silent_and_audited(self, make_config: Any) -> None:
        voice = _voice(make_config)
        event = _voice_event()

        async def call_action(action: str, **params: Any) -> Any:
            if action == "get_version_info":
                return {"data": {"app_name": "NapCat.Onebot"}}
            if action == "get_ai_characters":
                return {"status": "ok", "retcode": 0, "data": _CHARACTERS}
            if action == "send_group_ai_record":
                event.api.calls.append((action, params))
                return {"status": "ok", "retcode": 0, "data": {}}
            raise RuntimeError("unsupported action")

        event.bot.api.call_action = call_action  # type: ignore[method-assign]
        assert await voice.speak(event, "今天天气不错") == ""
        sent = [item for item in event.api.calls if item[0] == "send_group_ai_record"]
        assert sent and sent[0][1]["character"] == "lucy"
        assert sent[0][1]["text"] == "今天天气不错"
        assert voice.recorded[-1]["action"] == "voice"

    async def test_character_list_is_cached_per_group(self, make_config: Any) -> None:
        voice = _voice(make_config)
        event = _voice_event()
        await voice.characters(event)
        await voice.characters(event)
        hits = [item for item in event.api.calls if item[0] == "get_ai_characters"]
        assert len(hits) == 1


# --------------------------------------------------------------- 群文件整理


class TestTidyRule:
    def test_default_is_loose(self) -> None:
        assert FilesFeature._tidy_rule([]) == ("loose", 0, 0)

    @pytest.mark.parametrize("token", ["散落", "根目录", "未归档"])
    def test_loose_aliases(self, token: str) -> None:
        assert FilesFeature._tidy_rule([token])[0] == "loose"

    @pytest.mark.parametrize("token", ["过期", "已过期"])
    def test_expired_aliases(self, token: str) -> None:
        assert FilesFeature._tidy_rule([token]) == ("expired", 0, 0)

    def test_days_rule(self) -> None:
        assert FilesFeature._tidy_rule(["30天"]) == ("days", 30, 0)

    @pytest.mark.parametrize("token", ["100M", "100MB", "100mb", "大于100M"])
    def test_size_rule(self, token: str) -> None:
        rule, _, size = FilesFeature._tidy_rule([token])
        assert (rule, size) == ("size", 100 * 1024 * 1024)

    def test_last_token_wins(self) -> None:
        assert FilesFeature._tidy_rule(["过期", "7天"]) == ("days", 7, 0)

    def test_unknown_token_is_ignored(self) -> None:
        assert FilesFeature._tidy_rule(["随便写点什么"]) == ("loose", 0, 0)

    def test_rule_label_covers_every_rule(self) -> None:
        for rule, days, size in (
            ("loose", 0, 0),
            ("expired", 0, 0),
            ("days", 7, 0),
            ("size", 0, 1024 * 1024),
        ):
            assert FilesFeature._rule_label(rule, days, size)


# --------------------------------------------------------------- 相册云端


class TestPickedImage:
    def test_source_prefers_local_path(self, tmp_path: Any) -> None:
        target = tmp_path / "a.png"
        assert PickedImage(path=target).source == str(target)

    def test_source_falls_back_to_url(self) -> None:
        assert PickedImage(url="https://example.com/a.png").source == (
            "https://example.com/a.png"
        )


class TestAlbumCloud:
    def test_enabled_follows_config(self, make_config: Any) -> None:
        assert AlbumCloud(make_config()).enabled is True
        assert AlbumCloud(make_config(album={"cloud_random": False})).enabled is False

    async def test_medias_are_cached(self, make_config: Any, monkeypatch: Any) -> None:
        calls: list[tuple[Any, Any]] = []

        async def fake_list(event: Any, gid: Any, aid: Any, *, limit: int = 0) -> Any:
            calls.append((gid, aid))
            return [{"media_id": "m1", "url": "https://example.com/1.png"}]

        monkeypatch.setattr(cloud_mod, "list_album_media", fake_list)
        album = AlbumCloud(make_config())
        await album.medias(None, GID, "a1")
        await album.medias(None, GID, "a1")
        assert len(calls) == 1

    async def test_refresh_bypasses_cache(self, make_config: Any, monkeypatch: Any) -> None:
        calls: list[Any] = []

        async def fake_list(event: Any, gid: Any, aid: Any, *, limit: int = 0) -> Any:
            calls.append(aid)
            return [{"media_id": "m1", "url": "https://example.com/1.png"}]

        monkeypatch.setattr(cloud_mod, "list_album_media", fake_list)
        album = AlbumCloud(make_config())
        await album.medias(None, GID, "a1")
        await album.medias(None, GID, "a1", refresh=True)
        assert len(calls) == 2

    async def test_invalidate_drops_only_that_album(
        self, make_config: Any, monkeypatch: Any
    ) -> None:
        calls: list[Any] = []

        async def fake_list(event: Any, gid: Any, aid: Any, *, limit: int = 0) -> Any:
            calls.append(aid)
            return [{"media_id": "m1", "url": "https://example.com/1.png"}]

        monkeypatch.setattr(cloud_mod, "list_album_media", fake_list)
        album = AlbumCloud(make_config())
        await album.medias(None, GID, "a1")
        await album.medias(None, GID, "a2")
        album.invalidate(GID, "a1")
        await album.medias(None, GID, "a1")
        await album.medias(None, GID, "a2")
        assert calls == ["a1", "a2", "a1"]

    async def test_invalidate_without_album_drops_all(
        self, make_config: Any, monkeypatch: Any
    ) -> None:
        calls: list[Any] = []

        async def fake_list(event: Any, gid: Any, aid: Any, *, limit: int = 0) -> Any:
            calls.append(aid)
            return [{"media_id": "m1", "url": "https://example.com/1.png"}]

        monkeypatch.setattr(cloud_mod, "list_album_media", fake_list)
        album = AlbumCloud(make_config())
        await album.medias(None, GID, "a1")
        await album.medias(None, GID, "a2")
        album.invalidate(GID)
        await album.medias(None, GID, "a1")
        await album.medias(None, GID, "a2")
        assert calls == ["a1", "a2", "a1", "a2"]

    async def test_empty_result_is_not_cached(
        self, make_config: Any, monkeypatch: Any
    ) -> None:
        calls: list[Any] = []

        async def fake_list(event: Any, gid: Any, aid: Any, *, limit: int = 0) -> Any:
            calls.append(aid)
            return []

        monkeypatch.setattr(cloud_mod, "list_album_media", fake_list)
        album = AlbumCloud(make_config())
        await album.medias(None, GID, "a1")
        await album.medias(None, GID, "a1")
        assert len(calls) == 2

    async def test_random_url_skips_videos_and_empty_urls(
        self, make_config: Any, monkeypatch: Any
    ) -> None:
        async def fake_list(event: Any, gid: Any, aid: Any, *, limit: int = 0) -> Any:
            return [
                {"media_id": "v", "url": "https://example.com/v.mp4", "is_video": True},
                {"media_id": "n", "url": ""},
                {"media_id": "p", "url": "https://example.com/p.png"},
            ]

        monkeypatch.setattr(cloud_mod, "list_album_media", fake_list)
        album = AlbumCloud(make_config())
        assert await album.random_url(None, GID, "a1") == "https://example.com/p.png"

    async def test_random_url_returns_empty_when_nothing_usable(
        self, make_config: Any, monkeypatch: Any
    ) -> None:
        async def fake_list(event: Any, gid: Any, aid: Any, *, limit: int = 0) -> Any:
            return [{"media_id": "v", "url": "https://x/v.mp4", "is_video": True}]

        monkeypatch.setattr(cloud_mod, "list_album_media", fake_list)
        assert await AlbumCloud(make_config()).random_url(None, GID, "a1") == ""

    async def test_delete_invalidates_cache(
        self, make_config: Any, monkeypatch: Any
    ) -> None:
        calls: list[Any] = []

        async def fake_list(event: Any, gid: Any, aid: Any, *, limit: int = 0) -> Any:
            calls.append(aid)
            return [{"media_id": "m1", "url": "https://example.com/1.png"}]

        async def fake_del(event: Any, gid: Any, aid: Any, media_id: str) -> str:
            return ""

        monkeypatch.setattr(cloud_mod, "list_album_media", fake_list)
        monkeypatch.setattr(cloud_mod, "del_album_media", fake_del)
        album = AlbumCloud(make_config())
        await album.medias(None, GID, "a1")
        assert await album.delete(None, GID, "a1", "m1") == ""
        await album.medias(None, GID, "a1")
        assert calls == ["a1", "a1"]

    async def test_failed_delete_keeps_cache(
        self, make_config: Any, monkeypatch: Any
    ) -> None:
        calls: list[Any] = []

        async def fake_list(event: Any, gid: Any, aid: Any, *, limit: int = 0) -> Any:
            calls.append(aid)
            return [{"media_id": "m1", "url": "https://example.com/1.png"}]

        async def fake_del(event: Any, gid: Any, aid: Any, media_id: str) -> str:
            return "协议端不支持该操作"

        monkeypatch.setattr(cloud_mod, "list_album_media", fake_list)
        monkeypatch.setattr(cloud_mod, "del_album_media", fake_del)
        album = AlbumCloud(make_config())
        await album.medias(None, GID, "a1")
        assert await album.delete(None, GID, "a1", "m1") != ""
        await album.medias(None, GID, "a1")
        assert calls == ["a1"]

    async def test_album_id_of_matches_exact_then_partial(
        self, make_config: Any, monkeypatch: Any
    ) -> None:
        async def fake_albums(event: Any, gid: Any) -> Any:
            return [
                {"album_id": "1", "name": "日常摸鱼"},
                {"album_id": "2", "name": "日常"},
            ]

        monkeypatch.setattr(cloud_mod, "list_albums", fake_albums)
        album = AlbumCloud(make_config())
        assert await album.album_id_of(None, GID, "日常") == "2"
        assert await album.album_id_of(None, GID, "摸鱼") == "1"
        assert await album.album_id_of(None, GID, "不存在") == ""
        assert await album.album_id_of(None, GID, "  ") == ""

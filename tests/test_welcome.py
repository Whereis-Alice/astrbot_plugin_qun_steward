"""增强欢迎与入群验证的回归测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from astrbot.api import message_components as Comp
from astrbot_plugin_qun_steward.core.audit import AuditLog
from astrbot_plugin_qun_steward.core.store import FIELD_LABELS, GroupStore
from astrbot_plugin_qun_steward.features import welcome as welcome_mod
from astrbot_plugin_qun_steward.features.base import FeatureContext
from astrbot_plugin_qun_steward.features.welcome import WelcomeFeature
from astrbot_plugin_qun_steward.web.service import StewardWebService

GID = "10001"
UID = "20002"
BOT_ID = "30003"
WELCOME_FIELDS = (
    "welcome_templates",
    "welcome_mode",
    "welcome_images",
    "welcome_delay",
    "welcome_verify",
    "welcome_verify_timeout",
    "welcome_verify_max_attempts",
    "welcome_verify_timeout_action",
)


async def _to_image(_markdown: str) -> str:
    return ""


class _Bot:
    def __init__(self) -> None:
        self.ban_calls: list[dict[str, Any]] = []
        self.kick_calls: list[dict[str, Any]] = []

    async def get_group_member_info(self, **kwargs: Any) -> dict[str, Any]:
        return {**kwargs, "card": "阿狸", "nickname": "备用昵称"}

    async def get_stranger_info(self, **kwargs: Any) -> dict[str, Any]:
        return {**kwargs, "nickname": "陌生人阿狸"}

    async def get_group_info(self, **kwargs: Any) -> dict[str, Any]:
        return {**kwargs, "group_name": "接口群名", "member_count": 88}

    async def set_group_ban(self, **kwargs: Any) -> None:
        self.ban_calls.append(kwargs)

    async def set_group_kick(self, **kwargs: Any) -> None:
        self.kick_calls.append(kwargs)


class _Event:
    """覆盖欢迎功能用到的 AstrMessageEvent 方法，不引入真实平台。"""

    def __init__(
        self,
        *,
        raw_message: dict[str, Any] | None = None,
        message_str: str = "",
        sender_id: str = UID,
        group_id: str = GID,
    ) -> None:
        self.bot = _Bot()
        self.message_str = message_str
        self.message_obj = SimpleNamespace(raw_message=raw_message or {}, message=[])
        self.sent: list[tuple[str, Any]] = []
        self._sender_id = sender_id
        self._group_id = group_id

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return "操作者"

    def get_self_id(self) -> str:
        return BOT_ID

    def get_messages(self) -> list[Any]:
        return []

    def plain_result(self, text: str) -> tuple[str, str]:
        return "plain", text

    def chain_result(self, chain: list[Any]) -> tuple[str, list[Any]]:
        return "chain", chain

    async def send(self, result: tuple[str, Any]) -> None:
        self.sent.append(result)


@pytest.fixture
async def welcome(
    store: GroupStore, database: Any, make_config: Any
) -> Any:
    ctx = FeatureContext(
        context=None,  # type: ignore[arg-type]
        config=make_config(),
        store=store,
        permissions=None,  # type: ignore[arg-type]
        audit=AuditLog(database, make_config()),
        undo=None,  # type: ignore[arg-type]
        groups=None,  # type: ignore[arg-type]
        db=database,
        to_image=_to_image,
    )
    feature = WelcomeFeature(ctx)
    try:
        yield feature
    finally:
        await feature.shutdown()


def _increase_event(**raw: Any) -> _Event:
    payload: dict[str, Any] = {
        "post_type": "notice",
        "notice_type": "group_increase",
        "group_id": int(GID),
        "user_id": int(UID),
        "group_name": "通知群名",
        "member_count": 66,
    }
    payload.update(raw)
    return _Event(raw_message=payload)


class TestBuildWelcome:
    async def test_legacy_text_still_works_with_placeholders(
        self, welcome: WelcomeFeature, store: GroupStore
    ) -> None:
        await store.set(
            GID,
            "join_welcome",
            "{at} 欢迎 {nickname} 加入 {group_name}，你是第 {member_count} 位成员 {unknown}",
        )

        chain = await welcome.build_welcome(_increase_event(), GID, UID)

        assert isinstance(chain[0], Comp.At)
        assert chain[0].qq == int(UID)
        assert isinstance(chain[1], Comp.Plain)
        assert "欢迎 阿狸 加入 通知群名，你是第 66 位成员 {unknown}" in chain[1].text

    async def test_templates_win_over_legacy_text(
        self, welcome: WelcomeFeature, store: GroupStore
    ) -> None:
        await store.set(GID, "join_welcome", "旧欢迎")
        await store.update(
            GID,
            {"welcome_templates": ["A", "B"], "welcome_mode": "顺序"},
        )

        first = await welcome.build_welcome(_increase_event(), GID, UID)
        second = await welcome.build_welcome(_increase_event(), GID, UID)
        third = await welcome.build_welcome(_increase_event(), GID, UID)

        assert [first[0].text, second[0].text, third[0].text] == ["A", "B", "A"]

    async def test_random_mode_selects_from_templates(
        self,
        welcome: WelcomeFeature,
        store: GroupStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await store.update(GID, {"welcome_templates": ["A", "B"], "welcome_mode": "随机"})
        monkeypatch.setattr(welcome_mod.random, "choice", lambda items: items[0])

        chain = await welcome.build_welcome(_increase_event(), GID, UID)

        assert chain[0].text == "A"

    async def test_no_template_returns_empty_chain(
        self, welcome: WelcomeFeature, store: GroupStore
    ) -> None:
        await store.set(GID, "join_welcome", "")

        assert await welcome.build_welcome(_increase_event(), GID, UID) == []

    @pytest.mark.parametrize("kind", ["url", "local", "file_uri"])
    async def test_image_sources(
        self,
        welcome: WelcomeFeature,
        store: GroupStore,
        tmp_path: Path,
        kind: str,
    ) -> None:
        target = tmp_path / "welcome.png"
        target.write_bytes(b"png")
        source = {
            "url": "https://example.com/welcome.png",
            "local": str(target),
            "file_uri": "file:///" + target.as_posix(),
        }[kind]
        await store.update(GID, {"join_welcome": "欢迎", "welcome_images": [source]})

        chain = await welcome.build_welcome(_increase_event(), GID, UID)
        image = chain[-1]

        assert isinstance(image, Comp.Image)
        if kind == "url":
            assert image.file == "https://example.com/welcome.png"
        else:
            assert Path(image.path).resolve() == target.resolve()


class TestWelcomeNotice:
    async def test_ignores_other_notice_or_bot_self(self, welcome: WelcomeFeature) -> None:
        assert await welcome.handle_notice(_Event(raw_message={"notice_type": "poke"})) is None
        assert (
            await welcome.handle_notice(_increase_event(user_id=int(BOT_ID), group_id=""))
            is None
        )

    async def test_normal_welcome_applies_join_ban(
        self, welcome: WelcomeFeature, store: GroupStore
    ) -> None:
        await store.update(GID, {"join_welcome": "欢迎", "join_ban_time": 60})
        event = _increase_event()

        result = await welcome.handle_notice(event)

        assert isinstance(result, list)
        assert result[0].text == "欢迎"
        assert event.bot.ban_calls == [
            {"group_id": int(GID), "user_id": int(UID), "duration": 60}
        ]

    async def test_verification_skips_join_ban(
        self,
        welcome: WelcomeFeature,
        store: GroupStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await store.update(
            GID, {"welcome_verify": True, "join_welcome": "欢迎", "join_ban_time": 60}
        )
        monkeypatch.setattr(welcome, "_make_question", lambda: ("2 + 3", 5))
        event = _increase_event()

        result = await welcome.handle_notice(event)

        assert isinstance(result, list)
        assert isinstance(result[0], Comp.At)
        assert "2 + 3" in result[1].text
        assert event.bot.ban_calls == []
        assert welcome._pending[(GID, UID)].answer == 5


class TestVerification:
    async def _start(
        self,
        welcome: WelcomeFeature,
        store: GroupStore,
        monkeypatch: pytest.MonkeyPatch,
        **overrides: Any,
    ) -> _Event:
        values = {"welcome_verify": True, "join_welcome": "欢迎"}
        values.update(overrides)
        await store.update(GID, values)
        monkeypatch.setattr(welcome, "_make_question", lambda: ("2 + 3", 5))
        event = _increase_event()
        result = await welcome.handle_notice(event)
        assert isinstance(result, list)
        return event

    async def test_correct_answer_unbans_and_welcomes(
        self,
        welcome: WelcomeFeature,
        store: GroupStore,
        database: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await self._start(welcome, store, monkeypatch)

        result = await welcome.check_reply(_Event(message_str="5"))

        assert isinstance(result, list)
        assert result[0].text == "欢迎"
        assert welcome._pending == {}
        rows = await welcome.audit.query(group_id=GID, action="welcome_verify_pass")
        assert rows and rows[0]["target_id"] == UID

    async def test_non_numeric_chat_is_not_treated_as_answer(
        self,
        welcome: WelcomeFeature,
        store: GroupStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await self._start(welcome, store, monkeypatch)

        assert await welcome.check_reply(_Event(message_str="今天是 3 号")) is None
        assert (GID, UID) in welcome._pending

    async def test_wrong_answer_keeps_pending_before_limit(
        self,
        welcome: WelcomeFeature,
        store: GroupStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await self._start(welcome, store, monkeypatch, welcome_verify_max_attempts=2)

        result = await welcome.check_reply(_Event(message_str="4"))

        assert isinstance(result, str)
        assert "还可尝试 1 次" in result
        assert welcome._pending[(GID, UID)].attempts == 1
        assert (await welcome.check_reply(_Event(message_str="5")))[0].text == "欢迎"

    async def test_attempt_limit_executes_failure_action(
        self,
        welcome: WelcomeFeature,
        store: GroupStore,
        database: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await self._start(
            welcome,
            store,
            monkeypatch,
            welcome_verify_max_attempts=1,
            welcome_verify_timeout_action="踢出",
        )

        result = await welcome.check_reply(_Event(message_str="4"))

        assert isinstance(result, str)
        assert "连续答错 1 次" in result
        assert welcome._pending == {}
        rows = await welcome.audit.query(group_id=GID, action="welcome_verify_fail")
        assert rows and rows[0]["success"] is True

    async def test_kick_and_block_failure_action(
        self,
        welcome: WelcomeFeature,
        store: GroupStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await self._start(
            welcome,
            store,
            monkeypatch,
            welcome_verify_timeout_action="踢出并拉黑",
        )
        event = _increase_event()

        message = await welcome._finish_failure(event, GID, UID, "测试失败", "welcome_verify_fail")

        assert "踢出并拉黑" in message
        assert event.bot.kick_calls == [
            {
                "group_id": int(GID),
                "user_id": int(UID),
                "reject_add_request": True,
            }
        ]
        assert UID in store.value(GID, "block_ids")

    async def test_remind_only_failure_action_does_not_kick(
        self,
        welcome: WelcomeFeature,
        store: GroupStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await self._start(
            welcome,
            store,
            monkeypatch,
            welcome_verify_timeout_action="仅提醒",
        )
        event = _increase_event()

        message = await welcome._finish_failure(event, GID, UID, "测试失败", "welcome_verify_timeout")

        assert "请管理员人工处理" in message
        assert event.bot.kick_calls == []

    async def test_timeout_finishes_pending_and_sends_notice(
        self,
        welcome: WelcomeFeature,
        store: GroupStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await self._start(welcome, store, monkeypatch)
        event = _increase_event()

        await welcome._verify_timeout(event, GID, UID, 0)

        assert welcome._pending == {}
        assert event.sent and event.sent[0][0] == "plain"
        assert "超时 0 秒未答对" in event.sent[0][1]


class TestWelcomeTasks:
    async def test_delayed_welcome_is_sent_by_background_task(
        self,
        welcome: WelcomeFeature,
        store: GroupStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await store.update(GID, {"join_welcome": "迟到 welcome", "welcome_delay": 5})

        async def fast_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(welcome_mod.asyncio, "sleep", fast_sleep)
        event = _increase_event()
        assert await welcome.handle_notice(event) is None
        tasks = list(welcome._tasks)
        assert tasks
        await asyncio.gather(*tasks)
        assert event.sent and event.sent[0][0] == "chain"
        assert event.sent[0][1][0].text == "迟到 welcome"

    async def test_shutdown_cancels_delayed_task(
        self, welcome: WelcomeFeature, store: GroupStore
    ) -> None:
        await store.update(GID, {"join_welcome": "不会发出", "welcome_delay": 30})
        event = _increase_event()

        assert await welcome.handle_notice(event) is None
        task = next(iter(welcome._tasks))
        await welcome.shutdown()

        assert task.cancelled() or task.done()
        assert event.sent == []
        assert welcome._pending == {}


class TestWelcomeConfigurationSurface:
    def test_schema_store_and_test_defaults_are_in_sync(
        self, plugin_dir: Path, group_defaults: dict[str, Any]
    ) -> None:
        schema = json.loads((plugin_dir / "_conf_schema.json").read_text(encoding="utf-8"))
        items = schema["default"]["items"]

        for field in WELCOME_FIELDS:
            assert field in FIELD_LABELS
            assert field in items
            assert items[field]["default"] == group_defaults[field]

    def test_webui_renders_welcome_fields(
        self, store: GroupStore, make_config: Any
    ) -> None:
        ctx = FeatureContext(
            context=None,  # type: ignore[arg-type]
            config=make_config(),
            store=store,
            permissions=None,  # type: ignore[arg-type]
            audit=None,  # type: ignore[arg-type]
            undo=None,  # type: ignore[arg-type]
            groups=None,  # type: ignore[arg-type]
            db=None,  # type: ignore[arg-type]
            to_image=_to_image,
        )
        service = StewardWebService(ctx, SimpleNamespace())
        fields = {item["field"]: item for item in service.fields()}

        assert set(WELCOME_FIELDS) <= set(fields)
        assert fields["welcome_templates"]["type"] == "list"
        assert fields["welcome_verify"]["type"] == "bool"
        assert fields["welcome_images"]["hint"]

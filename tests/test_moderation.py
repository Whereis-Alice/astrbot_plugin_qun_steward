"""禁我：目标固定、时长安全、审计与撤销行为。"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest
from astrbot.api.message_components import At, Reply
from astrbot_plugin_qun_steward.core.config import MAX_BAN_SECONDS, StewardConfig
from astrbot_plugin_qun_steward.core.permission import PermissionResolver, PermLevel
from astrbot_plugin_qun_steward.features.base import FeatureContext
from astrbot_plugin_qun_steward.features.moderation import ModerationFeature
from astrbot_plugin_qun_steward.main import QunStewardPlugin

GROUP_ID = "10001"
SENDER_ID = "20002"
BOT_ID = "30003"
OTHER_ID = "40004"


class _Event:
    def __init__(self, make_config: Any, store: Any, database: Any) -> None:
        self.bot = SimpleNamespace()
        self.bans: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []
        self.bot.set_group_ban = self._set_group_ban
        self.bot.get_group_member_info = self._get_member_info
        self.roles: dict[str, Any] = {
            BOT_ID: {"role": "admin"}, SENDER_ID: {"role": "member"},
            OTHER_ID: {"role": "member"},
        }
        self.response: Any = None
        self.platform = "aiocqhttp"
        self.group_id = GROUP_ID
        self.message_str = "禁我 60"
        self.messages: list[Any] = []
        self.stopped = False
        self._sender_id = SENDER_ID
        config = make_config(random_ban_time="30~300")
        # 复用生产配置的时长规整逻辑，FakeConfig 只提供属性替身。
        config.ban_time_range = StewardConfig.ban_time_range.__get__(config)
        config.resolve_ban_time = StewardConfig.resolve_ban_time.__get__(config)
        self.feature = ModerationFeature(
            FeatureContext(
                context=None,  # type: ignore[arg-type]
                config=config,
                store=store,
                permissions=PermissionResolver(config, store),
                audit=SimpleNamespace(record=self._record),  # type: ignore[arg-type]
                undo=None,  # type: ignore[arg-type]
                groups=None,  # type: ignore[arg-type]
                db=database,
                to_image=None,  # type: ignore[arg-type]
            )
        )
        self.feature.ctx.undo = SimpleNamespace(push=self._push_undo)
        self.undo_entries: list[dict[str, Any]] = []

    async def _set_group_ban(self, **kwargs: Any) -> Any:
        self.bans.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def _get_member_info(self, *, user_id: int, **kwargs: Any) -> Any:
        result = self.roles.get(str(user_id), {})
        if isinstance(result, Exception):
            raise result
        return result

    async def _record(self, **kwargs: Any) -> None:
        self.logs.append(kwargs)

    def _push_undo(self, group_id: str, **kwargs: Any) -> None:
        self.undo_entries.append({"group_id": group_id, **kwargs})

    def get_group_id(self) -> str:
        return self.group_id

    def get_self_id(self) -> str:
        return BOT_ID

    def get_platform_name(self) -> str:
        return self.platform

    def get_messages(self) -> list[Any]:
        return self.messages

    def plain_result(self, text: str) -> str:
        return text

    def stop_event(self) -> None:
        self.stopped = True

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return "测试成员"


@pytest.fixture
async def event(store: Any, database: Any, make_config: Any) -> _Event:
    return _Event(make_config, store, database)


async def test_ban_self_uses_sender_as_only_target(event: _Event) -> None:
    result = await event.feature.ban_self(event, "60")

    assert result.startswith("你已被禁言60秒")
    assert event.bans == [{"group_id": int(GROUP_ID), "user_id": int(SENDER_ID), "duration": 60}]
    assert event.logs[0]["action"] == "self_ban"
    assert event.logs[0]["target_id"] == SENDER_ID
    assert event.undo_entries[0]["description"] == f"对 {SENDER_ID} 的禁言"


async def test_ban_self_rejects_zero_without_calling_protocol(event: _Event) -> None:
    result = await event.feature.ban_self(event, 0)

    assert result == "禁我时长必须大于 0 秒。"
    assert event.bans == []
    assert event.logs == []


async def test_ban_self_rejects_negative_without_calling_protocol(event: _Event) -> None:
    result = await event.feature.ban_self(event, -1)

    assert result == "禁我时长必须大于 0 秒。"
    assert event.bans == []


@pytest.mark.parametrize("invalid", ["abc", True, 1.5])
async def test_ban_self_rejects_invalid_duration(
    event: _Event, invalid: Any
) -> None:
    result = await event.feature.ban_self(event, invalid)

    assert result == "禁我时长必须是正整数秒。"
    assert event.bans == []


async def test_ban_self_uses_configured_random_range(event: _Event, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("astrbot_plugin_qun_steward.core.config.random.randint", lambda _a, _b: 45)

    result = await event.feature.ban_self(event)

    assert result.startswith("你已被禁言45秒")
    assert event.bans[0]["user_id"] == int(SENDER_ID)


async def test_llm_self_mute_uses_default_duration_and_member_permission(
    event: _Event,
) -> None:
    class _Permissions:
        def __init__(self) -> None:
            self.checks: list[dict[str, Any]] = []

        async def check_llm(self, _event: Any, _perm_key: str, **kwargs: Any) -> None:
            kwargs["perm_key"] = _perm_key
            self.checks.append(kwargs)

    permissions = _Permissions()
    plugin = SimpleNamespace(
        cfg=SimpleNamespace(enable_llm_tools=True),
        permissions=permissions,
        moderation=event.feature,
    )

    result = [item async for item in QunStewardPlugin.llm_ban_self(plugin, event)]

    assert result == ["你已被禁言60秒（1分钟）"]
    assert event.bans == [{"group_id": int(GROUP_ID), "user_id": int(SENDER_ID), "duration": 60}]
    assert permissions.checks == [
        {"perm_key": "self_ban", "bot_perm": PermLevel.ADMIN, "need_auth": True,
         "target_id": SENDER_ID}
    ]
    assert list(inspect.signature(QunStewardPlugin.llm_ban_self).parameters) == [
        "self", "event", "duration"
    ]


def _plugin(event: _Event, *, enabled: bool = True) -> Any:
    return SimpleNamespace(
        cfg=SimpleNamespace(enable_llm_tools=enabled),
        permissions=event.feature.permissions,
        moderation=event.feature,
    )


@pytest.mark.parametrize("entry", ["command", "self_tool", "generic_tool"])
@pytest.mark.parametrize(
    ("bot_role", "sender_role", "allowed", "reason"),
    [
        ("admin", "member", True, ""),
        ("admin", "admin", False, "都是群管理员"),
        ("owner", "admin", True, ""),
        ("admin", "owner", False, "目标是群主"),
        ("member", "member", False, "没有管理员权限"),
        ("member", "admin", False, "没有管理员权限"),
    ],
)
async def test_self_mute_permission_matrix(
    event: _Event, entry: str, bot_role: str, sender_role: str, allowed: bool, reason: str
) -> None:
    event.roles[BOT_ID] = {"role": bot_role}
    event.roles[SENDER_ID] = {"role": sender_role}
    plugin = _plugin(event)
    if entry == "command":
        output = [item async for item in QunStewardPlugin.cmd_ban_self(plugin, event)]
    elif entry == "self_tool":
        output = [item async for item in QunStewardPlugin.llm_ban_self(plugin, event)]
    else:
        output = [item async for item in QunStewardPlugin.llm_set_group_ban(
            plugin, event, user_id=int(SENDER_ID), duration=114514
        )]

    assert len(output) == 1
    if allowed:
        assert "你已被禁言" in output[0]
        assert event.bans[0]["user_id"] == int(SENDER_ID)
        assert event.bans[0]["duration"] == (114514 if entry == "generic_tool" else 60)
    else:
        assert reason in output[0]
        assert event.bans == []


@pytest.mark.parametrize("need_auth", [True, False])
async def test_member_cannot_mute_someone_else(event: _Event, need_auth: bool) -> None:
    output = [item async for item in QunStewardPlugin.llm_set_group_ban(
        _plugin(event), event, user_id=int(OTHER_ID), duration=60, need_auth=need_auth
    )]
    assert output == ["你没管理员权限"]
    assert event.bans == []


@pytest.mark.parametrize(
    ("bot_role", "sender_role", "target_role", "expected"),
    [
        ("admin", "owner", "admin", "都是群管理员"),
        ("owner", "owner", "admin", "已被禁言"),
        ("owner", "admin", "admin", "你动不了管理员"),
        ("member", "owner", "member", "没有管理员权限"),
    ],
)
async def test_generic_mute_checks_explicit_tool_target(
    event: _Event, bot_role: str, sender_role: str, target_role: str, expected: str
) -> None:
    # 无需在消息里 @ 目标，必须检查工具参数里的 QQ 号。
    event.roles[BOT_ID] = {"role": bot_role}
    event.roles[SENDER_ID] = {"role": sender_role}
    event.roles[OTHER_ID] = {"role": target_role}
    output = [item async for item in QunStewardPlugin.llm_set_group_ban(
        _plugin(event), event, user_id=int(OTHER_ID), duration=60
    )]
    assert expected in output[0]
    assert bool(event.bans) == ("已被禁言" in expected)


@pytest.mark.parametrize("entry", ["self_tool", "generic_tool"])
async def test_disabled_tools_do_not_mute(event: _Event, entry: str) -> None:
    plugin = _plugin(event, enabled=False)
    if entry == "self_tool":
        output = [item async for item in QunStewardPlugin.llm_ban_self(plugin, event)]
    else:
        output = [item async for item in QunStewardPlugin.llm_set_group_ban(
            plugin, event, int(SENDER_ID), 60
        )]
    assert output == []
    assert event.bans == []


@pytest.mark.parametrize("invalid", [0, -1, True, "abc", 1.5])
async def test_self_tool_rejects_invalid_duration(event: _Event, invalid: Any) -> None:
    output = [item async for item in QunStewardPlugin.llm_ban_self(_plugin(event), event, invalid)]
    assert "时长必须" in output[0]
    assert event.bans == []


async def test_member_cannot_self_unban_through_generic_tool(event: _Event) -> None:
    output = [item async for item in QunStewardPlugin.llm_set_group_ban(
        _plugin(event), event, int(SENDER_ID), 0, need_auth=False
    )]
    assert output == ["你没管理员权限"]
    assert event.bans == []


async def test_generic_tool_zero_uses_unban_permission_and_response(event: _Event) -> None:
    event.roles[SENDER_ID] = {"role": "admin"}
    event.feature.config.perms.update({"set_group_ban": "群主", "cancel_group_ban": "管理员"})
    output = [item async for item in QunStewardPlugin.llm_set_group_ban(
        _plugin(event), event, int(OTHER_ID), 0
    )]
    assert "已解除禁言" in output[0]
    assert event.bans[0]["duration"] == 0
    assert event.logs[0]["action"] == "unban"
    assert event.undo_entries == []


@pytest.mark.parametrize("target_id", [True, 0, -1, 1.5, "abc"])
async def test_malformed_target_does_not_reach_protocol(event: _Event, target_id: Any) -> None:
    event.roles[SENDER_ID] = {"role": "owner"}
    output = [item async for item in QunStewardPlugin.llm_set_group_ban(
        _plugin(event), event, target_id, 60
    )]
    assert "QQ 号必须是正整数" in output[0]
    assert event.bans == []


@pytest.mark.parametrize("response", [
    {"status": "failed", "retcode": 100, "message": "QQ 拒绝禁言"},
    RuntimeError("QQ 拒绝禁言"),
])
async def test_protocol_failure_is_not_reported_or_audited_as_success(event: _Event, response: Any) -> None:
    event.response = response
    output = await event.feature.ban_self(event, 60)
    assert "禁言失败" in output and "QQ 拒绝禁言" in output
    assert "已被禁言" not in output
    assert event.logs[0]["success"] is False
    assert event.undo_entries == []


async def test_undo_rechecks_role_and_reports_protocol_failure(event: _Event) -> None:
    await event.feature.ban_self(event, 60)
    event.roles[SENDER_ID] = {"role": "admin"}
    with pytest.raises(ValueError, match="都是群管理员"):
        await event.undo_entries[0]["revert"]()
    assert len(event.bans) == 1


async def test_self_mute_caps_duration_at_qq_limit(event: _Event) -> None:
    await event.feature.ban_self(event, MAX_BAN_SECONDS + 1)
    assert event.bans[0]["duration"] == MAX_BAN_SECONDS


@pytest.mark.parametrize("missing_id", [BOT_ID, SENDER_ID, OTHER_ID])
@pytest.mark.parametrize("response", [
    None, {}, {"level": 99}, {"role": "unknown"},
    {"status": "failed", "retcode": 1, "data": {"role": "member"}},
    RuntimeError("查询超时"),
])
async def test_unknown_identity_fails_closed(event: _Event, missing_id: str, response: Any) -> None:
    event.roles[SENDER_ID] = {"role": "owner"}
    event.roles[missing_id] = response
    output = [item async for item in QunStewardPlugin.llm_set_group_ban(
        _plugin(event), event, int(OTHER_ID), 60
    )]
    assert "无法确认" in output[0]
    assert event.bans == []


async def test_wrapped_member_info_is_supported(event: _Event) -> None:
    event.roles[BOT_ID] = {"status": "ok", "retcode": 0, "data": {"role": "admin"}}
    event.roles[SENDER_ID] = {"status": "ok", "retcode": 0, "data": {"role": "member"}}
    output = [item async for item in QunStewardPlugin.llm_set_group_ban(
        _plugin(event), event, int(SENDER_ID), 114514
    )]
    assert "你已被禁言114514秒" in output[0]
    assert len(event.bans) == 1


@pytest.mark.parametrize(("platform", "group_id"), [("telegram", GROUP_ID), ("aiocqhttp", "")])
async def test_tools_reject_non_qq_group_calls(event: _Event, platform: str, group_id: str) -> None:
    event.platform = platform
    event.group_id = group_id
    output = [item async for item in QunStewardPlugin.llm_ban_self(_plugin(event), event)]
    assert "仅支持" in output[0]
    assert event.bans == []


@pytest.mark.parametrize("sender_role", ["member", "admin"])
async def test_superuser_does_not_override_physical_qq_roles(event: _Event, sender_role: str) -> None:
    event.feature.config.admins_id = [SENDER_ID]
    event.roles[SENDER_ID] = {"role": sender_role}
    output = [item async for item in QunStewardPlugin.llm_set_group_ban(
        _plugin(event), event, int(SENDER_ID), 60
    )]
    assert bool(event.bans) == (sender_role == "member")
    assert ("已被禁言" if sender_role == "member" else "都是群管理员") in output[0]


async def test_bot_superuser_setting_does_not_grant_qq_admin(event: _Event) -> None:
    event.feature.config.admins_id = [BOT_ID]
    event.roles[BOT_ID] = {"role": "member"}
    output = [item async for item in QunStewardPlugin.llm_ban_self(_plugin(event), event)]
    assert "没有管理员权限" in output[0]
    assert event.bans == []


@pytest.mark.parametrize("target_id", [BOT_ID, OTHER_ID])
async def test_bot_self_and_group_owner_cannot_be_muted(event: _Event, target_id: str) -> None:
    event.roles[BOT_ID] = {"role": "owner"}
    event.roles[SENDER_ID] = {"role": "owner"}
    event.roles[OTHER_ID] = {"role": "owner"}
    output = [item async for item in QunStewardPlugin.llm_set_group_ban(
        _plugin(event), event, int(target_id), 60
    )]
    assert ("机器人无法" if target_id == BOT_ID else "目标是群主") in output[0]
    assert event.bans == []


@pytest.mark.parametrize("use_reply", [True, False])
async def test_command_checks_at_or_reply_target_before_execution(event: _Event, use_reply: bool) -> None:
    event.roles[SENDER_ID] = {"role": "owner"}
    event.roles[OTHER_ID] = {"role": "admin"}
    event.messages = [Reply(id="5", sender_id=int(OTHER_ID))] if use_reply else [At(qq=OTHER_ID)]
    event.message_str = "禁言 60"
    output = [item async for item in QunStewardPlugin.cmd_ban(_plugin(event), event)]
    assert "都是群管理员" in output[0]
    assert event.bans == []


@pytest.mark.parametrize("entry", ["self_tool", "generic_tool", "command"])
async def test_self_mute_respects_per_group_permission(event: _Event, entry: str) -> None:
    await event.feature.store.set(GROUP_ID, "perms", {"self_ban": "管理员"})
    if entry == "self_tool":
        output = [item async for item in QunStewardPlugin.llm_ban_self(_plugin(event), event)]
    elif entry == "generic_tool":
        output = [item async for item in QunStewardPlugin.llm_set_group_ban(
            _plugin(event), event, int(SENDER_ID), 60
        )]
    else:
        output = [item async for item in QunStewardPlugin.cmd_ban_self(_plugin(event), event)]
    assert output == ["你没管理员权限"]
    assert event.bans == []


async def test_need_auth_false_cannot_bypass_other_tool_permissions(event: _Event) -> None:
    output = [item async for item in QunStewardPlugin.llm_set_group_whole_ban(
        _plugin(event), event, True, need_auth=False
    )]
    assert output == ["你没管理员权限"]

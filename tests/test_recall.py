"""指定消息撤回工具：群边界、消息作者权限、协议失败和结果发送通道。"""

from __future__ import annotations

import functools
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot.api.message_components import Reply
from astrbot_plugin_qun_steward.core.permission import PermissionResolver
from astrbot_plugin_qun_steward.features.base import FeatureContext
from astrbot_plugin_qun_steward.features.recall import RecallFeature
from astrbot_plugin_qun_steward.main import QunStewardPlugin

GID, BOT, CALLER, AUTHOR = "10001", "20002", "30003", "40004"
MID = -123456


class _Event:
    def __init__(self, config: Any, store: Any, database: Any) -> None:
        self.platform = "aiocqhttp"
        self.group_id = GID
        self.messages: list[Any] = []
        self.roles: dict[str, Any] = {
            BOT: {"role": "admin"}, CALLER: {"role": "member"}, AUTHOR: {"role": "member"}
        }
        self.message: Any = {
            "message_id": MID, "group_id": int(GID), "message_type": "group",
            "sender": {"user_id": int(CALLER)},
        }
        self.bot = SimpleNamespace(
            get_group_member_info=AsyncMock(side_effect=self._member_info),
            get_msg=AsyncMock(side_effect=self._get_msg), delete_msg=AsyncMock(return_value=None),
        )
        # 任何对群消息结果的写入都应使测试失败。
        self.send = AsyncMock(side_effect=AssertionError("不能发送群消息"))
        self.set_result = Mock(side_effect=AssertionError("不能设置群消息结果"))
        self.plain_result = Mock(side_effect=AssertionError("不能构造群消息结果"))
        self.get_result = Mock(side_effect=AssertionError("不能进入直接发送分支"))
        self.audit = SimpleNamespace(record=AsyncMock())
        self.permissions = PermissionResolver(config, store)
        self.feature = RecallFeature(FeatureContext(
            context=None, config=config, store=store, permissions=self.permissions,
            audit=self.audit, undo=None, groups=None, db=database, to_image=None,
        ))
        self.plugin = SimpleNamespace(cfg=config, permissions=self.permissions, recall=self.feature)

    async def _member_info(self, *, user_id: int, **kwargs: Any) -> Any:
        value = self.roles.get(str(user_id), {})
        if isinstance(value, Exception):
            raise value
        return value

    async def _get_msg(self, **kwargs: Any) -> Any:
        if isinstance(self.message, Exception):
            raise self.message
        return self.message

    def get_group_id(self) -> str:
        return self.group_id

    def get_platform_name(self) -> str:
        return self.platform

    def get_sender_id(self) -> str:
        return CALLER

    def get_sender_name(self) -> str:
        return "测试群员"

    def get_self_id(self) -> str:
        return BOT

    def get_messages(self) -> list[Any]:
        return self.messages


@pytest.fixture
async def event(store: Any, database: Any, make_config: Any) -> _Event:
    config = make_config(enable_llm_tools=True, perms={"delete_msg": "成员"})
    return _Event(config, store, database)


async def _run(event: _Event, mid: Any = str(MID)) -> str:
    output = [item async for item in QunStewardPlugin.llm_delete_msg(event.plugin, event, mid)]
    assert len(output) == 1 and isinstance(output[0], str)
    event.send.assert_not_called()
    event.set_result.assert_not_called()
    event.plain_result.assert_not_called()
    return output[0]


@pytest.mark.parametrize("mid", [MID, str(MID), 123456, "123456"])
@pytest.mark.parametrize("wrapped", [True, False])
async def test_self_message_with_signed_id(event: _Event, mid: Any, wrapped: bool) -> None:
    event.message["message_id"] = int(mid)
    if wrapped:
        event.message = {"status": "ok", "retcode": 0, "data": event.message}
    result = await _run(event, mid)
    assert result == f"已撤回消息 {mid}。"
    event.bot.get_msg.assert_awaited_once_with(message_id=int(mid))
    event.bot.delete_msg.assert_awaited_once_with(message_id=int(mid))
    log = event.audit.record.call_args.kwargs
    assert log["action"] == "recall" and log["source"] == "llm" and log["success"] is True
    assert log["target_id"] == str(mid)


async def test_omitted_id_uses_actual_reply(event: _Event) -> None:
    event.messages = [Reply(id=str(MID), sender_id=int(CALLER))]
    result = await _run(event, "")
    assert result == f"已撤回消息 {MID}。"
    event.bot.get_msg.assert_awaited_once_with(message_id=MID)


async def test_explicit_id_does_not_use_a_different_reply(event: _Event) -> None:
    event.messages = [Reply(id="9999", sender_id=int(CALLER))]
    await _run(event)
    event.bot.get_msg.assert_awaited_once_with(message_id=MID)


@pytest.mark.parametrize("mid", [None, "", True, False, 0, "0", "abc", 12.5, "12.5"])
async def test_missing_or_invalid_id_never_calls_protocol(event: _Event, mid: Any) -> None:
    result = await _run(event, mid)
    assert "消息 ID" in result
    event.bot.get_msg.assert_not_called()
    event.bot.delete_msg.assert_not_called()


@pytest.mark.parametrize(
    ("bot_role", "caller_role", "author_id", "author_role", "expected"),
    [
        ("member", "member", BOT, "member", "已撤回"),
        ("member", "member", CALLER, "member", "机器人不是群管理员"),
        ("admin", "member", CALLER, "member", "已撤回"),
        ("admin", "admin", CALLER, "admin", "都是管理员"),
        ("owner", "admin", CALLER, "admin", "已撤回"),
        ("admin", "owner", CALLER, "owner", "无法撤回群主"),
        ("admin", "member", AUTHOR, "member", "需要管理员权限"),
        ("admin", "admin", AUTHOR, "member", "已撤回"),
        ("admin", "owner", AUTHOR, "admin", "都是管理员"),
        ("owner", "admin", AUTHOR, "admin", "同级或更高"),
        ("owner", "owner", AUTHOR, "admin", "已撤回"),
        ("admin", "owner", AUTHOR, "owner", "无法撤回群主"),
    ],
)
async def test_recall_role_matrix(
    event: _Event, bot_role: str, caller_role: str, author_id: str, author_role: str, expected: str
) -> None:
    event.roles[BOT] = {"role": bot_role}
    event.roles[CALLER] = {"role": caller_role}
    event.roles[author_id] = {"role": author_role}
    event.message["sender"]["user_id"] = int(author_id)
    result = await _run(event)
    assert expected in result
    assert event.bot.delete_msg.await_count == int("已撤回" in expected)


@pytest.mark.parametrize("response", [
    {"message_id": MID, "message_type": "private", "group_id": int(GID), "sender": {"user_id": int(CALLER)}},
    {"message_id": MID, "group_id": 99999, "sender": {"user_id": int(CALLER)}},
    {"message_id": MID, "sender": {"user_id": int(CALLER)}},
    {"message_id": MID, "group_id": int(GID)},
    {"message_id": MID, "group_id": int(GID), "sender": {"user_id": True}},
    {"message_id": 999, "group_id": int(GID), "sender": {"user_id": int(CALLER)}},
    None, [],
])
async def test_scope_or_author_cannot_be_guessed(event: _Event, response: Any) -> None:
    event.message = response
    result = await _run(event)
    assert "已撤回" not in result
    event.bot.delete_msg.assert_not_called()
    assert event.audit.record.call_args.kwargs["success"] is False


@pytest.mark.parametrize("key", [BOT, CALLER, AUTHOR])
async def test_unknown_role_does_not_allow_recall(event: _Event, key: str) -> None:
    event.roles[CALLER] = {"role": "owner"}
    event.roles[key] = RuntimeError("查询失败")
    event.message["sender"]["user_id"] = int(AUTHOR)
    assert "无法确认" in await _run(event)
    event.bot.delete_msg.assert_not_called()


@pytest.mark.parametrize("stage", ["get_msg", "delete_msg"])
@pytest.mark.parametrize("failure", [RuntimeError("QQ 拒绝操作"), {"status": "failed", "retcode": 1, "message": "QQ 拒绝操作"}])
async def test_protocol_failures_are_not_success(event: _Event, stage: str, failure: Any) -> None:
    method = AsyncMock(**({"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}))
    setattr(event.bot, stage, method)
    result = await _run(event)
    assert "失败" in result and "QQ 拒绝操作" in result
    if stage == "get_msg":
        event.bot.delete_msg.assert_not_called()
    assert event.audit.record.call_args.kwargs["success"] is False
    assert event.audit.record.call_args.kwargs["source"] == "llm"


async def test_already_deleted_message_is_not_retried(event: _Event) -> None:
    event.message["status"] = "deleted"
    assert "无需重复操作" in await _run(event)
    event.bot.delete_msg.assert_not_called()


@pytest.mark.parametrize("group_override", [True, False])
async def test_recall_permission_is_configurable(event: _Event, group_override: bool) -> None:
    if group_override:
        await event.feature.store.set(GID, "perms", {"delete_msg": "管理员"})
    else:
        event.feature.config.perms["delete_msg"] = "管理员"
    assert await _run(event) == "你没管理员权限"
    event.bot.get_msg.assert_not_called()
    event.bot.delete_msg.assert_not_called()


@pytest.mark.parametrize(("platform", "group_id"), [("telegram", GID), ("aiocqhttp", "")])
async def test_tool_only_works_in_qq_groups(event: _Event, platform: str, group_id: str) -> None:
    event.platform, event.group_id = platform, group_id
    assert "仅支持" in await _run(event)
    event.bot.get_msg.assert_not_called()
    event.bot.delete_msg.assert_not_called()


async def test_disabled_tool_has_an_internal_result_without_side_effects(event: _Event) -> None:
    event.feature.config.enable_llm_tools = False
    assert "未开启" in await _run(event)
    event.bot.get_msg.assert_not_called()
    event.bot.delete_msg.assert_not_called()


@pytest.mark.parametrize("target_role", ["member", "admin"])
async def test_superuser_cannot_bypass_bot_physical_role(event: _Event, target_role: str) -> None:
    event.feature.config.admins_id = [CALLER]
    event.message["sender"]["user_id"] = int(AUTHOR)
    event.roles[AUTHOR] = {"role": target_role}
    result = await _run(event)
    assert ("已撤回" if target_role == "member" else "都是管理员") in result
    assert event.bot.delete_msg.await_count == int(target_role == "member")


async def test_command_single_recall_stays_silent_and_detects_failed_response(event: _Event) -> None:
    assert await event.feature._recall_one(event, str(MID)) == ""
    event.bot.delete_msg.return_value = {"status": "failed", "retcode": 1, "message": "超过撤回时间"}
    assert "超过撤回时间" in await event.feature._recall_one(event, str(MID))
    assert event.audit.record.call_args.kwargs["success"] is False


@pytest.mark.parametrize("outcome", ["success", "permission_denied", "delete_failed", "disabled"])
async def test_actual_astrbot_executor_never_sends_tool_result_to_group(event: _Event, outcome: str) -> None:
    from astrbot.core.agent.tool import FunctionTool
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from mcp.types import CallToolResult

    if outcome == "permission_denied":
        event.feature.config.perms["delete_msg"] = "管理员"
    elif outcome == "delete_failed":
        event.bot.delete_msg.side_effect = RuntimeError("QQ 拒绝操作")
    elif outcome == "disabled":
        event.feature.config.enable_llm_tools = False
    tool = FunctionTool(
        name="llm_delete_msg", description="撤回指定消息",
        parameters={"type": "object", "properties": {}},
        handler=functools.partial(QunStewardPlugin.llm_delete_msg, event.plugin),
    )
    ctx = SimpleNamespace(context=SimpleNamespace(event=event), tool_call_timeout=5)
    results = [item async for item in FunctionToolExecutor.execute(tool, ctx, message_id=str(MID))]
    assert len(results) == 1 and isinstance(results[0], CallToolResult)
    assert isinstance(results[0].content[0].text, str)
    event.send.assert_not_called()
    event.set_result.assert_not_called()
    event.plain_result.assert_not_called()
    event.get_result.assert_not_called()

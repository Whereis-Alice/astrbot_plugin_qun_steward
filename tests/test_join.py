"""进群审核判定逻辑。"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from astrbot.core.message.components import Plain, Reply
from astrbot_plugin_qun_steward.core.protocol import clear_backend_cache
from astrbot_plugin_qun_steward.core.store import GroupStore
from astrbot_plugin_qun_steward.features.base import FeatureContext
from astrbot_plugin_qun_steward.features.join import JoinFeature

ConfigFactory = Callable[..., Any]
GID = "10001"
UID = "20002"


async def _to_image(_markdown: str) -> str:
    return ""


@pytest.fixture
async def join(store: GroupStore, database: Any, make_config: ConfigFactory) -> JoinFeature:
    """只注入 should_approve 真正会用到的依赖，其余留空。"""
    ctx = FeatureContext(
        context=None,  # type: ignore[arg-type]
        config=make_config(),
        store=store,
        permissions=None,  # type: ignore[arg-type]
        audit=None,  # type: ignore[arg-type]
        undo=None,  # type: ignore[arg-type]
        groups=None,  # type: ignore[arg-type]
        db=database,
        to_image=_to_image,
    )
    return JoinFeature(ctx)


@pytest.fixture(autouse=True)
def _clear_protocol_cache() -> Any:
    clear_backend_cache()
    yield
    clear_backend_cache()


class TestShouldApprove:
    async def test_blacklisted_user_is_rejected(
        self, join: JoinFeature, store: GroupStore
    ) -> None:
        await store.set(GID, "block_ids", [UID])
        approved, reason = await join.should_approve(GID, UID)
        assert approved is False
        assert reason == "黑名单用户"

    async def test_low_level_is_rejected(self, join: JoinFeature, store: GroupStore) -> None:
        await store.set(GID, "join_min_level", 20)
        approved, reason = await join.should_approve(GID, UID, user_level=5)
        assert approved is False
        assert "等级过低" in reason

    async def test_unknown_level_is_not_rejected_by_level_rule(
        self, join: JoinFeature, store: GroupStore
    ) -> None:
        # 协议端拿不到等级时（user_level=None）不应该误杀
        await store.set(GID, "join_min_level", 20)
        approved, _ = await join.should_approve(GID, UID, user_level=None)
        assert approved is None

    async def test_accept_word_passes(self, join: JoinFeature, store: GroupStore) -> None:
        await store.set(GID, "join_accept_words", ["朋友推荐"])
        approved, reason = await join.should_approve(GID, UID, comment="我是朋友推荐来的")
        assert approved is True
        assert "进群白词" in reason

    async def test_accept_word_is_case_insensitive(
        self, join: JoinFeature, store: GroupStore
    ) -> None:
        await store.set(GID, "join_accept_words", ["Steward"])
        approved, _ = await join.should_approve(GID, UID, comment="来自 STEWARD 群")
        assert approved is True

    async def test_reject_word_blocks_before_accept_word(
        self, join: JoinFeature, store: GroupStore
    ) -> None:
        # 黑词优先级高于白词，避免用「广告+口令」绕过审核
        await store.set(GID, "join_accept_words", ["口令"])
        await store.set(GID, "join_reject_words", ["广告"])
        approved, reason = await join.should_approve(GID, UID, comment="口令 广告")
        assert approved is False
        assert "进群黑词" in reason

    async def test_reject_word_can_auto_block(
        self, join: JoinFeature, store: GroupStore
    ) -> None:
        await store.set(GID, "join_reject_words", ["广告"])
        await store.set(GID, "reject_word_block", True)
        approved, reason = await join.should_approve(GID, UID, comment="发广告的")
        assert approved is False
        assert "已拉黑" in reason
        assert UID in store.value(GID, "block_ids")

    async def test_answer_section_of_comment_is_used(
        self, join: JoinFeature, store: GroupStore
    ) -> None:
        # QQ 的验证问题会把问题和答案拼在一起，只应匹配答案部分
        await store.set(GID, "join_accept_words", ["天气"])
        approved, _ = await join.should_approve(
            GID, UID, comment="问题：你从哪来？\n答案：看天气预报来的"
        )
        assert approved is True

    async def test_question_text_does_not_trigger_reject_word(
        self, join: JoinFeature, store: GroupStore
    ) -> None:
        await store.set(GID, "join_reject_words", ["广告"])
        approved, _ = await join.should_approve(
            GID, UID, comment="问题：是不是来发广告的？\n答案：不是"
        )
        assert approved is None

    async def test_no_match_reject_switch(self, join: JoinFeature, store: GroupStore) -> None:
        await store.update(GID, {"join_no_match_reject": True, "join_max_time": 0})
        approved, reason = await join.should_approve(GID, UID, comment="随便说点什么")
        assert approved is False
        assert reason == "未命中进群关键词"

    async def test_defaults_to_manual_review(self, join: JoinFeature, store: GroupStore) -> None:
        await store.set(GID, "join_max_time", 0)
        approved, reason = await join.should_approve(GID, UID, comment="你好")
        assert approved is None
        assert reason == "人工审核"

    async def test_repeated_attempts_get_blocked(
        self, join: JoinFeature, store: GroupStore
    ) -> None:
        await store.set(GID, "join_max_time", 2)
        first, _ = await join.should_approve(GID, UID, comment="试一次")
        assert first is None
        second, reason = await join.should_approve(GID, UID, comment="再试一次")
        assert second is False
        assert "次数达上限" in reason
        assert UID in store.value(GID, "block_ids")

    async def test_attempt_counter_is_per_group(
        self, join: JoinFeature, store: GroupStore
    ) -> None:
        await store.update(GID, {"join_max_time": 1})
        await store.update("30003", {"join_max_time": 1})
        first, first_reason = await join.should_approve(GID, UID, comment="a")
        assert first is False
        assert "次数达上限" in first_reason
        # 另一个群的计数独立，因此这里同样是「第一次就到上限」，而不是命中黑名单
        second, second_reason = await join.should_approve("30003", UID, comment="a")
        assert second is False
        assert "次数达上限" in second_reason


class _ProtocolApi:
    def __init__(self, app_name: str, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses = {"get_version_info": {"data": {"app_name": app_name}}}
        self.responses.update(responses or {})

    async def call_action(self, action: str, **kwargs: Any) -> Any:
        self.calls.append((action, kwargs))
        response = self.responses.get(action)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(kwargs)
        if response is None:
            raise RuntimeError(f"unknown action {action}")
        return response


class _JoinEvent:
    def __init__(
        self,
        api: _ProtocolApi,
        *,
        group_id: str = GID,
        raw_message: dict[str, Any] | None = None,
        reply: Reply | None = None,
    ) -> None:
        self.bot = SimpleNamespace(api=api)
        self.message_obj = SimpleNamespace(raw_message=raw_message, message=[])
        self._group_id = group_id
        self._reply = reply
        self.approvals: list[dict[str, Any]] = []

        async def stranger_info(**_kwargs: Any) -> dict[str, Any]:
            return {"nickname": "申请人", "qqLevel": 20}

        async def set_group_add_request(**kwargs: Any) -> None:
            self.approvals.append(kwargs)

        self.bot.get_stranger_info = stranger_info
        self.bot.set_group_add_request = set_group_add_request

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_id(self) -> str:
        return "90009"

    def get_messages(self) -> list[Any]:
        return [self._reply] if self._reply else []


def _ok(data: Any = None) -> dict[str, Any]:
    return {"status": "ok", "retcode": 0, "data": data}


class TestJoinProtocolSafety:
    async def test_llbot_approval_does_not_send_sub_type(self, join: JoinFeature) -> None:
        api = _ProtocolApi("LLOneBot", {"set_group_add_request": _ok({})})
        event = _JoinEvent(api)

        await join._send_approval(event, "flag-ll", True, "", "invite")

        calls = [item for item in api.calls if item[0] == "set_group_add_request"]
        assert calls == [("set_group_add_request", {"flag": "flag-ll", "approve": True, "reason": ""})]
        assert not event.approvals

    async def test_napcat_invite_is_tried_before_add(self, join: JoinFeature) -> None:
        attempts: list[str] = []
        api = _ProtocolApi("NapCat")
        event = _JoinEvent(api)

        async def approval(**kwargs: Any) -> None:
            attempts.append(str(kwargs["sub_type"]))
            if kwargs["sub_type"] == "invite":
                raise RuntimeError("旧端需要 add")

        event.bot.set_group_add_request = approval
        await join._send_approval(event, "flag-nap", True, "", "invite")

        assert attempts == ["invite", "add"]

    async def test_empty_flag_is_not_saved_or_approved(self, join: JoinFeature, database: Any) -> None:
        api = _ProtocolApi("NapCat")
        event = _JoinEvent(
            api,
            raw_message={
                "post_type": "request",
                "request_type": "group",
                "sub_type": "add",
                "group_id": int(GID),
                "user_id": int(UID),
                "flag": "",
            },
        )

        await join.event_monitoring(event)

        assert await join.pending(GID) == []
        assert event.approvals == []
        assert [name for name, _ in api.calls] == []

    async def test_remote_failure_keeps_local_pending_request(
        self, join: JoinFeature, database: Any
    ) -> None:
        await join._save_request(
            group_id=GID,
            user_id=UID,
            flag="local-pending",
            nickname="本地申请",
            comment="hello",
            level=None,
        )
        api = _ProtocolApi(
            "NapCat",
            {
                "get_group_system_msg": {"status": "failed", "message": "暂时不可用"},
                "get_group_ignored_notifies": {
                    "status": "failed",
                    "message": "暂时不可用",
                },
                "get_group_ignore_add_request": {
                    "status": "failed",
                    "message": "暂时不可用",
                },
            },
        )
        event = _JoinEvent(api)

        added, closed, error = await join.sync_pending(event, GID)

        assert added == 0
        assert closed == 0
        assert error == "暂时不可用"
        rows = await join.pending(GID)
        assert len(rows) == 1
        assert rows[0]["flag"] == "local-pending"
        assert rows[0]["status"] == "pending"

    async def test_cross_group_reply_is_rejected_before_protocol_call(
        self, join: JoinFeature
    ) -> None:
        await join._save_request(
            group_id="other-group",
            user_id=UID,
            flag="foreign-flag",
            nickname="别群用户",
            comment="",
            level=None,
        )
        reply = Reply(
            id="notice-1",
            chain=[Plain("【进群申请】\nflag：foreign-flag")],
        )
        api = _ProtocolApi("NapCat")
        event = _JoinEvent(api, reply=reply)

        result = await join.handle_approval(event, True)

        assert result == "这条进群申请不属于当前群，拒绝处理"
        assert [name for name, _ in api.calls] == []

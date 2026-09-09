"""群公告删除动作的跨端参数边界。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from astrbot_plugin_qun_steward.core import protocol
from astrbot_plugin_qun_steward.features.notice import NoticeFeature


class _Api:
    def __init__(self, app_name: str, responses: dict[str, Any]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses = {"get_version_info": {"data": {"app_name": app_name}}, **responses}

    async def call_action(self, action: str, **kwargs: Any) -> Any:
        self.calls.append((action, kwargs))
        value = self.responses.get(action)
        if value is None:
            raise RuntimeError("unknown action")
        if isinstance(value, list):
            return value.pop(0)
        if callable(value):
            return value(kwargs)
        return value


def _event(app_name: str, responses: dict[str, Any]) -> Any:
    api = _Api(app_name, responses)
    return SimpleNamespace(bot=SimpleNamespace(api=api), api=api)


@pytest.fixture(autouse=True)
def _clear_protocol_cache() -> Any:
    protocol.clear_backend_cache()
    yield
    protocol.clear_backend_cache()


def _ok(data: Any = None) -> dict[str, Any]:
    return {"status": "ok", "retcode": 0, "data": data or {}}


class TestDeleteNotice:
    async def test_napcat_sends_only_notice_id(self) -> None:
        event = _event("NapCat", {"_del_group_notice": _ok()})
        feature = object.__new__(NoticeFeature)
        result = await feature._delete_notice(event, 123, "notice-1", "fid-1")
        assert result.ok
        assert event.api.calls[-1] == (
            "_del_group_notice",
            {"group_id": 123, "notice_id": "notice-1"},
        )

    async def test_snowluma_prefers_fid_then_can_try_notice_id(self) -> None:
        event = _event(
            "SnowLuma",
            {
                "_del_group_notice": [
                    {"status": "failed", "message": "unsupported parameter: fid"},
                    _ok(),
                ]
            },
        )
        feature = object.__new__(NoticeFeature)
        result = await feature._delete_notice(event, 123, "notice-1", "fid-1")
        assert result.ok
        assert event.api.calls[-2:] == [
            ("_del_group_notice", {"group_id": 123, "fid": "fid-1"}),
            ("_del_group_notice", {"group_id": 123, "notice_id": "notice-1"}),
        ]

    async def test_llbot_uses_its_delete_action_and_notice_id(self) -> None:
        event = _event("LLOneBot", {"_delete_group_notice": _ok()})
        feature = object.__new__(NoticeFeature)
        result = await feature._delete_notice(event, 123, "notice-1", "fid-1")
        assert result.ok
        assert event.api.calls[-1] == (
            "_delete_group_notice",
            {"group_id": 123, "notice_id": "notice-1"},
        )

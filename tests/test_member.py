"""群成员清单与清理候选的安全边界。"""

from __future__ import annotations

import time

from astrbot_plugin_qun_steward.features.base import FeatureContext
from astrbot_plugin_qun_steward.features.member import MemberFeature, _normalize_members


def _feature(make_config):
    ctx = FeatureContext(
        context=None,  # type: ignore[arg-type]
        config=make_config(),
        store=None,  # type: ignore[arg-type]
        permissions=None,  # type: ignore[arg-type]
        audit=None,  # type: ignore[arg-type]
        undo=None,  # type: ignore[arg-type]
        groups=None,  # type: ignore[arg-type]
        db=None,  # type: ignore[arg-type]
        to_image=None,  # type: ignore[arg-type]
    )
    return MemberFeature(ctx)


def test_normalize_members_accepts_common_wrappers() -> None:
    members = _normalize_members({"data": {"members": [{"userId": "1"}]}})
    assert members == [{"userId": "1"}]


def test_collect_candidates_uses_user_id_alias_and_keeps_only_complete_members(
    make_config,
) -> None:
    feature = _feature(make_config)
    old = int(time.time()) - 40 * 86400
    members = [
        {
            "userId": "10001",
            "role": "member",
            "level": 3,
            "lastSentTime": old,
        },
        # 缺少等级 / 最后发言时间，不应被当成低等级不活跃成员。
        {"userId": "10002", "role": "member", "level": 3},
        # 缺少 QQ 号，不能进入破坏性操作候选。
        {"role": "member", "level": 3, "last_sent_time": old},
        # 管理员永远不应被清理。
        {"userId": "10003", "role": "admin", "level": 1, "lastSentTime": old},
        # 未知身份也不纳入候选，避免协议端字段不完整时误踢。
        {"userId": "10004", "level": 1, "lastSentTime": old},
    ]
    candidates = feature._collect_candidates(members, inactive_days=30, under_level=10)
    assert [item["userId"] for item in candidates] == ["10001"]

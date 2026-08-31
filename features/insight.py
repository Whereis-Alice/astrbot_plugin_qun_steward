"""群情报卡片：群信息 / 群荣誉榜 / 禁言列表。

全是只读查询，统一走 core.protocol.call_action 做跨端降级：某个扩展接口在当前
协议端上没有，就少显示对应字段，不会让整条指令失败。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api.event import AstrMessageEvent

from ..core.protocol import as_dict, as_list, call_action
from ..core.utils import format_datetime, format_duration
from .base import Feature

#: 基础群信息
_INFO_ACTIONS: tuple[str, ...] = ("get_group_info",)
#: 扩展群信息（群等级、建群时间、群备注等），两个动作名任选其一
_INFO_EX_ACTIONS: tuple[str, ...] = ("get_group_info_ex", "get_group_detail_info")
#: 当前的加群策略与成员权限设置
_ADMIN_SETTING_ACTIONS: tuple[str, ...] = ("get_group_admin_settings",)
#: @全体成员剩余次数
_AT_ALL_ACTIONS: tuple[str, ...] = ("get_group_at_all_remain",)
#: 群荣誉
_HONOR_ACTIONS: tuple[str, ...] = ("get_group_honor_info",)
#: 当前禁言中的成员
_SHUT_LIST_ACTIONS: tuple[str, ...] = ("get_group_shut_list",)

#: 加群方式（QQ 侧的 add_type 取值）
_ADD_TYPE_LABELS: dict[int, str] = {
    1: "允许任何人加入",
    2: "需要管理员审核",
    3: "不允许任何人加入",
    4: "答对问题并由管理员审核",
    5: "答对问题即自动通过",
}

#: 成员邀请策略
_INVITE_POLICY_LABELS: dict[str, str] = {
    "disabled": "不允许成员邀请",
    "require_approval": "邀请需管理员审核",
    "no_approval": "成员可直接邀请",
    "no_approval_under_100": "群人数少于 100 时可直接邀请",
}

#: 群荣誉的分组名 -> 展示标题，按展示顺序排列
_HONOR_GROUPS: tuple[tuple[str, str], ...] = (
    ("talkative_list", "历史龙王"),
    ("performer_list", "群聊之火"),
    ("legend_list", "群聊炽焰"),
    ("strong_newbie_list", "冒尖小春笋"),
    ("emotion_list", "快乐源泉（氛围担当）"),
)

#: 榜单每类最多展示几人、禁言列表最多展示多少条
_HONOR_LIMIT = 5
_SHUT_LIMIT = 80


def _yes_no(value: Any) -> str:
    if value is None:
        return "未知"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "开" if value else "关"
    return "开" if bool(value) else "关"


def _member_line(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    nickname = str(item.get("nickname") or item.get("nick") or "").strip()
    user_id = str(item.get("user_id") or item.get("uin") or "").strip()
    desc = str(item.get("description") or item.get("desc") or "").strip()
    who = nickname or user_id or "未知成员"
    parts = [who]
    if user_id and nickname:
        parts.append(f"({user_id})")
    line = "".join(parts)
    return f"{line} —— {desc}" if desc else line


class InsightFeature(Feature):
    """只读的群情报查询。"""

    # ------------------------------------------------------------- 群信息卡片

    async def group_info(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        if not group_id:
            return "请在群里使用该指令"
        gid = int(group_id)

        base = as_dict(
            (await call_action(event, _INFO_ACTIONS, group_id=gid, no_cache=True)).data
        )
        extra = as_dict(
            (await call_action(event, _INFO_EX_ACTIONS, group_id=gid, no_cache=True)).data
        )
        merged: dict[str, Any] = {**extra, **{k: v for k, v in base.items() if v not in (None, "")}}
        if not merged:
            return "获取群信息失败：协议端没有返回数据"

        name = str(merged.get("group_name") or "未知群名")
        lines = [f"【{name}】{gid}"]

        member_count = merged.get("member_count")
        max_count = merged.get("max_member_count")
        if member_count is not None:
            total = f"/{max_count}" if max_count else ""
            lines.append(f"成员：{member_count}{total}")
        if merged.get("group_level") is not None:
            lines.append(f"群等级：{merged.get('group_level')}")
        if merged.get("group_create_time"):
            lines.append(f"建群时间：{format_datetime(merged.get('group_create_time'))}")
        if str(merged.get("group_remark") or "").strip():
            lines.append(f"群备注：{merged.get('group_remark')}")
        if merged.get("group_all_shut") not in (None, 0, False):
            lines.append("当前状态：全员禁言中")

        remain = as_dict((await call_action(event, _AT_ALL_ACTIONS, group_id=gid)).data)
        if remain:
            group_left = remain.get("remain_at_all_count_for_group")
            self_left = remain.get("remain_at_all_count_for_uin")
            lines.append(
                f"@全体成员：本群今日剩 {group_left if group_left is not None else '未知'} 次，"
                f"本账号剩 {self_left if self_left is not None else '未知'} 次"
            )

        settings = as_dict((await call_action(event, _ADMIN_SETTING_ACTIONS, group_id=gid)).data)
        if settings:
            lines.append("")
            lines.append("—— 当前管理策略 ——")
            add_type = settings.get("add_type")
            if add_type is not None:
                label = _ADD_TYPE_LABELS.get(int(add_type), f"未知({add_type})")
                lines.append(f"加群方式：{label}")
            question = str(settings.get("group_question") or "").strip()
            if question:
                lines.append(f"入群问题：{question}")
            policy = str(settings.get("member_invite_policy") or "").strip()
            if policy:
                lines.append(f"成员邀请：{_INVITE_POLICY_LABELS.get(policy, policy)}")
            lines.append(f"成员传相册：{_yes_no(settings.get('allow_member_upload_album'))}")
            lines.append(f"成员发起临时会话：{_yes_no(settings.get('allow_member_temporary_session'))}")
            lines.append(f"成员建群：{_yes_no(settings.get('allow_member_create_group'))}")
            lines.append(f"新成员看历史消息：{_yes_no(settings.get('new_member_history_visible'))}")

        return "\n".join(lines)

    # --------------------------------------------------------------- 群荣誉榜

    async def honor(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        if not group_id:
            return "请在群里使用该指令"

        result = await call_action(
            event, _HONOR_ACTIONS, group_id=int(group_id), type="all"
        )
        if not result.ok:
            return f"获取群荣誉失败：{result.error}"
        data = as_dict(result.data)
        if not data:
            return "协议端没有返回群荣誉数据"

        lines = ["【群荣誉榜】"]
        current = data.get("current_talkative")
        if isinstance(current, dict) and current:
            day_count = current.get("day_count")
            suffix = f"（连续 {day_count} 天）" if day_count else ""
            lines.append(f"当前龙王：{_member_line(current)}{suffix}")

        for key, title in _HONOR_GROUPS:
            members = [line for line in (_member_line(item) for item in as_list(data.get(key))) if line]
            if not members:
                continue
            lines.append("")
            lines.append(f"—— {title} ——")
            lines.extend(f"{index}. {line}" for index, line in enumerate(members[:_HONOR_LIMIT], 1))

        if len(lines) == 1:
            return "本群暂时还没有荣誉数据"
        return "\n".join(lines)

    # --------------------------------------------------------------- 禁言列表

    async def shut_list(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        if not group_id:
            return "请在群里使用该指令"

        result = await call_action(event, _SHUT_LIST_ACTIONS, group_id=int(group_id))
        if not result.ok:
            return f"获取禁言列表失败：{result.error}"

        now = int(time.time())
        rows: list[tuple[int, str]] = []
        for item in as_list(result.data):
            if not isinstance(item, dict):
                continue
            until = int(item.get("shut_up_time") or item.get("shutUpTime") or 0)
            remain = until - now
            if remain <= 0:
                continue
            nickname = str(item.get("nickname") or item.get("nick") or "").strip()
            user_id = str(item.get("user_id") or item.get("uin") or "").strip()
            who = f"{nickname}({user_id})" if nickname else user_id or "未知成员"
            rows.append((remain, who))

        if not rows:
            return "本群当前没有被禁言的成员"

        rows.sort(key=lambda row: row[0])
        lines = [f"【禁言列表】共 {len(rows)} 人"]
        for index, (remain, who) in enumerate(rows[:_SHUT_LIMIT], 1):
            lines.append(f"{index}. {who} 剩余 {format_duration(remain)}")
        if len(rows) > _SHUT_LIMIT:
            lines.append(f"…… 还有 {len(rows) - _SHUT_LIMIT} 人未显示")
        return "\n".join(lines)

"""群情报卡片：群信息 / 群荣誉榜 / 禁言列表。

全是只读查询，统一走 core.protocol.call_action 做跨端降级：某个扩展接口在当前
协议端上没有，就少显示对应字段，不会让整条指令失败。

返回值是 core.card 里的结构化卡片，由 main.py 决定画成图还是降级成文字。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api.event import AstrMessageEvent

from ..core.card import Card, Heading, KeyValue, Note, Rank, RankRow, Stat, Stats, Tags
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

#: 管理策略里的开关项：协议端字段 -> 展示名
_POLICY_SWITCHES: tuple[tuple[str, str], ...] = (
    ("group_search", "允许被搜索"),
    ("allow_member_upload_album", "成员传相册"),
    ("allow_member_temporary_session", "成员临时会话"),
    ("allow_member_create_group", "成员建群"),
    ("new_member_history_visible", "新成员看历史"),
)

#: 榜单每类最多展示几人、禁言列表最多展示多少条
_HONOR_LIMIT = 5
_SHUT_LIMIT = 80
#: 协议端会用一个极大的时间戳表示长期/永久禁言，超过这个秒数就不再显示具体天数
_SHUT_LONG_TERM = 31 * 86400


def _switch_tag(label: str, value: Any) -> tuple[str, str]:
    """把一个开关画成标签：开=绿、关=灰、读不到=灰。"""
    if value is None:
        return f"{label} 未知", "muted"
    enabled = bool(value)
    return f"{label} " + ("开" if enabled else "关"), "ok" if enabled else "muted"


def _member_row(item: Any, weight: float = 0.0, section: str = "") -> RankRow | None:
    """榜单里的一行：昵称(QQ) + 荣誉描述。

    协议端经常把荣誉描述填成小节标题本身（「群聊之火」下每个人的 description
    都是「群聊之火」），这种重复信息不再往行尾塞。
    """
    if not isinstance(item, dict):
        return None
    nickname = str(item.get("nickname") or item.get("nick") or "").strip()
    user_id = str(item.get("user_id") or item.get("uin") or "").strip()
    if not nickname and not user_id:
        return None
    name = f"{nickname}({user_id})" if nickname and user_id else nickname or user_id
    desc = str(item.get("description") or item.get("desc") or "").strip()
    if desc and section and (desc in section or section in desc):
        desc = ""
    return RankRow(name=name, note=desc, weight=weight)


def _overview(merged: dict[str, Any], remain: dict[str, Any]) -> Stats:
    """顶部统计格：成员数、群等级、@全体成员剩余次数。"""
    items: list[Stat] = []
    member_count = merged.get("member_count")
    if member_count is not None:
        max_count = merged.get("max_member_count")
        items.append(
            Stat(
                label="成员",
                value=str(member_count),
                note=f"上限 {max_count}" if max_count else "",
            )
        )
    level = merged.get("group_level")
    if level is not None:
        items.append(Stat(label="群等级", value=str(level)))
    if remain:
        group_left = remain.get("remain_at_all_count_for_group")
        self_left = remain.get("remain_at_all_count_for_uin")
        items.append(
            Stat(
                label="@全体成员",
                value="未知" if group_left is None else f"{group_left} 次",
                note="" if self_left is None else f"本账号剩 {self_left} 次",
                tone="err" if group_left == 0 else "brand",
            )
        )
    return Stats(items=items)


def _profile_rows(merged: dict[str, Any]) -> list[tuple[str, str]]:
    """基础资料字段，读不到的直接不显示。"""
    rows: list[tuple[str, str]] = []
    if merged.get("group_create_time"):
        rows.append(("建群时间", format_datetime(merged.get("group_create_time"))))
    remark = str(merged.get("group_remark") or "").strip()
    if remark:
        rows.append(("群备注", remark))
    memo = str(merged.get("group_memo") or merged.get("group_announcement") or "").strip()
    if memo:
        rows.append(("群介绍", memo))
    return rows


def _policy_rows(settings: dict[str, Any], merged: dict[str, Any]) -> list[tuple[str, str]]:
    """管理策略里的文字项。

    入群问题在不同协议端上有时挂在管理设置里，有时挂在扩展群信息里，两处都找。
    """
    rows: list[tuple[str, str]] = []
    add_type = settings.get("add_type")
    if add_type is not None:
        try:
            label = _ADD_TYPE_LABELS.get(int(add_type), f"未知({add_type})")
        except (TypeError, ValueError):
            label = str(add_type)
        rows.append(("加群方式", label))
    question = str(settings.get("group_question") or merged.get("group_question") or "").strip()
    if question:
        rows.append(("入群问题", question))
    policy = str(settings.get("member_invite_policy") or "").strip()
    if policy:
        rows.append(("成员邀请", _INVITE_POLICY_LABELS.get(policy, policy)))
    return rows


class InsightFeature(Feature):
    """只读的群情报查询。"""

    # ------------------------------------------------------------- 群信息卡片

    async def group_info(self, event: AstrMessageEvent) -> str | Card:
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

        remain = as_dict((await call_action(event, _AT_ALL_ACTIONS, group_id=gid)).data)
        settings = as_dict((await call_action(event, _ADMIN_SETTING_ACTIONS, group_id=gid)).data)

        card = Card(
            title=str(merged.get("group_name") or "未知群名"),
            subtitle=f"群号 {gid}",
            badge="群信息",
            footer="数据取自协议端实时查询",
        )
        card.add(_overview(merged, remain))
        if merged.get("group_all_shut") not in (None, 0, False):
            card.add(Note(text="本群正处于全员禁言状态", tone="err"))

        profile = _profile_rows(merged)
        if profile:
            card.add(Heading(text="基础资料"), KeyValue(rows=profile))

        if settings:
            card.add(Heading(text="当前管理策略"))
            rows = _policy_rows(settings, merged)
            if rows:
                card.add(KeyValue(rows=rows))
            card.add(
                Tags(items=[_switch_tag(label, settings.get(key)) for key, label in _POLICY_SWITCHES])
            )
        else:
            card.add(Note(text="当前协议端读不到管理策略，已跳过这部分", tone="muted"))
        return card

    # --------------------------------------------------------------- 群荣誉榜

    async def honor(self, event: AstrMessageEvent) -> str | Card:
        group_id = event.get_group_id()
        if not group_id:
            return "请在群里使用该指令"

        result = await call_action(event, _HONOR_ACTIONS, group_id=int(group_id), type="all")
        if not result.ok:
            return f"获取群荣誉失败：{result.error}"
        data = as_dict(result.data)
        if not data:
            return "协议端没有返回群荣誉数据"

        card = Card(title="群荣誉榜", subtitle=f"群号 {group_id}", badge="荣誉")
        current = data.get("current_talkative")
        if isinstance(current, dict) and current:
            row = _member_row(current)
            if row is not None:
                day_count = current.get("day_count")
                card.add(
                    Stats(
                        items=[
                            Stat(
                                label="当前龙王",
                                value=row.name,
                                note=f"连续 {day_count} 天" if day_count else "",
                            )
                        ],
                        columns=1,
                    )
                )

        filled = False
        for key, title in _HONOR_GROUPS:
            candidates = (_member_row(item, section=title) for item in as_list(data.get(key)))
            rows = [row for row in candidates if row]
            if not rows:
                continue
            filled = True
            shown = rows[:_HONOR_LIMIT]
            note = f"共 {len(rows)} 人" if len(rows) > len(shown) else ""
            card.add(Heading(text=title, note=note), Rank(rows=shown))

        if not filled and not card.blocks:
            return "本群暂时还没有荣誉数据"
        return card

    # --------------------------------------------------------------- 禁言列表

    async def shut_list(self, event: AstrMessageEvent) -> str | Card:
        group_id = event.get_group_id()
        if not group_id:
            return "请在群里使用该指令"

        result = await call_action(event, _SHUT_LIST_ACTIONS, group_id=int(group_id))
        if not result.ok:
            return f"获取禁言列表失败：{result.error}"

        now = int(time.time())
        found: list[tuple[int, str]] = []
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
            found.append((remain, who))

        if not found:
            return "本群当前没有被禁言的成员"

        found.sort(key=lambda row: row[0])
        # 比例条按封顶后的剩余时间算，否则一个长期禁言会把其他人的条全压成一条线
        longest = max(min(remain, _SHUT_LONG_TERM) for remain, _ in found) or 1
        card = Card(
            title="禁言列表",
            subtitle=f"共 {len(found)} 人，按剩余时间从短到长",
            badge="禁言",
        )
        card.add(
            Rank(
                rows=[
                    RankRow(
                        name=who,
                        value="长期" if remain >= _SHUT_LONG_TERM else format_duration(remain, 2),
                        weight=min(remain, _SHUT_LONG_TERM) / longest,
                    )
                    for remain, who in found[:_SHUT_LIMIT]
                ],
                medals=False,
            )
        )
        if len(found) > _SHUT_LIMIT:
            card.add(Note(text=f"还有 {len(found) - _SHUT_LIMIT} 人未显示", tone="muted"))
        return card

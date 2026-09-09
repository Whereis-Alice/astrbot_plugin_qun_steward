"""群情报卡片与群管理策略：群信息 / 群荣誉榜 / 禁言列表。

查询与写入都统一走 core.protocol.call_action 做跨端降级：某个扩展接口在当前
协议端上没有，会给出明确提示，不会伪装成成功。

返回值是 core.card 里的结构化卡片，由 main.py 决定画成图还是降级成文字。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api.event import AstrMessageEvent

from ..core.card import Card, Heading, KeyValue, Note, Rank, RankRow, Stat, Stats, Tags
from ..core.protocol import as_dict, as_list, call_action, explain_action_error
from ..core.utils import format_datetime, format_duration, parse_bool, parse_int
from .base import Feature

#: 基础群信息
_INFO_ACTIONS: tuple[str, ...] = ("get_group_info",)
#: 扩展群信息（群等级、建群时间、群备注等），两个动作名任选其一
_INFO_EX_ACTIONS: tuple[str, ...] = ("get_group_info_ex", "get_group_detail_info")
#: 当前的加群策略与成员权限设置
_ADMIN_SETTING_ACTIONS: tuple[str, ...] = ("get_group_admin_settings",)
#: 写入加群方式与入群问题
_SET_ADD_OPTION_ACTIONS: tuple[str, ...] = ("set_group_add_option",)
#: 写入群搜索开关
_SET_SEARCH_ACTIONS: tuple[str, ...] = ("set_group_search",)
#: 写入成员邀请策略
_SET_INVITE_POLICY_ACTIONS: tuple[str, ...] = ("set_group_member_invite_policy",)
#: 写入新成员历史可见性
_SET_HISTORY_ACTIONS: tuple[str, ...] = ("set_group_new_member_history_visibility",)
#: @全体成员剩余次数
_AT_ALL_ACTIONS: tuple[str, ...] = ("get_group_at_all_remain",)
#: 群荣誉
_HONOR_ACTIONS: tuple[str, ...] = ("get_group_honor_info",)
#: 当前禁言中的成员
_SHUT_LIST_ACTIONS: tuple[str, ...] = ("get_group_shut_list",)

#: 加群方式（QQ 侧的 add_type 取值）
_ADD_TYPE_LABELS: dict[int, str] = {
    0: "未设置",
    1: "允许任何人加入",
    2: "需要管理员审核",
    3: "不允许任何人加入",
    4: "答对问题即自动通过",
    5: "答对问题后由管理员审核",
}

#: 用户输入的加群方式别名 -> QQ add_type。
_ADD_TYPE_INPUTS: dict[str, int] = {
    "自由加入": 1,
    "允许加入": 1,
    "任何人": 1,
    "开放加入": 1,
    "管理员审核": 2,
    "需要审核": 2,
    "审核加入": 2,
    "禁止加入": 3,
    "不允许加入": 3,
    "关闭加群": 3,
    "答题自动通过": 4,
    "答题通过": 4,
    "问题自动通过": 4,
    "答题需审核": 5,
    "答题审核": 5,
    "问题管理员审核": 5,
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

_CLEAR_QUESTION_WORDS = frozenset({"清空", "清除", "无", "关闭", "取消"})


def _switch_tag(label: str, value: Any) -> tuple[str, str]:
    """把一个开关画成标签：开=绿、关=灰、读不到=灰。"""
    if value is None:
        return f"{label} 未知", "muted"
    enabled = parse_bool(value, default=None)
    if enabled is None:
        return f"{label} 未知", "muted"
    return f"{label} " + ("开" if enabled else "关"), "ok" if enabled else "muted"


def _setting(settings: dict[str, Any], *keys: str) -> Any:
    """读取设置字段，兼容协议端的 camelCase 命名。"""
    for key in keys:
        value = settings.get(key)
        if value not in (None, ""):
            return value
    return None


def _camel_key(key: str) -> str:
    """把内部 snake_case 字段转成常见的 camelCase 别名。"""
    head, *tail = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def _search_enabled(settings: dict[str, Any]) -> bool | None:
    """把 group_search 或两个 no_* 反向字段统一成「允许搜索」。"""
    direct = _setting(settings, "group_search", "groupSearch")
    if direct is not None:
        return parse_bool(direct, default=None)

    finger = _setting(settings, "no_finger_open", "noFingerOpen")
    code = _setting(settings, "no_code_finger_open", "noCodeFingerOpen")
    if finger is None and code is None:
        return None
    # NapCat / SnowLuma 的 no_* 字段是「0 开、1 关」。少数桥接层会把它们
    # 序列化成 bool：此时 True 仍表示“关闭”，不能再取反一次。
    def closed(value: Any) -> bool | None:
        numeric = parse_int(value, None)
        if numeric is not None:
            return bool(numeric) if numeric in (0, 1) else None
        return parse_bool(value, default=None)

    values = [closed(value) for value in (finger, code)]
    known = [value for value in values if value is not None]
    if any(known):
        return False
    # 只返回了其中一个 no_* 字段时，只要它明确为 0，仍然可以判断为
    # 「允许搜索」；不能因为另一个字段缺失就把已知信息降级成未知。
    return True if known else None


def _whole_ban_enabled(value: Any) -> bool | None:
    """把三端不同的全员禁言状态值统一成布尔值。

    SnowLuma 使用 ``-1`` 表示开启、``0`` 表示关闭；NapCat 及部分桥接层则
    返回 bool 或 0/1。不能直接对 ``-1`` 调用 ``parse_bool``，否则会把正在
    全员禁言误显示成未知。
    """
    if isinstance(value, bool):
        return value
    numeric = parse_int(value, None)
    if numeric == -1:
        return True
    if numeric == 0:
        return False
    if numeric == 1:
        return True
    return parse_bool(value, default=None)


def _add_type_label(value: Any) -> str:
    """把 QQ add_type 转成人类可读的中文。"""
    parsed = parse_int(value, None)
    if parsed is not None:
        return _ADD_TYPE_LABELS.get(parsed, f"未知({parsed})")
    text = str(value or "").strip()
    return text or "未知"


def _parse_add_type(raw: Any) -> int | None:
    """解析数字或中文加群方式。"""
    text = "" if raw is None else str(raw).strip()
    if not text:
        return None
    parsed = parse_int(text, None)
    if parsed is not None and 0 <= parsed <= 5:
        return parsed
    compact = "".join(text.split())
    return _ADD_TYPE_INPUTS.get(compact)


def _parse_invite_policy(raw: Any) -> str | None:
    """解析成员邀请策略的中文别名。"""
    text = "".join(str(raw or "").strip().split())
    aliases = {
        "关闭": "disabled",
        "禁止邀请": "disabled",
        "不允许邀请": "disabled",
        "需审核": "require_approval",
        "审核": "require_approval",
        "邀请审核": "require_approval",
        "直接邀请": "no_approval",
        "允许直接邀请": "no_approval",
        "百人以下直接邀请": "no_approval_under_100",
        "100人以下直接邀请": "no_approval_under_100",
        "百人以下": "no_approval_under_100",
    }
    return aliases.get(text) or (
        text if text in _INVITE_POLICY_LABELS else None
    )


def _member_row(item: Any, weight: float = 0.0, section: str = "") -> RankRow | None:
    """榜单里的一行：昵称(QQ) + 荣誉描述。

    协议端经常把荣誉描述填成小节标题本身（「群聊之火」下每个人的 description
    都是「群聊之火」），这种重复信息不再往行尾塞。
    """
    if not isinstance(item, dict):
        return None
    nickname = str(
        item.get("nickname")
        or item.get("nick")
        or item.get("nick_name")
        or item.get("nickName")
        or ""
    ).strip()
    user_id = str(
        item.get("user_id") or item.get("userId") or item.get("uin") or ""
    ).strip()
    if not nickname and not user_id:
        return None
    name = f"{nickname}({user_id})" if nickname and user_id else nickname or user_id
    desc = str(
        item.get("description")
        or item.get("description_text")
        or item.get("desc")
        or ""
    ).strip()
    if desc and section and (desc in section or section in desc):
        desc = ""
    return RankRow(name=name, note=desc, weight=weight)


def _overview(merged: dict[str, Any], remain: dict[str, Any]) -> Stats:
    """顶部统计格：成员数、群等级、@全体成员剩余次数。"""
    items: list[Stat] = []
    member_count = parse_int(
        _setting(merged, "member_count", "memberCount", "member_num", "memberNum"),
        None,
    )
    if member_count is not None:
        max_count = parse_int(
            _setting(
                merged,
                "max_member_count",
                "maxMemberCount",
                "max_member_num",
                "maxMemberNum",
            ),
            None,
        )
        items.append(
            Stat(
                label="成员",
                value=str(member_count),
                note=f"上限 {max_count}" if max_count else "",
            )
        )
    level = _setting(merged, "group_level", "groupLevel", "level")
    if level is not None:
        items.append(Stat(label="群等级", value=str(level)))
    if remain:
        group_left = parse_int(
            _setting(
                remain,
                "remain_at_all_count_for_group",
                "remainAtAllCountForGroup",
            ),
            None,
        )
        self_left = parse_int(
            _setting(
                remain,
                "remain_at_all_count_for_uin",
                "remainAtAllCountForUin",
            ),
            None,
        )
        can_at_all = parse_bool(
            _setting(remain, "can_at_all", "canAtAll"),
            default=None,
        )
        if group_left is not None or self_left is not None or can_at_all is not None:
            items.append(
                Stat(
                    label="@全体成员",
                    value="未知" if group_left is None else f"{group_left} 次",
                    note="" if self_left is None else f"本账号剩 {self_left} 次",
                    tone="err" if group_left is not None and group_left <= 0 else "brand",
                )
            )
    return Stats(items=items)


def _profile_rows(merged: dict[str, Any]) -> list[tuple[str, str]]:
    """基础资料字段，读不到的直接不显示。"""
    rows: list[tuple[str, str]] = []
    create_time = _setting(merged, "group_create_time", "groupCreateTime", "create_time", "createTime")
    if create_time:
        rows.append(("建群时间", format_datetime(create_time)))
    remark = str(_setting(merged, "group_remark", "groupRemark") or "").strip()
    if remark:
        rows.append(("群备注", remark))
    memo = str(
        _setting(
            merged,
            "group_memo",
            "groupMemo",
            "group_announcement",
            "groupAnnouncement",
        )
        or ""
    ).strip()
    if memo:
        rows.append(("群介绍", memo))
    return rows


def _policy_rows(settings: dict[str, Any], merged: dict[str, Any]) -> list[tuple[str, str]]:
    """管理策略里的文字项。

    入群问题在不同协议端上有时挂在管理设置里，有时挂在扩展群信息里，两处都找。
    """
    rows: list[tuple[str, str]] = []
    add_type = _setting(settings, "add_type", "addType")
    if add_type is not None:
        rows.append(("加群方式", _add_type_label(add_type)))
    question = str(
        _setting(settings, "group_question", "groupQuestion")
        or _setting(merged, "group_question", "groupQuestion")
        or ""
    ).strip()
    if question:
        rows.append(("入群问题", question))
    policy = str(
        _setting(settings, "member_invite_policy", "memberInvitePolicy") or ""
    ).strip()
    if policy:
        rows.append(("成员邀请", _INVITE_POLICY_LABELS.get(policy, policy)))
    return rows


def _nested_dict(payload: Any, *keys: str) -> dict[str, Any]:
    """取协议响应里的业务字典，兼容一层或多层 data/info 包装。"""
    current = as_dict(payload)
    for _ in range(4):
        nested = next(
            (current.get(key) for key in keys if isinstance(current.get(key), dict)),
            None,
        )
        if nested is None:
            break
        current = nested
    return current


class InsightFeature(Feature):
    """群情报查询与群管理策略写入。"""

    # ------------------------------------------------------------- 群信息卡片

    async def group_info(self, event: AstrMessageEvent) -> str | Card:
        group_id = event.get_group_id()
        if not group_id:
            return "请在群里使用该指令"
        gid = int(group_id)

        base_result = await call_action(event, _INFO_ACTIONS, group_id=gid, no_cache=True)
        extra_result = await call_action(
            event, _INFO_EX_ACTIONS, group_id=gid, no_cache=True
        )
        base = _nested_dict(base_result.data, "group_info", "groupInfo", "info", "data")
        extra = _nested_dict(
            extra_result.data, "group_info_ex", "groupInfoEx", "group_info", "groupInfo", "info", "data"
        )
        merged: dict[str, Any] = {**extra, **{k: v for k, v in base.items() if v not in (None, "")}}
        remain_result = await call_action(event, _AT_ALL_ACTIONS, group_id=gid)
        settings_result = await call_action(event, _ADMIN_SETTING_ACTIONS, group_id=gid)
        remain = _nested_dict(
            remain_result.data,
            "remain",
            "at_all_remain",
            "atAllRemain",
            "data",
        )
        settings = _nested_dict(
            settings_result.data,
            "settings",
            "admin_settings",
            "adminSettings",
            "data",
        )

        failures: list[str] = []
        if not base_result.ok:
            failures.append(
                "基础群资料：" + explain_action_error(base_result, "基础群资料")
            )
        elif not base:
            failures.append("基础群资料：协议端未返回数据")
        if not extra_result.ok:
            failures.append(
                "扩展群资料：" + explain_action_error(extra_result, "扩展群资料")
            )
        elif not extra:
            failures.append("扩展群资料：协议端未返回数据")
        if not merged and failures:
            return "获取群信息失败：" + "；".join(failures)

        card = Card(
            title=str(_setting(merged, "group_name", "groupName", "name") or "未知群名"),
            subtitle=f"群号 {gid}",
            badge="群信息",
            footer="数据取自协议端实时查询",
        )
        card.add(_overview(merged, remain))
        if _whole_ban_enabled(
            _setting(merged, "group_all_shut", "groupAllShut", "all_shut", "allShut"),
        ) is True:
            card.add(Note(text="本群正处于全员禁言状态", tone="err"))

        if failures:
            card.add(Note(text="；".join(failures), tone="muted"))
        if not remain_result.ok:
            card.add(
                Note(
                    text="@全体成员剩余次数读取失败："
                    + explain_action_error(remain_result, "@全体成员次数"),
                    tone="muted",
                )
            )
        elif not remain:
            card.add(Note(text="协议端未返回 @全体成员剩余次数", tone="muted"))

        profile = _profile_rows(merged)
        if profile:
            card.add(Heading(text="基础资料"), KeyValue(rows=profile))

        if settings_result.ok and settings:
            card.add(Heading(text="当前管理策略"))
            rows = _policy_rows(settings, merged)
            if rows:
                card.add(KeyValue(rows=rows))
            combined_settings = {**merged, **settings}
            card.add(
                Tags(
                    items=[
                        _switch_tag(
                            label,
                            _search_enabled(combined_settings)
                            if key == "group_search"
                            else _setting(combined_settings, key, _camel_key(key)),
                        )
                        for key, label in _POLICY_SWITCHES
                    ]
                )
            )
        elif not settings_result.ok:
            card.add(
                Note(
                    text="当前协议端读取管理策略失败："
                    + explain_action_error(settings_result, "群管理策略"),
                    tone="muted",
                )
            )
        else:
            card.add(Note(text="协议端未返回管理策略，已跳过这部分", tone="muted"))
        return card

    # ----------------------------------------------------------- 策略写入

    async def _read_settings(
        self, event: AstrMessageEvent
    ) -> tuple[dict[str, Any], str]:
        """读取当前群管理策略；写入入群问题前必须先经过这里。"""
        group_id = event.get_group_id()
        if not group_id:
            return {}, "请在群里使用该指令"
        result = await call_action(
            event, _ADMIN_SETTING_ACTIONS, group_id=int(group_id)
        )
        if not result.ok:
            return {}, explain_action_error(result, "读取群管理策略")
        settings = as_dict(result.data)
        for key in ("settings", "admin_settings", "adminSettings"):
            nested = settings.get(key)
            if isinstance(nested, dict):
                settings = nested
                break
        if not settings:
            return {}, "协议端没有返回当前群管理策略"
        return settings, ""

    async def _write_setting(
        self,
        event: AstrMessageEvent,
        params: dict[str, Any],
        detail: str,
        success_text: str,
    ) -> str:
        """统一执行策略写入、审计与用户提示。"""
        result = await call_action(event, _SET_ADD_OPTION_ACTIONS, **params)
        if not result.ok:
            await self.log(event, "group_policy", detail=result.error, success=False)
            return f"{detail}失败：{explain_action_error(result, '群管理策略')}"
        await self.log(event, "group_policy", detail=detail)
        return success_text

    async def _write_action(
        self,
        event: AstrMessageEvent,
        actions: tuple[str, ...],
        params: dict[str, Any],
        detail: str,
        success_text: str,
    ) -> str:
        """执行不是 set_group_add_option 的策略动作。"""
        result = await call_action(event, actions, **params)
        if not result.ok:
            await self.log(event, "group_policy", detail=result.error, success=False)
            return f"{detail}失败：{explain_action_error(result, '群管理策略')}"
        await self.log(event, "group_policy", detail=detail)
        return success_text

    async def set_add_option(self, event: AstrMessageEvent, raw: Any = "") -> str:
        """设置或查询加群方式。"""
        group_id = event.get_group_id()
        if not group_id:
            return "请在群里使用该指令"
        text = str(raw or "").strip()
        if not text:
            settings, error = await self._read_settings(event)
            if error:
                return f"读取群管理策略失败：{error}"
            return f"当前加群方式：{_add_type_label(_setting(settings, 'add_type', 'addType'))}"

        add_type = _parse_add_type(text)
        if add_type is None:
            return (
                "无法识别加群方式。可用：自由加入、管理员审核、禁止加入、"
                "答题自动通过、答题需审核（也可写 0～5）"
            )
        return await self._write_setting(
            event,
            {"group_id": int(group_id), "add_type": add_type},
            "设置加群方式",
            f"已设置加群方式：{_ADD_TYPE_LABELS[add_type]}",
        )

    async def set_join_question(self, event: AstrMessageEvent, raw: Any = "") -> str:
        """设置入群问题；问题与答案用「|」分隔，清空时同时清除答案。"""
        group_id = event.get_group_id()
        if not group_id:
            return "请在群里使用该指令"
        text = str(raw or "").strip()
        if not text:
            return "用法：设置入群问题 <问题> [|答案]；发送「设置入群问题 清空」可移除"

        settings, error = await self._read_settings(event)
        if error:
            return f"读取当前加群设置失败，未修改任何内容：{error}"
        add_type = _parse_add_type(_setting(settings, "add_type", "addType"))
        if add_type is None:
            return "协议端没有返回当前加群方式，无法安全修改入群问题"

        compact = "".join(text.split())
        if compact in _CLEAR_QUESTION_WORDS:
            question, answer = "", ""
        else:
            if "|" in text or "｜" in text:
                separator = "|" if "|" in text else "｜"
                question, answer = (part.strip() for part in text.split(separator, 1))
            else:
                question = text
                answer = str(
                    _setting(settings, "group_answer", "groupAnswer") or ""
                ).strip()
            if not question:
                return "入群问题不能为空；如需移除请发送「设置入群问题 清空」"

        return await self._write_setting(
            event,
            {
                "group_id": int(group_id),
                "add_type": add_type,
                "group_question": question,
                "group_answer": answer,
            },
            "设置入群问题",
            "已更新入群问题" + ("与答案" if answer else ""),
        )

    async def set_search(self, event: AstrMessageEvent, raw: Any = "") -> str:
        """设置或查询群搜索开关。"""
        group_id = event.get_group_id()
        if not group_id:
            return "请在群里使用该指令"
        value = parse_bool(raw, default=None)
        if value is None:
            if str(raw or "").strip():
                return "用法：群搜索 开 / 关"
            settings, error = await self._read_settings(event)
            if error:
                return f"读取群管理策略失败：{error}"
            enabled = _search_enabled(settings)
            return "当前允许被搜索：" + ("开" if enabled else "关" if enabled is False else "未知")

        disabled = int(not value)
        return await self._write_action(
            event,
            _SET_SEARCH_ACTIONS,
            {
                "group_id": int(group_id),
                "no_finger_open": disabled,
                "no_code_finger_open": disabled,
            },
            "设置群搜索",
            f"已设置群搜索：{'开' if value else '关'}",
        )

    async def set_invite_policy(self, event: AstrMessageEvent, raw: Any = "") -> str:
        """设置或查询成员邀请策略。"""
        group_id = event.get_group_id()
        if not group_id:
            return "请在群里使用该指令"
        text = str(raw or "").strip()
        policy = _parse_invite_policy(text)
        if policy is None and text:
            return "无法识别邀请策略。可用：关闭、需审核、直接邀请、百人以下直接邀请"
        if policy is None:
            settings, error = await self._read_settings(event)
            if error:
                return f"读取群管理策略失败：{error}"
            current = str(
                _setting(settings, "member_invite_policy", "memberInvitePolicy") or ""
            )
            return f"当前成员邀请：{_INVITE_POLICY_LABELS.get(current, current or '未知')}"
        return await self._write_action(
            event,
            _SET_INVITE_POLICY_ACTIONS,
            {"group_id": int(group_id), "policy": policy},
            "设置成员邀请",
            f"已设置成员邀请：{_INVITE_POLICY_LABELS[policy]}",
        )

    async def set_history_visibility(self, event: AstrMessageEvent, raw: Any = "") -> str:
        """设置或查询新成员是否可查看历史消息。"""
        group_id = event.get_group_id()
        if not group_id:
            return "请在群里使用该指令"
        value = parse_bool(raw, default=None)
        if value is None:
            if str(raw or "").strip():
                return "用法：新成员历史 开 / 关"
            settings, error = await self._read_settings(event)
            if error:
                return f"读取群管理策略失败：{error}"
            current = parse_bool(
                _setting(settings, "new_member_history_visible", "newMemberHistoryVisible"),
                default=None,
            )
            return "当前新成员查看历史：" + (
                "开" if current else "关" if current is False else "未知"
            )
        return await self._write_action(
            event,
            _SET_HISTORY_ACTIONS,
            {"group_id": int(group_id), "visible": value},
            "设置新成员历史",
            f"已设置新成员查看历史：{'开' if value else '关'}",
        )

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
        current = data.get("current_talkative") or data.get("currentTalkative")
        if isinstance(current, dict) and current:
            row = _member_row(current)
            if row is not None:
                day_count = parse_int(
                    current.get("day_count") or current.get("dayCount"), None
                )
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
            candidates = (
                _member_row(item, section=title)
                for item in as_list(data.get(key) or data.get(_camel_key(key)))
            )
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
            until = parse_int(
                item.get("shut_up_time")
                or item.get("shutUpTime")
                or item.get("shut_time")
                or item.get("shutTime"),
                0,
            ) or 0
            remain = until - now
            if remain <= 0:
                continue
            nickname = str(
                item.get("nickname")
                or item.get("nick")
                or item.get("nick_name")
                or item.get("nickName")
                or ""
            ).strip()
            user_id = str(
                item.get("user_id") or item.get("userId") or item.get("uin") or ""
            ).strip()
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

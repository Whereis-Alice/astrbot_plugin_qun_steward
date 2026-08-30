"""配置管理、帮助、审计查询与撤销。

这些指令都不直接操作群，只读写插件自己的数据，所以放在一起。
"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent

from ..core.audit import action_label
from ..core.config import DISPLAY_NAME
from ..core.store import FIELD_LABELS
from ..core.utils import format_datetime, parse_int
from .base import Feature, rest_of

#: 操作日志单次最多展示条数
MAX_LOG_ROWS = 30

#: 帮助文档：(分组标题, [(指令, 说明)])
HELP_SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "禁言与成员",
        (
            ("禁言 <秒数> @某人", "禁言指定成员，秒数留空时按随机区间取值"),
            ("解禁 @某人", "解除禁言"),
            ("全禁 开/关", "开启或关闭全员禁言"),
            ("改名 <昵称> @某人", "修改群名片"),
            ("改头衔 <头衔> @某人", "设置专属头衔（需群主权限）"),
            ("申请头衔 <头衔>", "给自己申请头衔"),
            ("踢了 @某人", "把成员移出群聊"),
            ("群拉黑 @某人", "移出群聊并加入群黑名单"),
            ("上管 / 下管 @某人", "设置或取消管理员"),
            ("群友信息", "导出群成员列表（进群时间、等级、昵称）"),
            ("清理群友 <天数> <等级>", "清理长期不活跃且等级偏低的成员，需二次确认"),
        ),
    ),
    (
        "消息治理",
        (
            ("撤回", "引用一条消息撤回；或 @某人 + 数量批量撤回其最近消息"),
            ("设精 / 移精", "引用消息设为或移出精华"),
            ("群精华", "查看精华消息列表"),
            ("设置禁词 +词 -词", "增删自定义违禁词，不带正负号时视为整表覆盖"),
            ("内置禁词 开/关", "启用或关闭内置敏感词库"),
            ("禁词禁言 <秒数>", "命中违禁词后的禁言时长"),
            ("刷屏禁言 <秒数>", "刷屏判定后的禁言时长"),
            ("投票禁言 <秒数> @某人", "发起投票禁言，其他人用「赞同禁言 / 反对禁言」表态"),
        ),
    ),
    (
        "群务设置",
        (
            ("设置群名 <名称>", "修改群名称"),
            ("设置群头像", "引用一张图片作为群头像"),
            ("发布群公告 <内容>", "发布群公告，可同时带图"),
            ("群公告", "查看已发布的群公告"),
            ("开启宵禁 HH:MM HH:MM", "按时间段自动全员禁言"),
            ("关闭宵禁", "取消宵禁计划"),
        ),
    ),
    (
        "进群与退群",
        (
            ("进群审核 开/关", "是否接管进群申请"),
            ("进群白词 / 进群黑词", "命中白词自动通过，命中黑词自动驳回"),
            ("未命中驳回 开/关", "答案没命中白词时是否直接驳回"),
            ("进群等级 <数字>", "低于该 QQ 等级直接驳回"),
            ("进群次数 <数字>", "同一人反复申请超过次数后拉黑"),
            ("进群黑名单 +QQ -QQ", "增删进群黑名单"),
            ("待审进群", "查看待处理的进群申请及其序号"),
            ("批准 <序号> / 驳回 <序号> <理由>", "处理进群申请，也可直接引用申请通知"),
            ("进群禁言 <秒数>", "新成员入群后自动禁言"),
            ("进群欢迎 <欢迎语>", "设置欢迎词，支持 {nickname} 占位"),
            ("退群通知 开/关", "有人主动退群时在群里提示"),
            ("退群拉黑 开/关", "主动退群者加入黑名单"),
        ),
    ),
    (
        "文件与相册",
        (
            ("查看群文件 [路径]", "浏览群文件目录"),
            ("上传群文件 <路径>", "引用文件后上传到指定目录"),
            ("删除群文件 <路径>", "删除群文件或目录内文件"),
            ("上传群相册 [相册名] [数量]", "把图片或聊天记录长图上传到群相册"),
        ),
    ),
    (
        "插件自身",
        (
            ("群管配置", "不带参数导出本群配置；带「字段: 值」多行文本则写入"),
            ("群管重置 [群号|all]", "让群回到跟随默认配置的状态"),
            ("操作日志 [关键词] [条数]", "查看本群的管理操作记录"),
            ("撤销", "撤销最近一次可回滚的危险操作"),
            ("群务帮助", "显示这份说明"),
        ),
    ),
)


class ConfigFeature(Feature):
    """插件配置、帮助、审计与撤销。"""

    # ---------------------------------------------------------------- 配置

    async def show_or_apply(self, event: AstrMessageEvent) -> str:
        """「群管配置」：无参数导出，有参数导入。"""
        raw = rest_of(event)
        group_id, payload = self._split_group_id(raw, event.get_group_id())
        if not group_id:
            return "请在群里使用，或在指令后写上群号。"

        if not payload:
            return self._export_text(group_id)

        updated, unknown = await self.store.import_lines(group_id, payload)
        if not updated and not unknown:
            return "没有解析到任何配置项，格式形如「进群审核: 开」，多项用换行分隔。"
        await self.log(
            event,
            "set_config",
            target_id=group_id,
            detail="、".join(updated) or "无",
            success=bool(updated),
        )
        lines: list[str] = []
        if updated:
            lines.append(f"已更新 {len(updated)} 项：" + "、".join(updated))
        if unknown:
            preview = "、".join(unknown[:5])
            lines.append(f"有 {len(unknown)} 行没认出来：{preview}")
            lines.append("可用字段：" + "、".join(FIELD_LABELS.values()))
        return "\n".join(lines)

    def _export_text(self, group_id: str) -> str:
        """导出配置文本，并标明哪些字段是本群单独设置的。"""
        body = self.store.export_lines(group_id)
        overrides = self.store.overrides(group_id)
        header = f"群 {group_id} 的配置"
        if overrides:
            names = "、".join(FIELD_LABELS.get(field, field) for field in overrides)
            header += f"（本群单独设置：{names}）"
        else:
            header += "（全部跟随默认配置）"
        tip = "复制下面的文本，改完之后发「群管配置」+ 内容即可写回。"
        return f"{header}\n{tip}\n\n{body}"

    async def reset(self, event: AstrMessageEvent) -> str:
        """「群管重置」：清除覆写，回到跟随默认值。"""
        target = rest_of(event).strip()
        if target in ("all", "全部"):
            if not event.is_admin():
                return "只有超级管理员才能重置全部群配置。"
            await self.store.follow_default(None)
            await self.log(event, "reset_config", target_id="all", detail="全部群")
            return "已清除所有群的单独配置，现在全部跟随默认配置。"

        group_id = target or event.get_group_id()
        if not group_id:
            return "请在群里使用，或在指令后写上群号。"
        if not self.store.overrides(group_id):
            return f"群 {group_id} 本来就跟随默认配置，无需重置。"
        await self.store.follow_default(group_id)
        await self.log(event, "reset_config", target_id=group_id)
        return f"群 {group_id} 的单独配置已清除，现在跟随默认配置。"

    @staticmethod
    def _split_group_id(raw: str, fallback: str) -> tuple[str, str]:
        """从参数里剥离可选的前导群号。"""
        text = (raw or "").strip()
        if not text:
            return str(fallback or ""), ""
        head, _, tail = text.partition(" ")
        head = head.strip()
        if head.isdigit() and len(head) >= 5:
            return head, tail.strip()
        return str(fallback or ""), text

    # ---------------------------------------------------------------- 帮助

    def help_markdown(self) -> str:
        """生成帮助文档（Markdown）。"""
        lines = [f"# {DISPLAY_NAME}", "", "所有指令都需要 @机器人 或使用唤醒前缀。", ""]
        for title, items in HELP_SECTIONS:
            lines.append(f"## {title}")
            lines.append("")
            lines.append("| 指令 | 说明 |")
            lines.append("| --- | --- |")
            for command, description in items:
                lines.append(f"| {command} | {description} |")
            lines.append("")
        lines.append("> 权限阈值、默认配置与更多开关可在 AstrBot 面板的插件配置或管理页里调整。")
        return "\n".join(lines)

    # ---------------------------------------------------------------- 审计

    async def show_audit(self, event: AstrMessageEvent) -> str:
        """「操作日志」：查看本群的管理操作记录。"""
        if not self.audit.enabled:
            return "审计日志当前是关闭状态，可在插件配置里打开 audit.enable。"

        keyword, limit = self._parse_audit_args(rest_of(event))
        rows = await self.audit.query(
            group_id=event.get_group_id(), keyword=keyword, limit=limit
        )
        if not rows:
            return "没有查到符合条件的操作记录。"

        lines = ["# 操作日志", ""]
        if keyword:
            lines.append(f"关键词：{keyword}")
            lines.append("")
        lines.append("| 时间 | 操作 | 执行者 | 对象 | 详情 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for row in rows:
            mark = "" if row.get("success") else "（失败）"
            operator = row.get("operator_name") or row.get("operator_id") or "-"
            lines.append(
                "| {time} | {action}{mark} | {operator} | {target} | {detail} |".format(
                    time=format_datetime(row.get("created_at")),
                    action=action_label(str(row.get("action") or "")),
                    mark=mark,
                    operator=operator,
                    target=row.get("target_id") or "-",
                    detail=(str(row.get("detail") or "-")).replace("|", "/")[:60],
                )
            )
        return "\n".join(lines)

    @staticmethod
    def _parse_audit_args(raw: str) -> tuple[str | None, int]:
        tokens = (raw or "").split()
        limit = 15
        keyword: str | None = None
        if tokens and tokens[-1].isdigit():
            limit = max(1, min(parse_int(tokens[-1], limit) or limit, MAX_LOG_ROWS))
            tokens = tokens[:-1]
        if tokens:
            keyword = " ".join(tokens)
        return keyword, limit

    # ---------------------------------------------------------------- 撤销

    async def undo_last(self, event: AstrMessageEvent) -> str:
        """「撤销」：回滚最近一次可撤销的操作。"""
        group_id = event.get_group_id()
        entry = self.undo.peek(group_id)
        if entry is None:
            return "没有可撤销的操作。（只有禁言、改名片、全员禁言等可回滚操作会进入撤销栈）"

        ok, message = await self.undo.undo(group_id)
        await self.log(
            event,
            "undo",
            target_id=entry.action,
            detail=message,
            success=ok,
        )
        return message

    async def show_pending_undo(self, event: AstrMessageEvent) -> str:
        """辅助信息：列出撤销栈里还有什么。"""
        entries = self.undo.pending(event.get_group_id())
        if not entries:
            return "撤销栈是空的。"
        lines = ["可撤销的操作（从新到旧）："]
        for index, entry in enumerate(entries, start=1):
            lines.append(f"{index}. {entry.description}（{format_datetime(entry.created_at)}）")
        return "\n".join(lines)

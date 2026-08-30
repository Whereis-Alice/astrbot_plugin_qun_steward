"""基础群管操作：禁言、改名、头衔、踢人、拉黑、管理员。"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..core.config import LOG_TAG
from ..core.utils import extract_image_url, format_duration, get_nickname
from .base import Feature, resolve_targets


class ModerationFeature(Feature):
    """群成员管理。所有操作都会写审计日志，可回滚的会进撤销栈。"""

    # ------------------------------------------------------------ 内部工具 --- #
    async def _throttle(self, index: int) -> None:
        """批量操作时按配置节流，降低协议端风控概率。"""
        if index == 0:
            return
        interval = self.config.safety.float("batch_interval", 0.4)
        if interval > 0:
            await asyncio.sleep(interval)

    def _targets(self, event: AstrMessageEvent, target_id: Any = "") -> list[str]:
        if target_id:
            return [str(target_id)]
        return resolve_targets(event)

    # ---------------------------------------------------------------- 禁言 --- #
    async def ban(
        self,
        event: AstrMessageEvent,
        ban_time: Any = None,
        target_id: Any = "",
    ) -> str:
        """禁言。未指定时长时按随机区间取值。"""
        group_id = event.get_group_id()
        seconds = self.config.resolve_ban_time(
            self.store.value(group_id, "random_ban_time"), ban_time
        )
        targets = self._targets(event, target_id)
        if not targets:
            return "未指定要禁言的用户"

        results: list[str] = []
        for index, tid in enumerate(targets):
            await self._throttle(index)
            try:
                await event.bot.set_group_ban(
                    group_id=int(group_id), user_id=int(tid), duration=seconds
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{LOG_TAG} 禁言失败 group={group_id} user={tid}: {exc}")
                results.append(f"用户[{tid}]禁言失败：{exc}")
                await self.log(event, "ban", target_id=tid, detail=str(exc), success=False)
                continue
            results.append(f"用户[{tid}]已被禁言{seconds}秒（{format_duration(seconds)}）")
            await self.log(event, "ban", target_id=tid, detail=f"{seconds}秒")
            self._push_unban_undo(event, tid)
        return "\n".join(results)

    async def unban(self, event: AstrMessageEvent, target_id: Any = "") -> str:
        """解除禁言。"""
        group_id = event.get_group_id()
        targets = self._targets(event, target_id)
        if not targets:
            return "未指定要解禁的用户"

        results: list[str] = []
        for index, tid in enumerate(targets):
            await self._throttle(index)
            try:
                await event.bot.set_group_ban(
                    group_id=int(group_id), user_id=int(tid), duration=0
                )
            except Exception as exc:  # noqa: BLE001
                results.append(f"用户[{tid}]解禁失败：{exc}")
                await self.log(event, "unban", target_id=tid, detail=str(exc), success=False)
                continue
            results.append(f"用户[{tid}]已解除禁言")
            await self.log(event, "unban", target_id=tid)
        return "\n".join(results)

    def _push_unban_undo(self, event: AstrMessageEvent, target_id: str) -> None:
        group_id = int(event.get_group_id())
        bot = event.bot

        async def revert() -> None:
            await bot.set_group_ban(group_id=group_id, user_id=int(target_id), duration=0)

        self.undo.push(
            event.get_group_id(),
            operator_id=event.get_sender_id(),
            action="ban",
            description=f"对 {target_id} 的禁言",
            revert=revert,
        )

    async def whole_ban(self, event: AstrMessageEvent, enable: bool) -> str:
        """全员禁言开关。"""
        group_id = event.get_group_id()
        try:
            await event.bot.set_group_whole_ban(group_id=int(group_id), enable=enable)
        except Exception as exc:  # noqa: BLE001
            await self.log(event, "whole_ban", detail=str(exc), success=False)
            return f"操作失败：{exc}"
        await self.log(event, "whole_ban", detail="开启" if enable else "关闭")
        if enable:
            bot = event.bot

            async def revert() -> None:
                await bot.set_group_whole_ban(group_id=int(group_id), enable=False)

            self.undo.push(
                group_id,
                operator_id=event.get_sender_id(),
                action="whole_ban",
                description="全员禁言",
                revert=revert,
            )
        return "已开启全体禁言" if enable else "已关闭全体禁言"

    # ------------------------------------------------------------ 名片头衔 --- #
    async def set_card(
        self,
        event: AstrMessageEvent,
        target_id: Any = "",
        target_card: Any = "",
    ) -> str:
        """修改群名片。未指定目标时改自己。"""
        group_id = event.get_group_id()
        targets = self._targets(event, target_id) or [str(event.get_sender_id())]
        card = str(target_card or "")

        results: list[str] = []
        for index, tid in enumerate(targets):
            await self._throttle(index)
            old_card = await get_nickname(event, tid)
            try:
                await event.bot.set_group_card(
                    group_id=int(group_id), user_id=int(tid), card=card
                )
            except Exception as exc:  # noqa: BLE001
                results.append(f"修改 {tid} 的群昵称失败：{exc}")
                await self.log(event, "set_card", target_id=tid, detail=str(exc), success=False)
                continue
            results.append(
                f"已修改{old_card}的群昵称为【{card}】" if card else f"已清除{old_card}的群昵称"
            )
            await self.log(event, "set_card", target_id=tid, detail=card or "（清空）")
            self._push_card_undo(event, tid, old_card)
        return "\n".join(results)

    def _push_card_undo(self, event: AstrMessageEvent, target_id: str, old_card: str) -> None:
        group_id = event.get_group_id()
        bot = event.bot

        async def revert() -> None:
            await bot.set_group_card(
                group_id=int(group_id), user_id=int(target_id), card=old_card
            )

        self.undo.push(
            group_id,
            operator_id=event.get_sender_id(),
            action="set_card",
            description=f"{target_id} 的群名片修改",
            revert=revert,
        )

    async def set_title(
        self,
        event: AstrMessageEvent,
        target_id: Any = "",
        special_title: Any = "",
    ) -> str:
        """设置专属头衔（需要群主权限）。"""
        group_id = event.get_group_id()
        targets = self._targets(event, target_id) or [str(event.get_sender_id())]
        title = str(special_title or "")

        results: list[str] = []
        for index, tid in enumerate(targets):
            await self._throttle(index)
            name = await get_nickname(event, tid)
            try:
                await event.bot.set_group_special_title(
                    group_id=int(group_id),
                    user_id=int(tid),
                    special_title=title,
                    duration=-1,
                )
            except Exception as exc:  # noqa: BLE001
                results.append(f"设置 {name} 的头衔失败：{exc}")
                await self.log(event, "set_title", target_id=tid, detail=str(exc), success=False)
                continue
            results.append(f"已修改{name}的头衔为【{title}】" if title else f"已清除{name}的头衔")
            await self.log(event, "set_title", target_id=tid, detail=title or "（清空）")
        return "\n".join(results)

    # ------------------------------------------------------------ 踢人拉黑 --- #
    async def kick(self, event: AstrMessageEvent, target_id: Any = "") -> str:
        """移出群聊。不可撤销。"""
        return await self._kick(event, target_id, reject=False)

    async def block(self, event: AstrMessageEvent, target_id: Any = "") -> str:
        """移出群聊并加入群黑名单。不可撤销：OneBot 没有统一的移出黑名单接口。"""
        return await self._kick(event, target_id, reject=True)

    async def _kick(self, event: AstrMessageEvent, target_id: Any, reject: bool) -> str:
        group_id = event.get_group_id()
        targets = self._targets(event, target_id)
        if not targets:
            return "未指定要拉黑的用户" if reject else "未指定要踢出的用户"

        max_batch = self.config.safety.int("max_batch_kick", 50)
        truncated = False
        if max_batch > 0 and len(targets) > max_batch:
            targets = targets[:max_batch]
            truncated = True

        action = "block" if reject else "kick"
        results: list[str] = []
        for index, tid in enumerate(targets):
            await self._throttle(index)
            name = await get_nickname(event, tid)
            try:
                await event.bot.set_group_kick(
                    group_id=int(group_id), user_id=int(tid), reject_add_request=reject
                )
            except Exception as exc:  # noqa: BLE001
                results.append(f"操作【{tid}-{name}】失败：{exc}")
                await self.log(event, action, target_id=tid, detail=str(exc), success=False)
                continue
            results.append(
                f"已将【{tid}-{name}】踢出本群并拉黑!"
                if reject
                else f"已将【{tid}-{name}】踢出本群"
            )
            await self.log(event, action, target_id=tid, detail=name)
        if truncated:
            results.append(f"（一次最多操作 {max_batch} 人，其余已忽略）")
        return "\n".join(results)

    # -------------------------------------------------------------- 管理员 --- #
    async def set_admin(self, event: AstrMessageEvent, enable: bool) -> str:
        """设置 / 取消管理员。"""
        group_id = event.get_group_id()
        targets = resolve_targets(event)
        if not targets:
            return "未指定要操作的用户"

        results: list[str] = []
        action = "set_admin" if enable else "unset_admin"
        for index, tid in enumerate(targets):
            await self._throttle(index)
            name = await get_nickname(event, tid)
            try:
                await event.bot.set_group_admin(
                    group_id=int(group_id), user_id=int(tid), enable=enable
                )
            except Exception as exc:  # noqa: BLE001
                results.append(f"操作 {name} 失败：{exc}")
                await self.log(event, action, target_id=tid, detail=str(exc), success=False)
                continue
            results.append(
                f"{name}已被设为管理员" if enable else f"{name}的管理员身份已被取消"
            )
            await self.log(event, action, target_id=tid, detail=name)
            self._push_admin_undo(event, tid, name, enable)
        return "\n".join(results)

    def _push_admin_undo(
        self, event: AstrMessageEvent, target_id: str, name: str, enable: bool
    ) -> None:
        group_id = event.get_group_id()
        bot = event.bot

        async def revert() -> None:
            await bot.set_group_admin(
                group_id=int(group_id), user_id=int(target_id), enable=not enable
            )

        verb = "设为管理员" if enable else "取消管理员"
        self.undo.push(
            group_id,
            operator_id=event.get_sender_id(),
            action="set_admin",
            description=f"把 {name}({target_id}) {verb}",
            revert=revert,
        )

    # ------------------------------------------------------------ 群名头像 --- #
    async def set_group_name(self, event: AstrMessageEvent, group_name: Any = None) -> str:
        name = str(group_name or "").strip()
        if not name:
            return "未输入新群名"
        group_id = event.get_group_id()
        try:
            await event.bot.set_group_name(group_id=int(group_id), group_name=name)
        except Exception as exc:  # noqa: BLE001
            await self.log(event, "set_group_name", detail=str(exc), success=False)
            return f"修改群名失败：{exc}"
        await self.log(event, "set_group_name", detail=name)
        self.groups.invalidate(group_id)
        return f"本群群名更新为：{name}"

    async def set_portrait(self, event: AstrMessageEvent, image_url: str | None = None) -> str:
        url = image_url or extract_image_url(event)
        if not url:
            return "未获取到新头像，请在指令里附图或引用一张图片"
        group_id = event.get_group_id()
        try:
            await event.bot.set_group_portrait(group_id=int(group_id), file=url)
        except Exception as exc:  # noqa: BLE001
            await self.log(event, "set_portrait", detail=str(exc), success=False)
            return f"更新群头像失败：{exc}"
        await self.log(event, "set_portrait")
        return "群头像已更新"

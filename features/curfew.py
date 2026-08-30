"""宵禁：在指定时间段自动开启 / 关闭全员禁言。

相比上游做了这些修复：
- 恢复历史任务时会正确传入 manager，避免失败时无法自愈；
- 协议端 client 尚未就绪时不再抛 UnboundLocalError，改为按需惰性创建管理器；
- 插件卸载时会关停调度器，不再残留后台线程。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import zoneinfo
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.job import Job
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..core.config import LOG_TAG
from .base import Feature, FeatureContext


def parse_time(raw: str) -> tuple[str, int, int] | None:
    """解析 HH:MM（兼容中文冒号），返回 (标准化字符串, 时, 分)。"""
    try:
        cleaned = str(raw).strip().replace("：", ":")
        hour_text, minute_text = cleaned.split(":")
        hour, minute = int(hour_text), int(minute_text)
    except Exception:  # noqa: BLE001
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}", hour, minute


class CurfewStore:
    """宵禁配置持久化：{bot_id: {group_id: {start_time, end_time}}}。"""

    def __init__(self, file: Path) -> None:
        self.file = file
        self.data: dict[str, dict[str, dict[str, str]]] = {}

    def load(self) -> None:
        if not self.file.exists():
            self.data = {}
            return
        try:
            self.data = json.loads(self.file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG} 加载宵禁配置失败: {exc}")
            self.data = {}

    def save(self) -> None:
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            self.file.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG} 保存宵禁配置失败: {exc}")


class GroupCurfew:
    """单个群的宵禁任务，维护「开始」「结束」两个定时任务。"""

    def __init__(
        self,
        client: Any,
        group_id: str,
        start_time: str,
        end_time: str,
        scheduler: AsyncIOScheduler,
        manager: BotCurfewManager | None = None,
    ) -> None:
        self.client = client
        self.group_id = group_id
        self.start_time = start_time
        self.end_time = end_time
        self.scheduler = scheduler
        self.manager = manager
        self.start_job: Job | None = None
        self.end_job: Job | None = None
        self.whole_ban_status = False
        self._lock = asyncio.Lock()

    async def enable(self) -> None:
        async with self._lock:
            if self.whole_ban_status:
                return
            self.whole_ban_status = True
        try:
            await self.client.send_group_msg(
                group_id=int(self.group_id), message=f"【{self.start_time}】本群宵禁开始！"
            )
            await self.client.set_group_whole_ban(group_id=int(self.group_id), enable=True)
            logger.info(f"{LOG_TAG} 群 {self.group_id} 已进入宵禁")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG} 群 {self.group_id} 宵禁开启失败: {exc}")
            async with self._lock:
                self.whole_ban_status = False
            if self.manager is not None:
                await self.manager.remove_group_on_error(self.group_id)

    async def disable(self) -> None:
        async with self._lock:
            if not self.whole_ban_status:
                return
            self.whole_ban_status = False
        try:
            await self.client.send_group_msg(
                group_id=int(self.group_id), message=f"【{self.end_time}】本群宵禁结束！"
            )
            await self.client.set_group_whole_ban(group_id=int(self.group_id), enable=False)
            logger.info(f"{LOG_TAG} 群 {self.group_id} 已解除宵禁")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG} 群 {self.group_id} 宵禁解除失败: {exc}")
            async with self._lock:
                self.whole_ban_status = True

    async def schedule(self) -> None:
        parsed_start = parse_time(self.start_time)
        parsed_end = parse_time(self.end_time)
        if not parsed_start or not parsed_end:
            raise ValueError(f"宵禁时间无效：{self.start_time}~{self.end_time}")
        _, start_h, start_m = parsed_start
        _, end_h, end_m = parsed_end

        self.start_job = self.scheduler.add_job(
            self.enable,
            trigger=CronTrigger(hour=start_h, minute=start_m),
            name=f"curfew_start_{self.group_id}",
            misfire_grace_time=60,
        )
        self.end_job = self.scheduler.add_job(
            self.disable,
            trigger=CronTrigger(hour=end_h, minute=end_m),
            name=f"curfew_end_{self.group_id}",
            misfire_grace_time=60,
        )

        # 插件在宵禁时间段内启动时，立即补上全员禁言
        now = datetime.now(self.scheduler.timezone)
        start_dt = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        end_dt = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        if start_dt >= end_dt:
            end_dt += timedelta(days=1)
        if start_dt <= now < end_dt:
            await self.enable()

    def unschedule(self) -> None:
        for job in (self.start_job, self.end_job):
            if job is None:
                continue
            with contextlib.suppress(Exception):  # 调度器可能已被重建
                job.remove()
        self.start_job = None
        self.end_job = None


class BotCurfewManager:
    """一个机器人账号下所有群的宵禁调度。"""

    def __init__(
        self, client: Any, bot_id: str, store: CurfewStore, scheduler: AsyncIOScheduler
    ) -> None:
        self.client = client
        self.bot_id = bot_id
        self.store = store
        self.scheduler = scheduler
        self.store.data.setdefault(bot_id, {})
        self.tasks: dict[str, GroupCurfew] = {}

    @property
    def bot_data(self) -> dict[str, dict[str, str]]:
        return self.store.data.setdefault(self.bot_id, {})

    async def restore(self) -> None:
        for group_id, times in list(self.bot_data.items()):
            try:
                task = GroupCurfew(
                    self.client,
                    group_id,
                    times["start_time"],
                    times["end_time"],
                    self.scheduler,
                    manager=self,  # 上游漏了这个参数，导致失败时无法自愈
                )
                await task.schedule()
                self.tasks[group_id] = task
            except Exception as exc:  # noqa: BLE001
                logger.error(f"{LOG_TAG} 恢复群 {group_id} 宵禁失败: {exc}")

    def _persist(self) -> None:
        data = self.bot_data
        data.clear()
        data.update(
            {
                gid: {"start_time": task.start_time, "end_time": task.end_time}
                for gid, task in self.tasks.items()
            }
        )
        self.store.save()

    async def remove_group_on_error(self, group_id: str) -> None:
        task = self.tasks.pop(group_id, None)
        if task:
            task.unschedule()
        self._persist()
        logger.info(f"{LOG_TAG} 群 {group_id} 宵禁因操作失败已移除")

    async def enable(self, group_id: str, start_time: str, end_time: str) -> None:
        existing = self.tasks.pop(group_id, None)
        if existing:
            existing.unschedule()
        task = GroupCurfew(
            self.client, group_id, start_time, end_time, self.scheduler, manager=self
        )
        await task.schedule()
        self.tasks[group_id] = task
        self._persist()

    async def disable(self, group_id: str) -> bool:
        task = self.tasks.pop(group_id, None)
        if not task:
            return False
        task.unschedule()
        self._persist()
        return True

    def snapshot(self) -> list[dict[str, str]]:
        return [
            {"group_id": gid, "start_time": task.start_time, "end_time": task.end_time}
            for gid, task in self.tasks.items()
        ]


class CurfewFeature(Feature):
    """宵禁功能入口，按机器人账号维护多个管理器。"""

    def __init__(self, ctx: FeatureContext) -> None:
        super().__init__(ctx)
        self.timezone = self._resolve_timezone(self.config.timezone)
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.store_file = CurfewStore(self.config.curfew_path)
        self.store_file.load()
        self.managers: dict[str, BotCurfewManager] = {}

    @staticmethod
    def _resolve_timezone(name: str) -> zoneinfo.ZoneInfo:
        try:
            return zoneinfo.ZoneInfo(name)
        except Exception:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 时区 {name} 无效，回退到 Asia/Shanghai")
            return zoneinfo.ZoneInfo("Asia/Shanghai")

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()

    async def restore_all(self) -> None:
        """按已保存的配置恢复宵禁任务。协议端未就绪时静默跳过，等下次触发再恢复。"""
        self.start()
        clients = self._iter_clients()
        if not clients:
            logger.debug(f"{LOG_TAG} 暂无可用的 aiocqhttp 连接，宵禁恢复稍后重试")
            return
        for bot_id, client in clients.items():
            if bot_id in self.managers:
                continue
            manager = BotCurfewManager(client, bot_id, self.store_file, self.scheduler)
            self.managers[bot_id] = manager
            await manager.restore()
        logger.info(f"{LOG_TAG} 宵禁任务恢复完成，共 {len(self.managers)} 个账号")

    def _iter_clients(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        try:
            insts = self.ctx.context.platform_manager.platform_insts
        except Exception:  # noqa: BLE001
            return result
        for inst in insts:
            meta = getattr(inst, "meta", None)
            name = getattr(meta(), "name", "") if callable(meta) else getattr(meta, "name", "")
            if name != "aiocqhttp":
                continue
            getter = getattr(inst, "get_client", None)
            client = getter() if callable(getter) else None
            if client is None:
                continue
            bot_id = str(getattr(client, "_self_id", "") or "")
            if not bot_id:
                # 尚未握手完成，用适配器 id 兜底，等到指令触发时会用真实 self_id 覆盖
                continue
            result[bot_id] = client
        return result

    async def _manager_for(self, event: AstrMessageEvent) -> BotCurfewManager:
        """按需取 / 建当前账号的宵禁管理器。"""
        self.start()
        bot_id = str(event.get_self_id())
        manager = self.managers.get(bot_id)
        if manager is None:
            manager = BotCurfewManager(event.bot, bot_id, self.store_file, self.scheduler)
            self.managers[bot_id] = manager
            await manager.restore()
        else:
            manager.client = event.bot
        return manager

    # -------------------------------------------------------------- 指令 --- #
    async def enable(
        self,
        event: AstrMessageEvent,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> str:
        if not start_time or not end_time:
            return "用法：开启宵禁 23:00 07:00"
        parsed_start = parse_time(start_time)
        parsed_end = parse_time(end_time)
        if not parsed_start or not parsed_end:
            return "时间格式错误，应为 HH:MM，例如 23:00"
        start_str, start_h, start_m = parsed_start
        end_str, end_h, end_m = parsed_end
        if (start_h, start_m) == (end_h, end_m):
            return "开始时间和结束时间不能相同"

        manager = await self._manager_for(event)
        group_id = event.get_group_id()
        try:
            await manager.enable(group_id, start_str, end_str)
        except Exception as exc:  # noqa: BLE001
            await self.log(event, "curfew_on", detail=str(exc), success=False)
            return f"宵禁任务创建失败：{exc}"
        await self.log(event, "curfew_on", detail=f"{start_str}~{end_str}")
        return f"宵禁任务已创建：每天 {start_str} ~ {end_str} 自动全员禁言"

    async def disable(self, event: AstrMessageEvent) -> str:
        manager = await self._manager_for(event)
        group_id = event.get_group_id()
        if await manager.disable(group_id):
            await self.log(event, "curfew_off")
            return "本群宵禁任务已取消"
        return "本群没有宵禁任务"

    def snapshot(self) -> list[dict[str, str]]:
        """所有账号的宵禁任务，供 WebUI 展示。"""
        items: list[dict[str, str]] = []
        for bot_id, manager in self.managers.items():
            for entry in manager.snapshot():
                items.append({"bot_id": bot_id, **entry})
        return items

    async def shutdown(self) -> None:
        for manager in self.managers.values():
            for task in list(manager.tasks.values()):
                task.unschedule()
            manager.tasks.clear()
        self.store_file.save()
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

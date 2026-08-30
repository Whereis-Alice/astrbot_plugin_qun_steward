"""群列表 / 群信息缓存。

WebUI 需要频繁展示「机器人在哪些群、是不是管理员」，直接打协议端会很慢，
这里做一层 TTL 缓存。

重要：接口异常时只降级为「缓存数据」，绝不会因为拿不到群信息就删掉群配置
（上游插件的 member_count <= 0 就删配置是高危 bug）。
"""

from __future__ import annotations

import asyncio
import copy
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context

from .config import LOG_TAG
from .store import GroupStore

#: 机器人身份排序优先级：群主 > 管理员 > 普通成员
_ROLE_PRIORITY: dict[str, int] = {"owner": 0, "admin": 1, "member": 2, "unknown": 3}

_ROLE_LABELS: dict[str, str] = {
    "owner": "群主",
    "admin": "管理员",
    "member": "成员",
    "unknown": "未知",
}


def role_label(role: str) -> str:
    return _ROLE_LABELS.get(role, "未知")


def group_avatar(group_id: Any) -> str:
    return f"https://p.qlogo.cn/gh/{group_id}/{group_id}/640"


def user_avatar(user_id: Any) -> str:
    return f"https://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640"


def _as_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def _as_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        data = payload.get("data")
        return data if isinstance(data, dict) else payload
    return {}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class GroupInfoCache:
    """聚合所有 aiocqhttp 实例的群列表，并缓存机器人在各群的身份。"""

    def __init__(self, context: Context, store: GroupStore, ttl: int = 90) -> None:
        self._context = context
        self._store = store
        self._ttl = ttl
        self._lock = asyncio.Lock()
        self._role_lock = asyncio.Lock()
        self._refreshed_at = 0.0
        self._groups: dict[str, dict[str, Any]] = {}
        self._clients: dict[str, Any] = {}
        self._roles: dict[str, str] = {}
        self._bot_ids: dict[int, str] = {}

    # ---------------------------------------------------------------- 公开 --- #
    async def list_groups(self, force: bool = False) -> list[dict[str, Any]]:
        if force or not self._fresh or not self._groups:
            await self._refresh(force=force)
        return self._sorted()

    async def list_groups_with_bot_roles(self, force: bool = False) -> list[dict[str, Any]]:
        if force or not self._fresh or not self._groups:
            await self._refresh(force=force)
        await self._hydrate_roles(force=force)
        return self._sorted()

    async def get_group(self, group_id: Any, force: bool = False) -> dict[str, Any]:
        gid = str(group_id).strip()
        if not gid:
            raise ValueError("群号不能为空")
        if force or not self._fresh or gid not in self._groups:
            await self._refresh(force=force)
        group = copy.deepcopy(self._groups.get(gid) or self._fallback(gid))
        group["bot_role"] = self._roles.get(gid, "unknown")
        group["bot_role_label"] = role_label(group["bot_role"])
        return group

    def invalidate(self, group_id: Any | None = None) -> None:
        """让缓存失效。注意这里只清缓存，不会动任何配置数据。"""
        if group_id is None:
            self._groups.clear()
            self._roles.clear()
            self._refreshed_at = 0.0
            return
        gid = str(group_id).strip()
        self._roles.pop(gid, None)
        self._refreshed_at = 0.0

    async def client_for(self, group_id: Any) -> Any | None:
        """取一个能操作该群的协议端 client。"""
        gid = str(group_id).strip()
        if not self._fresh or gid not in self._clients:
            await self._refresh(force=False)
        client = self._clients.get(gid)
        if client is not None:
            return client
        clients = self._iter_clients()
        return clients[0] if clients else None

    # ---------------------------------------------------------------- 内部 --- #
    @property
    def _fresh(self) -> bool:
        return (time.time() - self._refreshed_at) < self._ttl

    def _iter_clients(self) -> list[Any]:
        clients: list[Any] = []
        try:
            insts = self._context.platform_manager.platform_insts
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{LOG_TAG} 读取平台实例失败: {exc}")
            return clients
        for inst in insts:
            meta = getattr(inst, "meta", None)
            name = getattr(meta(), "name", "") if callable(meta) else getattr(meta, "name", "")
            if name != "aiocqhttp":
                continue
            getter = getattr(inst, "get_client", None)
            if not callable(getter):
                continue
            try:
                client = getter()
            except Exception:  # noqa: BLE001
                continue
            if client is not None:
                clients.append(client)
        return clients

    async def _refresh(self, force: bool = False) -> None:
        async with self._lock:
            if not force and self._fresh and self._groups:
                return
            groups: dict[str, dict[str, Any]] = {}
            clients: dict[str, Any] = {}

            for client in self._iter_clients():
                try:
                    payload = await client.call_action("get_group_list")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"{LOG_TAG} 获取群列表失败: {exc}")
                    continue
                for item in _as_list(payload):
                    gid = str(item.get("group_id") or "").strip()
                    if not gid or gid in groups:
                        continue
                    groups[gid] = self._normalize(item)
                    clients[gid] = client

            # 有配置但当前拿不到的群，用占位数据补上，保证 WebUI 里配置不会「凭空消失」
            for gid in self._store.overridden_group_ids():
                groups.setdefault(gid, self._fallback(gid))

            if not groups and self._groups:
                # 协议端全挂了：保留旧缓存，什么都不改
                logger.warning(f"{LOG_TAG} 群列表刷新失败，继续使用上一次缓存")
                return

            self._groups = groups
            self._clients = clients
            self._refreshed_at = time.time()

    async def _hydrate_roles(self, force: bool = False) -> None:
        async with self._role_lock:
            targets = [gid for gid in self._groups if force or gid not in self._roles]
            if not targets:
                return
            semaphore = asyncio.Semaphore(8)

            async def load(gid: str) -> None:
                async with semaphore:
                    self._roles[gid] = await self._fetch_role(gid)

            await asyncio.gather(*(load(gid) for gid in targets))

    async def _fetch_role(self, gid: str) -> str:
        candidates: list[Any] = []
        preferred = self._clients.get(gid)
        if preferred is not None:
            candidates.append(preferred)
        candidates.extend(c for c in self._iter_clients() if c is not preferred)

        for client in candidates:
            bot_id = await self._bot_id(client)
            if not bot_id.isdigit():
                continue
            try:
                payload = await client.call_action(
                    "get_group_member_info",
                    group_id=int(gid),
                    user_id=int(bot_id),
                    no_cache=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"{LOG_TAG} 获取机器人身份失败 group={gid}: {exc}")
                continue
            role = str(_as_dict(payload).get("role") or "").lower()
            if role in {"owner", "admin", "member"}:
                return role
        return "unknown"

    async def _bot_id(self, client: Any) -> str:
        key = id(client)
        cached = self._bot_ids.get(key)
        if cached:
            return cached
        bot_id = ""
        try:
            payload = await client.call_action("get_login_info")
            bot_id = str(_as_dict(payload).get("user_id") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{LOG_TAG} 获取登录信息失败: {exc}")
        if bot_id:
            self._bot_ids[key] = bot_id
        return bot_id

    def _sorted(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for gid, group in self._groups.items():
            entry = copy.deepcopy(group)
            entry["bot_role"] = self._roles.get(gid, "unknown")
            entry["bot_role_label"] = role_label(entry["bot_role"])
            entry["customized"] = not self._store.follows_default(gid)
            items.append(entry)
        return sorted(
            items,
            key=lambda item: (
                _ROLE_PRIORITY.get(str(item.get("bot_role")), 3),
                not str(item.get("group_id", "")).isdigit(),
                _to_int(item.get("group_id")),
            ),
        )

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
        gid = str(raw.get("group_id") or "").strip()
        return {
            "group_id": gid,
            "group_name": str(raw.get("group_name") or "").strip() or f"群 {gid}",
            "avatar": group_avatar(gid),
            "member_count": _to_int(raw.get("member_count")),
            "max_member_count": _to_int(raw.get("max_member_count")),
            "source": "live",
        }

    @staticmethod
    def _fallback(gid: str) -> dict[str, Any]:
        return {
            "group_id": gid,
            "group_name": f"群 {gid}",
            "avatar": group_avatar(gid),
            "member_count": 0,
            "max_member_count": 0,
            "source": "cached",
        }

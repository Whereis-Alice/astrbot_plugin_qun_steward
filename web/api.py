"""WebUI HTTP 接口层：把 StewardWebService 暴露成插件 API 路由。

所有响应统一为 JSON：成功 -> {"ok": true, "data": ...}，失败 -> {"ok": false, "error": "..."}。
前端通过 AstrBot 注入的 window.AstrBotPluginPage.apiGet / apiPost 访问，
无需关心完整前缀（实际路径为 /api/plug/<插件名>/<子路径>）。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from astrbot.api import logger

from ..core.config import LOG_TAG, PLUGIN_NAME
from ..features.base import FeatureContext
from .service import StewardWebService

try:  # pragma: no cover - 运行时由 AstrBot 提供
    from quart import jsonify, request
except Exception:  # pragma: no cover  # noqa: BLE001
    jsonify = None  # type: ignore[assignment]
    request = None  # type: ignore[assignment]


def _ok(data: Any = None) -> Any:
    return jsonify({"ok": True, "data": data})


def _fail(message: str, status: int = 400) -> Any:
    response = jsonify({"ok": False, "error": message})
    return response, status


class StewardWebController:
    """注册并处理插件 WebUI 的全部接口。"""

    def __init__(self, ctx: FeatureContext, plugin: Any) -> None:
        self.ctx = ctx
        self.service = StewardWebService(ctx, plugin)
        self._registered = False

    # ------------------------------------------------------------------ 注册

    def _routes(self) -> list[tuple[str, list[str], Callable[..., Any], str]]:
        return [
            ("ping", ["GET"], self.ping, "健康检查"),
            ("overview", ["GET"], self.overview, "总览数据"),
            ("groups", ["GET"], self.groups, "群列表"),
            ("fields", ["GET"], self.fields, "群配置字段元信息"),
            ("group", ["GET"], self.group, "查看单群配置"),
            ("group/save", ["POST"], self.group_save, "保存单群配置"),
            ("group/import", ["POST"], self.group_import, "文本导入单群配置"),
            ("group/reset", ["POST"], self.group_reset, "重置群配置为默认"),
            ("defaults", ["GET"], self.defaults, "查看默认模板"),
            ("defaults/save", ["POST"], self.defaults_save, "保存默认模板"),
            ("perms", ["GET"], self.perms, "查看权限矩阵"),
            ("perms/save", ["POST"], self.perms_save, "保存权限矩阵"),
            ("settings", ["GET"], self.settings, "查看全局设置"),
            ("settings/save", ["POST"], self.settings_save, "保存全局设置"),
            ("words", ["GET"], self.words, "查看违禁词配置"),
            ("audit", ["GET"], self.audit, "查询操作日志"),
            ("join/pending", ["GET"], self.pending_joins, "查看待审进群申请"),
            ("undo", ["GET"], self.undo_stack, "查看可撤销操作"),
            ("undo/run", ["POST"], self.undo_run, "执行撤销"),
            ("album", ["GET"], self.album, "查看群相册配置"),
            ("runtime", ["GET"], self.runtime, "运行期信息"),
        ]

    def register_routes(self) -> None:
        """把所有路由注册到 AstrBot。重复调用是安全的。"""
        if self._registered:
            return
        if jsonify is None or request is None:
            logger.warning("%s 未检测到 quart，WebUI 接口未注册", LOG_TAG)
            return
        register = getattr(self.ctx.context, "register_web_api", None)
        if not callable(register):
            logger.warning("%s 当前 AstrBot 版本不支持插件 Web API", LOG_TAG)
            return
        ok = 0
        for suffix, methods, handler, desc in self._routes():
            route = f"/{PLUGIN_NAME}/{suffix}"
            try:
                register(route, self._wrap(suffix, handler), methods, desc)
                ok += 1
            except Exception as exc:  # 注册冲突不应影响插件加载  # noqa: BLE001
                logger.error("%s 注册接口 %s 失败：%s", LOG_TAG, route, exc)
        self._registered = True
        logger.info("%s WebUI 接口已注册 %d 个", LOG_TAG, ok)

    def _wrap(self, suffix: str, handler: Callable[..., Any]) -> Callable[..., Any]:
        async def endpoint(*_args: Any, **_kwargs: Any) -> Any:
            try:
                result = handler()
                if inspect.isawaitable(result):
                    result = await result
                return _ok(result)
            except ValueError as exc:
                return _fail(str(exc) or "参数不合法", 400)
            except Exception as exc:  # 兜底，避免 500 页面  # noqa: BLE001
                logger.exception("%s 接口 %s 异常", LOG_TAG, suffix)
                return _fail(f"服务器内部错误：{exc}", 500)

        endpoint.__name__ = "steward_" + suffix.replace("/", "_")
        return endpoint

    # -------------------------------------------------------------- 请求解析

    @staticmethod
    def _query(key: str, default: str = "") -> str:
        return str(request.args.get(key, default) or "").strip()

    @staticmethod
    def _query_int(key: str, default: int, *, minimum: int = 0, maximum: int = 500) -> int:
        raw = str(request.args.get(key, "") or "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"参数 {key} 需要整数") from exc
        return max(minimum, min(maximum, value))

    @staticmethod
    def _query_bool(key: str) -> bool:
        return str(request.args.get(key, "") or "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    async def _body() -> dict[str, Any]:
        data = await request.get_json(force=True, silent=True)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError("请求体需要 JSON 对象")
        return data

    @staticmethod
    def _need_group(data: dict[str, Any] | None = None) -> str:
        if data is None:
            group_id = StewardWebController._query("group_id")
        else:
            group_id = str(data.get("group_id", "") or "").strip()
        if not group_id:
            raise ValueError("缺少 group_id")
        return group_id

    @staticmethod
    def _need_changes(data: dict[str, Any]) -> dict[str, Any]:
        changes = data.get("changes")
        if changes is None:
            changes = {}
        if not isinstance(changes, dict):
            raise ValueError("changes 需要 JSON 对象")
        return changes

    # ------------------------------------------------------------------ 处理

    def ping(self) -> dict[str, Any]:
        return {"plugin": PLUGIN_NAME, "version": self.service.version()}

    async def overview(self) -> dict[str, Any]:
        return await self.service.overview()

    async def groups(self) -> list[dict[str, Any]]:
        return await self.service.groups(force=self._query_bool("force"))

    def fields(self) -> list[dict[str, Any]]:
        return self.service.fields()

    def group(self) -> dict[str, Any]:
        return self.service.group_config(self._need_group())

    async def group_save(self) -> dict[str, Any]:
        data = await self._body()
        return await self.service.save_group_config(self._need_group(data), self._need_changes(data))

    async def group_import(self) -> dict[str, Any]:
        data = await self._body()
        text = str(data.get("text", "") or "")
        if not text.strip():
            raise ValueError("导入内容为空")
        return await self.service.import_group_text(self._need_group(data), text)

    async def group_reset(self) -> dict[str, Any]:
        data = await self._body()
        group_id = str(data.get("group_id", "") or "").strip() or None
        return await self.service.reset_group(group_id)

    def defaults(self) -> dict[str, Any]:
        return self.service.defaults()

    async def defaults_save(self) -> dict[str, Any]:
        data = await self._body()
        return await self.service.save_defaults(self._need_changes(data))

    def perms(self) -> dict[str, Any]:
        return self.service.perms()

    async def perms_save(self) -> dict[str, Any]:
        data = await self._body()
        return self.service.save_perms(self._need_changes(data))

    def settings(self) -> dict[str, Any]:
        return self.service.settings()

    async def settings_save(self) -> dict[str, Any]:
        data = await self._body()
        section = str(data.get("section", "") or "").strip()
        if not section:
            raise ValueError("缺少 section")
        return self.service.save_settings(section, self._need_changes(data))

    def words(self) -> dict[str, Any]:
        return self.service.words(self._need_group())

    async def audit(self) -> dict[str, Any]:
        return await self.service.audit(
            group_id=self._query("group_id") or None,
            action=self._query("action") or None,
            keyword=self._query("keyword") or None,
            limit=self._query_int("limit", 50, minimum=1, maximum=200),
            offset=self._query_int("offset", 0, minimum=0, maximum=100000),
        )

    async def pending_joins(self) -> list[dict[str, Any]]:
        return await self.service.pending_joins(self._query("group_id") or None)

    def undo_stack(self) -> list[dict[str, Any]]:
        return self.service.undo_stack(self._need_group())

    async def undo_run(self) -> dict[str, Any]:
        data = await self._body()
        return await self.service.run_undo(self._need_group(data))

    async def album(self) -> dict[str, Any]:
        return await self.service.album(self._need_group())

    def runtime(self) -> dict[str, Any]:
        return self.service.runtime()

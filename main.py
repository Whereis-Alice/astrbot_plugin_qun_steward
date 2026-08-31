"""群务管家（astrbot_plugin_qun_steward）—— 指令注册与事件编排层。

本文件只做三件事：

1. 组装依赖：配置 / 数据库 / 群配置存储 / 权限 / 审计 / 撤销栈 / 群信息缓存；
2. 把 AstrBot 的指令、被动事件、LLM 工具映射到 features 包里的业务方法；
3. 管理插件生命周期：初始化、后台维护任务、退出清理。

业务逻辑一律不写在这里，方便单独测试各功能模块。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core.audit import AuditLog
from .core.config import LOG_TAG, StewardConfig
from .core.db import Database
from .core.group_cache import GroupInfoCache
from .core.permission import PermissionResolver, PermLevel, perm_required
from .core.store import GroupStore
from .core.undo import UndoStack
from .core.utils import parse_bool
from .features.album import AlbumFeature
from .features.base import FeatureContext, args_of, rest_of
from .features.config_cmd import ConfigFeature
from .features.curfew import CurfewFeature
from .features.essence import EssenceFeature
from .features.files import FilesFeature
from .features.join import JoinFeature
from .features.member import MemberFeature
from .features.moderation import ModerationFeature
from .features.notice import NoticeFeature
from .features.recall import RecallFeature
from .features.vote import VoteFeature
from .features.words import WordFeature
from .web import StewardWebController

#: 后台维护任务的间隔（秒）：清理刷屏计数、过期审计日志、过期进群申请
MAINTENANCE_INTERVAL = 3600

#: 超过这个长度（或含换行）的回复渲染成图片，避免刷屏
RICH_TEXT_THRESHOLD = 120


def _arg(event: AstrMessageEvent, index: int = 0) -> str:
    """取指令的第 index 个参数，取不到返回空串。"""
    args = args_of(event)
    return args[index] if len(args) > index else ""


def _optional(event: AstrMessageEvent, index: int = 0) -> str | None:
    """取参数；没写参数时返回 None，让业务层走「查询当前值」分支。"""
    return _arg(event, index) or None


def _switch(event: AstrMessageEvent, default: bool = True) -> bool:
    """解析「开 / 关」参数，识别不出来时用 default。"""
    parsed = parse_bool(_arg(event), default=None)
    return default if parsed is None else parsed


class QunStewardPlugin(Star):
    """群务管家主插件：QQ 群管理 + 群相册。"""

    def __init__(self, context: Context, config: Any) -> None:
        super().__init__(context)
        self.cfg = StewardConfig(config, context)
        self.db = Database(self.cfg.db_path)
        self.store = GroupStore(self.db, self.cfg.build_group_defaults())
        # perm_required 装饰器会读这个属性，名字不要改
        self.permissions = PermissionResolver(self.cfg, self.store)
        self.audit = AuditLog(self.db, self.cfg)
        self.undo = UndoStack(self.cfg)
        self.groups = GroupInfoCache(context, self.store)

        feature_ctx = FeatureContext(
            context=context,
            config=self.cfg,
            store=self.store,
            permissions=self.permissions,
            audit=self.audit,
            undo=self.undo,
            groups=self.groups,
            db=self.db,
            to_image=self.text_to_image,
        )
        self.feature_ctx = feature_ctx

        self.moderation = ModerationFeature(feature_ctx)
        self.essence = EssenceFeature(feature_ctx)
        self.recall = RecallFeature(feature_ctx)
        self.notice = NoticeFeature(feature_ctx)
        self.words = WordFeature(feature_ctx)
        self.vote = VoteFeature(feature_ctx)
        self.curfew = CurfewFeature(feature_ctx)
        self.join = JoinFeature(feature_ctx)
        self.member = MemberFeature(feature_ctx)
        self.files = FilesFeature(feature_ctx)
        self.album = AlbumFeature(feature_ctx)
        self.configs = ConfigFeature(feature_ctx)

        self.web = StewardWebController(feature_ctx, self)
        self.web.register_routes()

        self._tasks: set[asyncio.Task[Any]] = set()

    # ================================================================ 生命周期

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """起一个后台任务并持有引用，避免被 GC 提前回收。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def initialize(self) -> None:
        await self.db.connect()
        await self.store.load()
        self.curfew.start()
        self._spawn(self.curfew.restore_all())
        self._spawn(self.album.initialize())
        self._spawn(self._maintenance_loop())
        logger.info(f"{LOG_TAG} 初始化完成，已加载 {len(self.store.overridden_group_ids())} 个自定义群配置")

    async def terminate(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        await self.vote.shutdown()
        await self.curfew.shutdown()
        await self.db.close()
        logger.info(f"{LOG_TAG} 已卸载")

    async def _maintenance_loop(self) -> None:
        """定时清理：刷屏计数、过期审计日志、过期进群申请。"""
        while True:
            try:
                await asyncio.sleep(MAINTENANCE_INTERVAL)
                self.words.prune()
                logs = await self.audit.purge()
                requests = await self.join.purge_requests(self.cfg.audit.int("retain_days", 30))
                if logs or requests:
                    logger.debug(f"{LOG_TAG} 维护完成：清理审计 {logs} 条、进群申请 {requests} 条")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{LOG_TAG} 后台维护任务出错：{exc}")

    @filter.on_platform_loaded()
    async def on_platform_loaded(self) -> None:
        """平台就绪后兜底恢复宵禁计划：initialize 阶段可能还没有 bot 实例。"""
        if not self.curfew.snapshot():
            self._spawn(self.curfew.restore_all())

    # ================================================================ 公共工具

    async def _rich(self, event: AstrMessageEvent, text: str) -> Any:
        """长文本渲染成图片，短文本直接发文字；渲染失败自动回退。"""
        if not text:
            return None
        if len(text) <= RICH_TEXT_THRESHOLD and "\n" not in text:
            return event.plain_result(text)
        try:
            return event.image_result(await self.text_to_image(text))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 文本转图片失败，回退纯文本：{exc}")
            return event.plain_result(text)

    # ================================================================ 插件自身

    @filter.command("群务帮助", alias={"群管帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """群务帮助：查看全部指令"""
        yield event.image_result(await self.text_to_image(self.configs.help_markdown()))

    @filter.command("群管配置", alias={"群管设置"})
    @perm_required(PermLevel.MEMBER, perm_key="set_config", check_at=False)
    async def cmd_config(self, event: AstrMessageEvent):
        """群管配置 [群号] [配置串]：不带配置串则导出当前配置"""
        # 导出的配置需要能被直接复制回来，所以固定用纯文本
        yield event.plain_result(await self.configs.show_or_apply(event))

    @filter.command("群管重置")
    @perm_required(PermLevel.MEMBER, perm_key="reset_config")
    async def cmd_config_reset(self, event: AstrMessageEvent):
        """群管重置 [群号|all]：让群回到跟随默认配置"""
        yield event.plain_result(await self.configs.reset(event))

    @filter.command("操作日志", alias={"群务日志"})
    @perm_required(PermLevel.MEMBER, perm_key="audit_view")
    async def cmd_audit(self, event: AstrMessageEvent):
        """操作日志 [关键词] [条数]"""
        result = await self._rich(event, await self.configs.show_audit(event))
        if result:
            yield result

    @filter.command("撤销", alias={"撤销操作"})
    @perm_required(PermLevel.ADMIN, perm_key="undo")
    async def cmd_undo(self, event: AstrMessageEvent):
        """撤销：回滚最近一次可撤销的危险操作"""
        if _arg(event) in {"列表", "查看", "list"}:
            yield event.plain_result(await self.configs.show_pending_undo(event))
            return
        yield event.plain_result(await self.configs.undo_last(event))

    # ================================================================ 禁言相关

    @filter.command("禁言")
    @perm_required(PermLevel.ADMIN, perm_key="set_group_ban")
    async def cmd_ban(self, event: AstrMessageEvent):
        """禁言 <秒数> @某人：秒数留空按随机区间取值"""
        yield event.plain_result(await self.moderation.ban(event, ban_time=_optional(event)))

    @filter.command("解禁")
    @perm_required(PermLevel.ADMIN, perm_key="cancel_group_ban")
    async def cmd_unban(self, event: AstrMessageEvent):
        """解禁 @某人"""
        yield event.plain_result(await self.moderation.unban(event))

    @filter.command("全禁", alias={"全员禁言"})
    @perm_required(PermLevel.ADMIN, perm_key="whole_ban")
    async def cmd_whole_ban(self, event: AstrMessageEvent):
        """全禁 开/关：不写参数默认开启"""
        yield event.plain_result(await self.moderation.whole_ban(event, _switch(event, True)))

    @filter.command("投票禁言")
    @perm_required(PermLevel.ADMIN, perm_key="vote")
    async def cmd_vote_start(self, event: AstrMessageEvent):
        """投票禁言 <秒数> @某人：发起投票"""
        yield event.plain_result(await self.vote.start(event, ban_time=_optional(event)))

    @filter.command("赞同禁言", alias={"同意禁言"})
    @perm_required(PermLevel.ADMIN, perm_key="vote")
    async def cmd_vote_agree(self, event: AstrMessageEvent):
        """赞同禁言：为进行中的投票投赞成票"""
        yield event.plain_result(await self.vote.cast(event, True))

    @filter.command("反对禁言", alias={"不同意禁言"})
    @perm_required(PermLevel.ADMIN, perm_key="vote")
    async def cmd_vote_disagree(self, event: AstrMessageEvent):
        """反对禁言：为进行中的投票投反对票"""
        yield event.plain_result(await self.vote.cast(event, False))

    @filter.command("开启宵禁")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @perm_required(PermLevel.ADMIN, perm_key="curfew")
    async def cmd_curfew_on(self, event: AstrMessageEvent):
        """开启宵禁 HH:MM HH:MM：到点自动全员禁言，到点自动解除"""
        yield event.plain_result(
            await self.curfew.enable(event, _optional(event, 0), _optional(event, 1))
        )

    @filter.command("关闭宵禁")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @perm_required(PermLevel.ADMIN, perm_key="curfew")
    async def cmd_curfew_off(self, event: AstrMessageEvent):
        """关闭宵禁：取消本群的宵禁计划"""
        yield event.plain_result(await self.curfew.disable(event))

    # ================================================================ 成员管理

    @filter.command("改名")
    @perm_required(PermLevel.ADMIN, perm_key="set_group_card")
    async def cmd_set_card(self, event: AstrMessageEvent):
        """改名 <新群名片> @某人：不 @ 人则改自己"""
        yield event.plain_result(await self.moderation.set_card(event, target_card=rest_of(event)))

    @filter.command("改头衔", alias={"头衔"})
    @perm_required(PermLevel.OWNER, perm_key="set_group_special_title")
    async def cmd_set_title(self, event: AstrMessageEvent):
        """改头衔 <头衔> @某人"""
        yield event.plain_result(
            await self.moderation.set_title(event, special_title=rest_of(event))
        )

    @filter.command("申请头衔", alias={"我要头衔"})
    @perm_required(PermLevel.OWNER, perm_key="set_group_special_title_me")
    async def cmd_set_title_me(self, event: AstrMessageEvent):
        """申请头衔 <头衔>：给自己设置头衔"""
        yield event.plain_result(
            await self.moderation.set_title(
                event, target_id=event.get_sender_id(), special_title=rest_of(event)
            )
        )

    @filter.command("踢了", alias={"踢出群聊"})
    @perm_required(PermLevel.ADMIN, perm_key="set_group_kick")
    async def cmd_kick(self, event: AstrMessageEvent):
        """踢了 @某人"""
        yield event.plain_result(await self.moderation.kick(event))

    @filter.command("群拉黑")
    @perm_required(PermLevel.ADMIN, perm_key="set_group_block")
    async def cmd_block(self, event: AstrMessageEvent):
        """群拉黑 @某人：踢出并加入群黑名单（不可撤销）"""
        yield event.plain_result(await self.moderation.block(event))

    @filter.command("上管", alias={"设置管理员"})
    @perm_required(PermLevel.OWNER, perm_key="admin", check_at=False)
    async def cmd_set_admin(self, event: AstrMessageEvent):
        """上管 @某人"""
        yield event.plain_result(await self.moderation.set_admin(event, True))

    @filter.command("下管", alias={"取消管理员"})
    @perm_required(PermLevel.OWNER, perm_key="admin", check_at=False)
    async def cmd_unset_admin(self, event: AstrMessageEvent):
        """下管 @某人"""
        yield event.plain_result(await self.moderation.set_admin(event, False))

    @filter.command("群友信息", alias={"群成员信息"})
    @perm_required(PermLevel.MEMBER, perm_key="get_group_member_list")
    async def cmd_member_list(self, event: AstrMessageEvent) -> None:
        """群友信息：导出群成员列表"""
        # 内部自行分批发送，这里不再 yield
        await self.member.member_list(event)

    @filter.command("清理群友")
    @perm_required(PermLevel.MEMBER, perm_key="clear_group_member")
    async def cmd_clear_members(self, event: AstrMessageEvent) -> None:
        """清理群友 <不活跃天数> <等级上限>：默认 30 天 / 10 级，需二次确认"""
        await self.member.clear_members(event, _arg(event, 0) or 30, _arg(event, 1) or 10)

    # ================================================================ 消息治理

    @filter.command("撤回")
    @perm_required(PermLevel.MEMBER, perm_key="delete_msg")
    async def cmd_recall(self, event: AstrMessageEvent):
        """撤回：引用消息撤回单条，或 @某人 + 数量批量撤回"""
        yield event.plain_result(await self.recall.recall(event))

    @filter.command("设精", alias={"设为精华"})
    @perm_required(PermLevel.ADMIN, perm_key="essence")
    async def cmd_essence_add(self, event: AstrMessageEvent):
        """设精：引用一条消息设为精华"""
        yield event.plain_result(await self.essence.set_essence(event, True))

    @filter.command("移精", alias={"移除精华"})
    @perm_required(PermLevel.ADMIN, perm_key="essence")
    async def cmd_essence_del(self, event: AstrMessageEvent):
        """移精：引用一条消息移出精华"""
        yield event.plain_result(await self.essence.set_essence(event, False))

    @filter.command("群精华", alias={"查看群精华"})
    @perm_required(PermLevel.ADMIN, perm_key="get_essence_msg_list")
    async def cmd_essence_list(self, event: AstrMessageEvent):
        """群精华：查看精华消息列表"""
        result = await self._rich(event, await self.essence.list_essence(event))
        if result:
            yield result

    @filter.command("设置禁词", alias={"禁词", "违禁词"})
    @perm_required(PermLevel.ADMIN, perm_key="word_ban")
    async def cmd_set_words(self, event: AstrMessageEvent):
        """设置禁词 +词 -词：带正负号增删，不带则整表覆写，留空查看"""
        yield event.plain_result(await self.words.manage_words(event))

    @filter.command("内置禁词")
    @perm_required(PermLevel.ADMIN, perm_key="word_ban")
    async def cmd_builtin_words(self, event: AstrMessageEvent):
        """内置禁词 开/关"""
        yield event.plain_result(await self.words.toggle_builtin(event, _optional(event)))

    @filter.command("禁词禁言")
    @perm_required(PermLevel.ADMIN, perm_key="word_ban")
    async def cmd_word_ban_time(self, event: AstrMessageEvent):
        """禁词禁言 <秒数>：0 表示只撤回不禁言"""
        yield event.plain_result(await self.words.word_ban_time(event, _optional(event)))

    @filter.command("刷屏禁言")
    @perm_required(PermLevel.ADMIN, perm_key="spamming")
    async def cmd_spamming_ban_time(self, event: AstrMessageEvent):
        """刷屏禁言 <秒数>：0 表示关闭刷屏检测"""
        yield event.plain_result(await self.words.spamming_ban_time(event, _optional(event)))

    # ================================================================ 群务设置

    @filter.command("设置群名")
    @perm_required(PermLevel.ADMIN, perm_key="set_group_name")
    async def cmd_set_group_name(self, event: AstrMessageEvent):
        """设置群名 <新群名>"""
        yield event.plain_result(await self.moderation.set_group_name(event, rest_of(event)))

    @filter.command("设置群头像")
    @perm_required(PermLevel.ADMIN, perm_key="set_group_portrait")
    async def cmd_set_portrait(self, event: AstrMessageEvent):
        """设置群头像：指令带图或引用一张图片"""
        yield event.plain_result(await self.moderation.set_portrait(event))

    @filter.command("发布群公告")
    @perm_required(PermLevel.ADMIN, perm_key="send_group_notice")
    async def cmd_publish_notice(self, event: AstrMessageEvent):
        """发布群公告 <内容>：可同时附一张图"""
        yield event.plain_result(await self.notice.publish(event, rest_of(event)))

    @filter.command("群公告", alias={"查看群公告"})
    @perm_required(PermLevel.MEMBER, perm_key="get_group_notice")
    async def cmd_view_notice(self, event: AstrMessageEvent):
        """群公告：查看已发布的群公告"""
        result = await self._rich(event, await self.notice.view(event))
        if result:
            yield result

    # ================================================================ 进群退群

    @filter.command("进群审核")
    @perm_required(PermLevel.ADMIN, perm_key="join")
    async def cmd_join_review(self, event: AstrMessageEvent):
        """进群审核 开/关：是否由插件接管进群申请"""
        yield event.plain_result(await self.join.toggle_review(event, _optional(event)))

    @filter.command("进群白词")
    @perm_required(PermLevel.ADMIN, perm_key="join")
    async def cmd_join_accept_words(self, event: AstrMessageEvent):
        """进群白词 <词1> <词2>：答案命中即自动通过"""
        yield event.plain_result(await self.join.set_accept_words(event))

    @filter.command("进群黑词")
    @perm_required(PermLevel.ADMIN, perm_key="join")
    async def cmd_join_reject_words(self, event: AstrMessageEvent):
        """进群黑词 <词1> <词2>：答案命中即自动驳回"""
        yield event.plain_result(await self.join.set_reject_words(event))

    @filter.command("未命中驳回")
    @perm_required(PermLevel.ADMIN, perm_key="join")
    async def cmd_join_no_match(self, event: AstrMessageEvent):
        """未命中驳回 开/关：答案没命中白词时是否直接驳回"""
        yield event.plain_result(await self.join.toggle_no_match_reject(event, _optional(event)))

    @filter.command("进群等级")
    @perm_required(PermLevel.ADMIN, perm_key="join")
    async def cmd_join_min_level(self, event: AstrMessageEvent):
        """进群等级 <数字>：QQ 等级低于此值直接驳回"""
        yield event.plain_result(await self.join.set_min_level(event, _optional(event)))

    @filter.command("进群次数")
    @perm_required(PermLevel.ADMIN, perm_key="join")
    async def cmd_join_max_time(self, event: AstrMessageEvent):
        """进群次数 <数字>：同一人反复申请超过次数后拉黑"""
        yield event.plain_result(await self.join.set_max_time(event, _optional(event)))

    @filter.command("进群黑名单")
    @perm_required(PermLevel.ADMIN, perm_key="join")
    async def cmd_join_block_ids(self, event: AstrMessageEvent):
        """进群黑名单 +QQ -QQ：增删黑名单，留空查看"""
        yield event.plain_result(await self.join.manage_block_ids(event))

    @filter.command("待审进群", alias={"待审申请", "进群申请"})
    @perm_required(PermLevel.ADMIN, perm_key="approve")
    async def cmd_join_pending(self, event: AstrMessageEvent):
        """待审进群：列出待处理的进群申请及序号"""
        result = await self._rich(event, await self.join.list_pending(event))
        if result:
            yield result

    @filter.command("批准", alias={"同意进群"})
    @perm_required(PermLevel.ADMIN, perm_key="approve")
    async def cmd_join_approve(self, event: AstrMessageEvent):
        """批准 [序号]：通过进群申请，也可直接引用申请通知"""
        yield event.plain_result(await self.join.handle_approval(event, True, rest_of(event)))

    @filter.command("驳回", alias={"拒绝进群", "不批准"})
    @perm_required(PermLevel.ADMIN, perm_key="approve")
    async def cmd_join_reject(self, event: AstrMessageEvent):
        """驳回 [序号] [理由]：拒绝进群申请"""
        yield event.plain_result(await self.join.handle_approval(event, False, rest_of(event)))

    @filter.command("进群禁言")
    @perm_required(PermLevel.ADMIN, perm_key="welcome")
    async def cmd_join_ban(self, event: AstrMessageEvent):
        """进群禁言 <秒数>：新成员入群后自动禁言，0 关闭"""
        yield event.plain_result(await self.join.set_join_ban(event, _optional(event)))

    @filter.command("进群欢迎")
    @perm_required(PermLevel.MEMBER, perm_key="welcome")
    async def cmd_join_welcome(self, event: AstrMessageEvent):
        """进群欢迎 <欢迎语>：支持 {nickname} 占位，留空查看，「关」清空"""
        yield event.plain_result(await self.join.set_welcome(event))

    @filter.command("退群通知")
    @perm_required(PermLevel.MEMBER, perm_key="leave")
    async def cmd_leave_notify(self, event: AstrMessageEvent):
        """退群通知 开/关：有人主动退群时在群里提示"""
        yield event.plain_result(await self.join.toggle_leave_notify(event, _optional(event)))

    @filter.command("退群拉黑")
    @perm_required(PermLevel.ADMIN, perm_key="leave")
    async def cmd_leave_block(self, event: AstrMessageEvent):
        """退群拉黑 开/关：主动退群者加入进群黑名单"""
        yield event.plain_result(await self.join.toggle_leave_block(event, _optional(event)))

    # ================================================================ 群文件

    @filter.command("查看群文件")
    @perm_required(PermLevel.MEMBER, perm_key="view_group_file")
    async def cmd_view_file(self, event: AstrMessageEvent):
        """查看群文件 [文件夹/序号] [文件/序号]"""
        result = await self._rich(event, await self.files.view(event, rest_of(event)))
        if result:
            yield result

    @filter.command("上传群文件")
    @perm_required(PermLevel.MEMBER, perm_key="upload_group_file")
    async def cmd_upload_file(self, event: AstrMessageEvent):
        """上传群文件 <文件夹名/文件名>：引用要上传的文件后使用"""
        yield event.plain_result(await self.files.upload(event, rest_of(event)))

    @filter.command("删除群文件")
    @perm_required(PermLevel.ADMIN, perm_key="delete_group_file")
    async def cmd_delete_file(self, event: AstrMessageEvent):
        """删除群文件 <文件夹名/文件名>"""
        yield event.plain_result(await self.files.delete(event, rest_of(event)))

    # ================================================================ 群相册

    @filter.command("上传群相册", alias={"up"})
    @perm_required(PermLevel.MEMBER, perm_key="album_upload")
    async def cmd_album_upload(self, event: AstrMessageEvent):
        """上传群相册 [相册名] [数量]：把图片或聊天记录长图存进群相册"""
        # 成功时返回空串：QQ 客户端本身会显示相册卡片，不必再刷一条文字
        if reply := await self.album.upload(event):
            yield event.plain_result(reply)

    # ================================================================ 被动监听

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_ban_words(self, event: AstrMessageEvent):
        """违禁词检测"""
        if result := await self.words.check_ban_words(event):
            yield event.plain_result(result)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_spamming(self, event: AstrMessageEvent):
        """刷屏检测"""
        if result := await self.words.check_spamming(event):
            yield event.plain_result(result)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_notice(self, event: AstrMessageEvent):
        """进群申请 / 进群 / 退群事件"""
        if result := await self.join.event_monitoring(event):
            yield event.plain_result(result)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_album_keyword(self, event: AstrMessageEvent):
        """关键词随机图：命中相册名时随机发一张留档图片"""
        if path := await self.album.random_keyword(event):
            yield event.chain_result([Comp.Image.fromFileSystem(str(path))])

    # ================================================================ LLM 工具
    #
    # 全部受 enable_llm_tools 总开关控制；need_auth=True 时按发起者权限鉴权，
    # 踢人 / 拉黑这类不可逆操作强制鉴权，避免被提示词绕过。

    @filter.llm_tool()
    async def llm_set_group_ban(
        self,
        event: AstrMessageEvent,
        user_id: int,
        duration: int,
        need_auth: bool = True,
    ):
        """
        在群聊中禁言某用户，被禁言的用户在禁言期间无法发言。
        Args:
            user_id(number): 要禁言的用户QQ号
            duration(number): 禁言秒数，0 表示解除禁言
            need_auth(boolean): 是否鉴权，机器人自行发起填 False，代当前用户发起填 True
        """
        if not self.cfg.enable_llm_tools:
            return
        if reason := await self.permissions.check_llm(
            event, "set_group_ban", bot_perm=PermLevel.ADMIN, need_auth=need_auth
        ):
            yield reason
            return
        if result := await self.moderation.ban(event, ban_time=duration, target_id=user_id):
            yield result

    @filter.llm_tool()
    async def llm_set_group_card(
        self,
        event: AstrMessageEvent,
        user_id: int,
        card: str,
        need_auth: bool = True,
    ):
        """
        修改群聊中某用户的群名片。
        Args:
            user_id(number): 要改名的用户QQ号
            card(string): 新的群名片，留空表示恢复原昵称
            need_auth(boolean): 是否鉴权，机器人自行发起填 False，代当前用户发起填 True
        """
        if not self.cfg.enable_llm_tools:
            return
        if reason := await self.permissions.check_llm(
            event, "set_group_card", bot_perm=PermLevel.ADMIN, need_auth=need_auth
        ):
            yield reason
            return
        if result := await self.moderation.set_card(event, target_id=user_id, target_card=card):
            yield result

    @filter.llm_tool()
    async def llm_set_group_special_title(
        self,
        event: AstrMessageEvent,
        user_id: int,
        special_title: str,
        need_auth: bool = True,
    ):
        """
        给群聊中某用户设置专属头衔，需要机器人是群主。
        Args:
            user_id(number): 要设置头衔的用户QQ号
            special_title(string): 新头衔，留空表示清除头衔
            need_auth(boolean): 是否鉴权，机器人自行发起填 False，代当前用户发起填 True
        """
        if not self.cfg.enable_llm_tools:
            return
        if reason := await self.permissions.check_llm(
            event, "set_group_special_title", bot_perm=PermLevel.OWNER, need_auth=need_auth
        ):
            yield reason
            return
        if result := await self.moderation.set_title(
            event, target_id=user_id, special_title=special_title
        ):
            yield result

    @filter.llm_tool()
    async def llm_set_group_whole_ban(
        self,
        event: AstrMessageEvent,
        enable: bool,
        need_auth: bool = True,
    ):
        """
        开启或关闭全员禁言。
        Args:
            enable(boolean): True 开启全员禁言，False 解除全员禁言
            need_auth(boolean): 是否鉴权，机器人自行发起填 False，代当前用户发起填 True
        """
        if not self.cfg.enable_llm_tools:
            return
        if reason := await self.permissions.check_llm(
            event, "whole_ban", bot_perm=PermLevel.ADMIN, need_auth=need_auth
        ):
            yield reason
            return
        if result := await self.moderation.whole_ban(event, bool(enable)):
            yield result

    @filter.llm_tool()
    async def llm_set_group_kick(self, event: AstrMessageEvent, user_id: int):
        """
        把某用户移出群聊。这是不可逆操作，始终按发起者权限鉴权。
        Args:
            user_id(number): 要移出群聊的用户QQ号
        """
        if not self.cfg.enable_llm_tools:
            return
        if reason := await self.permissions.check_llm(
            event, "set_group_kick", bot_perm=PermLevel.ADMIN, need_auth=True
        ):
            yield reason
            return
        if result := await self.moderation.kick(event, target_id=user_id):
            yield result

    @filter.llm_tool()
    async def llm_set_group_block(self, event: AstrMessageEvent, user_id: int):
        """
        把某用户移出群聊并加入群黑名单。这是不可逆操作，始终按发起者权限鉴权。
        Args:
            user_id(number): 要拉黑的用户QQ号
        """
        if not self.cfg.enable_llm_tools:
            return
        if reason := await self.permissions.check_llm(
            event, "set_group_block", bot_perm=PermLevel.ADMIN, need_auth=True
        ):
            yield reason
            return
        if result := await self.moderation.block(event, target_id=user_id):
            yield result

    @filter.llm_tool()
    async def llm_set_essence_msg(
        self,
        event: AstrMessageEvent,
        message_id: int,
        enable: bool = True,
        need_auth: bool = True,
    ):
        """
        把一条群消息设为精华或移出精华。
        Args:
            message_id(number): 目标消息的消息ID
            enable(boolean): True 设为精华，False 移出精华
            need_auth(boolean): 是否鉴权，机器人自行发起填 False，代当前用户发起填 True
        """
        if not self.cfg.enable_llm_tools:
            return
        if reason := await self.permissions.check_llm(
            event, "essence", bot_perm=PermLevel.ADMIN, need_auth=need_auth
        ):
            yield reason
            return
        if result := await self.essence.set_essence(event, bool(enable), message_id=message_id):
            yield result

    @filter.llm_tool()
    async def llm_get_essence_msg_list(self, event: AstrMessageEvent, need_auth: bool = True):
        """
        获取当前群聊的精华消息列表。
        Args:
            need_auth(boolean): 是否鉴权，机器人自行发起填 False，代当前用户发起填 True
        """
        if not self.cfg.enable_llm_tools:
            return
        if reason := await self.permissions.check_llm(
            event, "get_essence_msg_list", bot_perm=PermLevel.ADMIN, need_auth=need_auth
        ):
            yield reason
            return
        if result := await self.essence.list_essence(event):
            yield result

    @filter.llm_tool()
    async def llm_set_group_name(
        self, event: AstrMessageEvent, group_name: str, need_auth: bool = True
    ):
        """
        修改当前群聊的群名称。
        Args:
            group_name(string): 新的群名称
            need_auth(boolean): 是否鉴权，机器人自行发起填 False，代当前用户发起填 True
        """
        if not self.cfg.enable_llm_tools:
            return
        if reason := await self.permissions.check_llm(
            event, "set_group_name", bot_perm=PermLevel.ADMIN, need_auth=need_auth
        ):
            yield reason
            return
        if result := await self.moderation.set_group_name(event, group_name):
            yield result

    @filter.llm_tool()
    async def llm_set_group_portrait(
        self, event: AstrMessageEvent, image_url: str = "", need_auth: bool = True
    ):
        """
        修改当前群聊的群头像。
        Args:
            image_url(string): 新头像的图片直链，留空则取用户消息里引用的图片
            need_auth(boolean): 是否鉴权，机器人自行发起填 False，代当前用户发起填 True
        """
        if not self.cfg.enable_llm_tools:
            return
        if reason := await self.permissions.check_llm(
            event, "set_group_portrait", bot_perm=PermLevel.ADMIN, need_auth=need_auth
        ):
            yield reason
            return
        if result := await self.moderation.set_portrait(event, image_url or None):
            yield result

    @filter.llm_tool()
    async def llm_send_group_notice(
        self, event: AstrMessageEvent, content: str, need_auth: bool = True
    ):
        """
        在当前群聊发布一条群公告。
        Args:
            content(string): 群公告正文
            need_auth(boolean): 是否鉴权，机器人自行发起填 False，代当前用户发起填 True
        """
        if not self.cfg.enable_llm_tools:
            return
        if reason := await self.permissions.check_llm(
            event, "send_group_notice", bot_perm=PermLevel.ADMIN, need_auth=need_auth
        ):
            yield reason
            return
        if result := await self.notice.publish(event, content):
            yield result

    @filter.llm_tool()
    async def llm_get_group_notice(self, event: AstrMessageEvent, need_auth: bool = True):
        """
        获取当前群聊已发布的群公告。
        Args:
            need_auth(boolean): 是否鉴权，机器人自行发起填 False，代当前用户发起填 True
        """
        if not self.cfg.enable_llm_tools:
            return
        if reason := await self.permissions.check_llm(
            event, "get_group_notice", bot_perm=PermLevel.MEMBER, need_auth=need_auth
        ):
            yield reason
            return
        if result := await self.notice.view(event):
            yield result

    @filter.llm_tool()
    async def llm_upload_group_file(
        self, event: AstrMessageEvent, path: str, need_auth: bool = True
    ):
        """
        把用户引用的文件上传到群文件的指定位置。
        Args:
            path(string): 目标路径，格式为「文件夹名/文件名」或「文件名」
            need_auth(boolean): 是否鉴权，机器人自行发起填 False，代当前用户发起填 True
        """
        if not self.cfg.enable_llm_tools:
            return
        if reason := await self.permissions.check_llm(
            event, "upload_group_file", bot_perm=PermLevel.MEMBER, need_auth=need_auth
        ):
            yield reason
            return
        if result := await self.files.upload(event, path):
            yield result

    @filter.llm_tool()
    async def llm_delete_group_file(
        self, event: AstrMessageEvent, path: str, need_auth: bool = True
    ):
        """
        删除群文件里的某个文件。
        Args:
            path(string): 目标路径，格式为「文件夹名/文件名」或「文件名」
            need_auth(boolean): 是否鉴权，机器人自行发起填 False，代当前用户发起填 True
        """
        if not self.cfg.enable_llm_tools:
            return
        if reason := await self.permissions.check_llm(
            event, "delete_group_file", bot_perm=PermLevel.ADMIN, need_auth=need_auth
        ):
            yield reason
            return
        if result := await self.files.delete(event, path):
            yield result

    @filter.llm_tool()
    async def llm_view_group_file(
        self, event: AstrMessageEvent, path: str = "", need_auth: bool = True
    ):
        """
        浏览群文件目录或查看某个群文件的详情。
        Args:
            path(string): 目标路径，留空表示根目录
            need_auth(boolean): 是否鉴权，机器人自行发起填 False，代当前用户发起填 True
        """
        if not self.cfg.enable_llm_tools:
            return
        if reason := await self.permissions.check_llm(
            event, "view_group_file", bot_perm=PermLevel.MEMBER, need_auth=need_auth
        ):
            yield reason
            return
        if result := await self.files.view(event, path):
            yield result

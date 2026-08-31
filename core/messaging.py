"""长列表的输出通道。

群务里有不少"天生很长"的输出（审计日志、禁词表、群友清理预览、待审进群、
禁言列表……）。直接发纯文本会刷屏，全渲染成图片又不能复制。这里实现合并转发
通道：把长文本切成若干节点，用一条合并转发消息发出去，点开才展开。

协议端差异集中在 core.protocol.call_action 里处理，本模块只负责组装节点。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .config import DISPLAY_NAME, LOG_TAG
from .protocol import call_action

#: 发送合并转发的动作名候选（群聊专用接口优先）
FORWARD_ACTIONS: tuple[str, ...] = ("send_group_forward_msg", "send_forward_msg")

#: 单个转发节点默认最多放多少行
DEFAULT_NODE_LINES = 15

#: 合并转发最多多少个节点，超出的丢弃并提示（协议端对节点数也有上限）
MAX_NODES = 40


def chunk(lines: Sequence[str], per_node: int = DEFAULT_NODE_LINES) -> list[str]:
    """把行列表按 per_node 行一组切成若干文本块。"""
    per_node = max(1, per_node)
    blocks: list[str] = []
    for start in range(0, len(lines), per_node):
        block = "\n".join(str(line) for line in lines[start : start + per_node]).strip()
        if block:
            blocks.append(block)
    return blocks


def split_text(text: str, per_node: int = DEFAULT_NODE_LINES) -> list[str]:
    """把一段长文本按行切块；开头的标题行单独成块，方便一眼看到概要。"""
    lines = [line for line in text.splitlines()]
    if not lines:
        return []
    head, rest = lines[0], lines[1:]
    blocks = [head.strip()] if head.strip() else []
    blocks.extend(chunk(rest, per_node))
    return blocks or [text.strip()]


def build_nodes(uin: str, name: str, blocks: Sequence[str]) -> list[dict[str, Any]]:
    """组装 OneBot 自定义转发节点。

    各协议端读的字段名不完全一致（uin/user_id、name/nickname），一并给出，
    多余字段会被忽略。
    """
    nodes: list[dict[str, Any]] = []
    for block in blocks[:MAX_NODES]:
        nodes.append(
            {
                "type": "node",
                "data": {
                    "uin": str(uin),
                    "user_id": str(uin),
                    "name": name,
                    "nickname": name,
                    "content": [{"type": "text", "data": {"text": block}}],
                },
            }
        )
    return nodes


async def send_forward(
    event: AstrMessageEvent,
    blocks: Sequence[str],
    *,
    name: str = "",
    summary: str = "",
) -> bool:
    """把若干文本块作为一条合并转发发到当前群，失败返回 False 交由调用方回退。"""
    group_id = event.get_group_id()
    if not group_id or not blocks:
        return False

    try:
        self_id = str(event.get_self_id())
    except Exception:  # noqa: BLE001 - 少数适配器没有实现
        self_id = ""
    nodes = build_nodes(self_id or "10000", name or DISPLAY_NAME, blocks)
    if not nodes:
        return False

    result = await call_action(
        event,
        FORWARD_ACTIONS,
        group_id=int(group_id),
        messages=nodes,
        summary=summary or None,
    )
    if result.ok:
        return True
    logger.debug(f"{LOG_TAG} 合并转发发送失败，回退其他形式：{result.error}")
    return False

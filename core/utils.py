"""通用工具函数：消息解析、类型转换、文案格式化、文件下载。"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import aiohttp
from astrbot.api import logger
from astrbot.core.message.components import At, Image, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent

_TRUE_WORDS = frozenset({"开", "开启", "启用", "打开", "on", "true", "yes", "1", "是", "真"})
_FALSE_WORDS = frozenset({"关", "关闭", "禁用", "off", "false", "no", "0", "否", "假"})
_ILLEGAL_PATH_CHARS = frozenset('\\/:*?"<>|')


# --------------------------------------------------------------------------- #
# 类型与文案                                                                    #
# --------------------------------------------------------------------------- #
def parse_bool(value: Any, default: bool | None = None) -> bool | None:
    """把用户输入解析成布尔值。

    无法判定时返回 *default*（默认 None），调用方据此区分"设置"与"查看"。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return default


def parse_int(value: Any, default: int | None = None) -> int | None:
    """宽松地把用户输入解析成整数，失败返回 *default*。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if value is None:
        return default
    text = str(value).strip()
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    return default


def switch_text(value: Any) -> str:
    """布尔值转「开 / 关」，其它类型原样字符串化。"""
    if isinstance(value, bool):
        return "开" if value else "关"
    return str(value)


def list_text(value: Any, empty: str = "（空）") -> str:
    """列表转顿号分隔的可读文本。"""
    if not value:
        return empty
    if isinstance(value, (list, tuple, set)):
        return "、".join(str(item) for item in value)
    return str(value)


def timestamp_seconds(value: Any, default: int | None = None) -> int | None:
    """把秒、毫秒、微秒或纳秒时间戳统一成 Unix 秒。"""
    parsed = parse_int(value, default)
    if parsed is None:
        return default
    # 当前 Unix 秒约为 1e9；协议端偶尔会把同一字段序列化成更高精度的
    # 时间戳。按数量级逐级缩放，避免把毫秒时间显示成 51382 年。
    result = parsed
    for _ in range(3):
        if abs(result) < 100_000_000_000:
            break
        result //= 1000
    return result


def format_date(timestamp: Any) -> str:
    """时间戳转 YYYY-MM-DD。"""
    try:
        value = timestamp_seconds(timestamp)
        if value is None:
            return "未知"
        return time.strftime("%Y-%m-%d", time.localtime(value))
    except (TypeError, ValueError, OSError):
        return "未知"


def format_datetime(timestamp: Any) -> str:
    """时间戳转 YYYY-MM-DD HH:MM:SS。"""
    try:
        value = timestamp_seconds(timestamp)
        if value is None:
            return "未知"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))
    except (TypeError, ValueError, OSError):
        return "未知"


def format_duration(seconds: Any, units: int = 0) -> str:
    """秒数转人类可读时长，例如 3720 -> 1小时2分钟。

    units 大于 0 时只保留最高的几段：列表里「2天23小时」比精确到秒好读。
    """
    total = parse_int(seconds, 0) or 0
    if total <= 0:
        return "0秒"
    parts: list[str] = []
    for unit_seconds, unit_name in ((86400, "天"), (3600, "小时"), (60, "分钟"), (1, "秒")):
        value, total = divmod(total, unit_seconds)
        if value:
            parts.append(f"{value}{unit_name}")
        if units > 0 and len(parts) >= units:
            break
    return "".join(parts)


def format_size(num_bytes: Any) -> str:
    """字节数转 KB/MB/GB。"""
    size = float(parse_int(num_bytes, 0) or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.2f} GB"


def md_cell(value: Any, limit: int = 0) -> str:
    """把任意值洗成能安全放进 Markdown 表格的单元格文本。

    竖线会把一格切成两格、换行会把一行切成两行，都得先换掉；limit 大于 0 时截断。
    """
    text = " ".join(str(value if value not in (None, "") else "-").split()).replace("|", "/")
    if limit > 0 and len(text) > limit:
        text = text[: max(1, limit - 1)] + "…"
    return text


def sanitize_filename(name: str, fallback: str = "default") -> str:
    """清理文件名中的非法字符。"""
    if not name or not name.strip():
        return fallback
    cleaned = "".join(
        "_" if char in _ILLEGAL_PATH_CHARS or ord(char) < 32 else char for char in name
    ).strip()
    return cleaned or fallback


def parse_time_range(raw: Any, fallback: tuple[int, int] = (30, 300)) -> tuple[int, int]:
    """解析「最小~最大」形式的秒数区间，非法时返回 *fallback*。"""
    text = str(raw or "").strip().replace("～", "~").replace("-", "~")
    match = re.fullmatch(r"(\d+)\s*~\s*(\d+)", text)
    if not match:
        return fallback
    low, high = int(match.group(1)), int(match.group(2))
    if low > high:
        low, high = high, low
    low = max(1, min(low, 2592000))
    high = max(1, min(high, 2592000))
    return low, high


def split_tokens(raw: str) -> list[str]:
    """按空白切分参数，兼容全角空格。"""
    return [token for token in re.split(r"[\s\u3000]+", (raw or "").strip()) if token]


def apply_delta(current: list[str], tokens: list[str]) -> tuple[list[str], list[str], list[str]]:
    """按 +词 / -词 增删列表，返回 (新列表, 新增, 移除)。

    若所有 token 都不带 +/-，则视为整表覆写。
    """
    if tokens and all(not token.startswith(("+", "-")) for token in tokens):
        return list(dict.fromkeys(tokens)), [], []

    result = list(dict.fromkeys(str(item) for item in current))
    added: list[str] = []
    removed: list[str] = []
    for token in tokens:
        word = token[1:].strip()
        if not word:
            continue
        if token.startswith("+"):
            if word not in result:
                result.append(word)
                added.append(word)
        elif token.startswith("-") and word in result:
            result.remove(word)
            removed.append(word)
    return result, added, removed


# --------------------------------------------------------------------------- #
# 消息解析                                                                      #
# --------------------------------------------------------------------------- #
def get_reply_segment(event: AstrMessageEvent) -> Reply | None:
    """取出消息里的引用段（若有）。"""
    for segment in event.get_messages() or []:
        if isinstance(segment, Reply):
            return segment
    return None


def get_replyer_id(event: AstrMessageEvent) -> str | None:
    """被引用消息的发送者 QQ 号。"""
    reply = get_reply_segment(event)
    sender_id = getattr(reply, "sender_id", None) if reply else None
    return str(sender_id) if sender_id else None


def get_reply_message_id(event: AstrMessageEvent) -> str | None:
    """被引用消息的消息 ID。"""
    reply = get_reply_segment(event)
    if not reply:
        return None
    message_id = getattr(reply, "id", None) or getattr(reply, "message_id", None)
    return str(message_id) if message_id else None


def get_reply_text(event: AstrMessageEvent) -> str:
    """把被引用消息拼成纯文本。"""
    reply = get_reply_segment(event)
    if not reply or not reply.chain:
        return ""
    chunks: list[str] = []
    for segment in reply.chain:
        if isinstance(segment, Plain):
            chunks.append(segment.text)
        elif isinstance(segment, At):
            chunks.append(f"@{getattr(segment, 'name', '') or segment.qq} ")
        else:
            name = getattr(segment, "name", None)
            if name:
                chunks.append(f"[文件: {name}]")
    return "".join(chunks)


def get_ats(event: AstrMessageEvent) -> list[str]:
    """取出消息中 @ 到的 QQ 号（排除机器人自己）。"""
    self_id = str(event.get_self_id())
    targets: list[str] = []
    for segment in event.get_messages() or []:
        if isinstance(segment, At):
            qq = str(segment.qq)
            if qq != self_id and qq not in targets:
                targets.append(qq)
    return targets


def extract_image_url(event: AstrMessageEvent) -> str | None:
    """从当前消息或引用消息中取第一张图片的地址。"""
    chains: list[Any] = [event.get_messages() or []]
    reply = get_reply_segment(event)
    if reply and reply.chain:
        chains.insert(0, reply.chain)
    for chain in chains:
        for segment in chain:
            if isinstance(segment, Image):
                # AstrBot's Image.fromFileSystem keeps the real path in
                # ``path`` but exposes a file:/// URI in ``file``.  Prefer the
                # path so Windows drive letters and spaces are not lost.
                for value in (
                    getattr(segment, "path", None),
                    getattr(segment, "url", None),
                    getattr(segment, "file", None),
                ):
                    if value:
                        return str(value)
    return None


async def get_nickname(event: AstrMessageEvent, user_id: str | int) -> str:
    """获取群名片 / 昵称，失败时回落到 QQ 号。"""
    uid = str(user_id)
    client = getattr(event, "bot", None)
    if client is None or not uid.isdigit():
        return uid
    group_id = event.get_group_id()
    if group_id:
        try:
            info = await client.get_group_member_info(
                group_id=int(group_id), user_id=int(uid), no_cache=False
            )
            name = info.get("card") or info.get("nickname")
            if name:
                return str(name)
        except Exception:  # noqa: BLE001 - 群资料拿不到就降级
            pass
    try:
        info = await client.get_stranger_info(user_id=int(uid))
        name = info.get("nickname") or info.get("nick")
        if name:
            return str(name)
    except Exception:  # noqa: BLE001
        pass
    return uid


# --------------------------------------------------------------------------- #
# 网络                                                                          #
# --------------------------------------------------------------------------- #
async def download_bytes(url: str, timeout: int = 30) -> bytes | None:
    """下载 URL 内容，也兼容 ``file://`` 指向的本地文件。"""
    if str(url).lower().startswith("file:"):
        path = _local_path_from_source(str(url))
        try:
            return path.read_bytes() if path.is_file() else None
        except OSError as exc:
            logger.warning(f"[群务管家] 读取本地文件失败 {url}: {exc}")
            return None
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with (
            aiohttp.ClientSession(timeout=client_timeout) as session,
            session.get(url) as response,
        ):
            response.raise_for_status()
            return await response.read()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[群务管家] 下载失败 {url}: {exc}")
        return None


async def download_file(url: str, save_path: Path, timeout: int = 60) -> Path | None:
    """下载文件到本地，成功返回路径。"""
    data = await download_bytes(url, timeout=timeout)
    if data is None:
        return None
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(data)
    return save_path


async def load_bytes(source: str) -> bytes | None:
    """把本地路径 / file URI / URL / base64 统一读成 bytes。"""
    if not source:
        return None
    source = str(source).strip()
    lowered = source.lower()
    if lowered.startswith("base64://"):
        try:
            encoded = "".join(source[9:].split())
            return base64.b64decode(encoded, validate=True)
        except Exception:  # noqa: BLE001
            return None

    if lowered.startswith("data:"):
        try:
            header, encoded = source.split(",", 1)
            if ";base64" in header.lower():
                return base64.b64decode("".join(encoded.split()), validate=True)
            from urllib.parse import unquote as unquote_data

            return unquote_data(encoded).encode("utf-8")
        except Exception:  # noqa: BLE001
            return None

    path = _local_path_from_source(source)
    try:
        if path.is_file():
            return path.read_bytes()
    except OSError:
        pass
    if lowered.startswith(("http://", "https://")):
        return await download_bytes(source)
    return None


def _local_path_from_source(source: str) -> Path:
    """将本地路径或 file:// URI 转成 Path；远程 URL 返回一个无效 Path。"""
    if not source.lower().startswith("file:"):
        return Path(source)

    parsed = urlsplit(source)
    path = unquote(parsed.path or "")
    netloc = unquote(parsed.netloc or "")
    if netloc and netloc.lower() != "localhost":
        # UNC path: file://server/share/file
        path = f"//{netloc}{path}"
    elif netloc and len(netloc) == 2 and netloc[1] == ":":
        # Non-standard but common file://C:/path form.
        path = f"{netloc}{path}"

    # urlsplit gives file:///C:/... as /C:/... on Windows.
    if os.name == "nt" and re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return Path(path)


async def gather_limited(coros: list[Any], limit: int = 10) -> list[Any]:
    """带并发上限地并行执行协程，异常以返回值形式给出。"""
    semaphore = asyncio.Semaphore(max(1, limit))

    async def _run(coro: Any) -> Any:
        async with semaphore:
            return await coro

    return await asyncio.gather(*(_run(coro) for coro in coros), return_exceptions=True)

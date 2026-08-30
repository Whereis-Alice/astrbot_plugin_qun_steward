"""字体解析：按优先级找一款可用的中文字体，避免强制联网下载。

优先级：
1. 配置里显式指定的自定义字体
2. 数据目录下已放好的 NotoSansSC-{Regular,Bold}
3. 系统自带中文字体
4. 仅当开启 fonts.auto_download 时才按 manifest 下载
5. 兜底 Pillow 内置位图字体
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

from astrbot.api import logger
from PIL import ImageFont

from ...core.config import LOG_TAG, StewardConfig
from ...core.utils import download_bytes
from .emoji_text import SYSTEM_EMOJI_FONTS, EmojiFont

#: 数据目录 / 系统目录里会去找的字体文件名
_NOTO_NAMES = {
    False: ("NotoSansSC-Regular.ttf", "NotoSansSC-Regular.otf"),
    True: ("NotoSansSC-Bold.ttf", "NotoSansSC-Bold.otf"),
}

#: 各平台常见的中文字体，按可读性排序
_SYSTEM_FONTS: dict[str, tuple[str, ...]] = {
    "win32": (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ),
    "darwin": (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
    ),
    "linux": (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ),
}


#: 数据目录里会去找的彩色 emoji 字体文件名
_EMOJI_NAMES: tuple[str, ...] = (
    "seguiemj.ttf",
    "NotoColorEmoji.ttf",
    "AppleColorEmoji.ttf",
    "OpenMoji-color-glyf_colr_0.ttf",
)


def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


class FontResolver:
    """按优先级解析字体路径，并缓存已加载的字体对象。"""

    def __init__(self, config: StewardConfig) -> None:
        self.config = config
        self._cache: dict[tuple[int, bool], Any] = {}
        self._resolved: dict[bool, Path | None] = {}
        self._emoji: EmojiFont | None = None
        self._warned = False

    # ------------------------------------------------------------------ 查找

    def _candidates(self, bold: bool) -> list[Path]:
        fonts = self.config.fonts
        paths: list[Path] = []

        custom = fonts.str("custom_font_bold_path" if bold else "custom_font_path", "")
        if custom:
            paths.append(Path(custom))
        # 粗体缺失时允许退回常规体，反之亦然
        fallback = fonts.str("custom_font_path" if bold else "custom_font_bold_path", "")
        if fallback:
            paths.append(Path(fallback))

        font_dir = self.config.font_dir
        for name in _NOTO_NAMES[bold]:
            paths.append(font_dir / name)
        for name in _NOTO_NAMES[not bold]:
            paths.append(font_dir / name)

        paths.extend(Path(p) for p in _SYSTEM_FONTS[_platform_key()])
        return paths

    def resolve(self, bold: bool = False) -> Path | None:
        """返回第一个真实存在的字体文件，找不到返回 None。"""
        if bold in self._resolved:
            return self._resolved[bold]
        found: Path | None = None
        seen: set[str] = set()
        for path in self._candidates(bold):
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            try:
                if path.is_file():
                    found = path
                    break
            except OSError:
                continue
        self._resolved[bold] = found
        return found

    def resolve_emoji(self) -> Path | None:
        """找一款彩色 emoji 字体：自定义路径 → 数据目录 → 系统自带。"""
        candidates: list[Path] = []
        custom = self.config.fonts.str("custom_emoji_font_path", "")
        if custom:
            candidates.append(Path(custom))
        candidates.extend(self.config.font_dir / name for name in _EMOJI_NAMES)
        candidates.extend(Path(p) for p in SYSTEM_EMOJI_FONTS)
        for path in candidates:
            try:
                if path.is_file():
                    return path
            except OSError:
                continue
        return None

    def emoji_font(self) -> EmojiFont:
        """返回缓存的彩色 emoji 字体包装对象（找不到时 available 为 False）。"""
        if self._emoji is None:
            path = self.resolve_emoji()
            if path is None:
                logger.debug(f"{LOG_TAG} 未找到彩色 emoji 字体，emoji 将按正文字体绘制")
            self._emoji = EmojiFont(path)
        return self._emoji

    def invalidate(self) -> None:
        """字体文件变动（例如刚下载完）后清缓存。"""
        self._cache.clear()
        self._resolved.clear()
        self._emoji = None

    # ------------------------------------------------------------------ 加载

    def load(self, size: int, bold: bool = False) -> Any:
        """加载字体；全部失败时返回 Pillow 内置字体，保证不抛异常。"""
        key = (size, bold)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        path = self.resolve(bold)
        font: Any = None
        if path is not None:
            try:
                font = ImageFont.truetype(str(path), size)
            except Exception as exc:  # noqa: BLE001 - 坏字体文件不应让功能整体失效
                logger.warning(f"{LOG_TAG} 字体加载失败 {path}: {exc}")

        if font is None:
            if not self._warned:
                self._warned = True
                logger.warning(
                    f"{LOG_TAG} 未找到可用中文字体，图片将使用内置字体（中文可能显示为方块）。"
                    f"可在配置里指定 custom_font_path，或把 NotoSansSC-Regular.ttf 放到 {self.config.font_dir}"
                )
            font = ImageFont.load_default()

        self._cache[key] = font
        return font

    # ------------------------------------------------------------------ 下载

    async def ensure_downloaded(self) -> bool:
        """开启 auto_download 且本地无字体时，按 manifest 下载一次。"""
        if self.resolve(False) is not None:
            return True
        if not self.config.fonts.bool("auto_download", False):
            return False

        manifest_url = self.config.fonts.str("download_manifest", "")
        if not manifest_url:
            logger.warning(f"{LOG_TAG} 已开启字体自动下载，但未配置 download_manifest")
            return False

        try:
            ok = await self._download_from_manifest(manifest_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 字体自动下载失败: {exc}")
            return False
        if ok:
            self.invalidate()
        return ok

    async def _download_from_manifest(self, manifest_url: str) -> bool:
        import json

        raw = await download_bytes(manifest_url, timeout=30)
        if not raw:
            return False
        manifest = json.loads(raw.decode("utf-8"))
        if int(manifest.get("schema_version", 0)) != 1:
            logger.warning(f"{LOG_TAG} 字体 manifest 版本不支持: {manifest.get('schema_version')}")
            return False

        target_dir = self.config.font_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        for entry in manifest.get("fonts", []) or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            url = str(entry.get("url") or "")
            digest = str(entry.get("sha256") or "")
            size = int(entry.get("size") or 0)
            if not name or not url or not digest or size <= 0:
                logger.warning(f"{LOG_TAG} 跳过字体条目（字段不完整）: {entry}")
                continue
            if (target_dir / name).is_file():
                downloaded += 1
                continue
            if await self._download_one(target_dir, name, url, digest, size):
                downloaded += 1
        return downloaded > 0

    async def _download_one(
        self, target_dir: Path, name: str, url: str, digest: str, size: int
    ) -> bool:
        """下载到临时文件，校验大小与 sha256 后再原子替换。"""
        data = await download_bytes(url, timeout=120)
        if not data:
            logger.warning(f"{LOG_TAG} 字体下载失败: {name}")
            return False
        if len(data) != size:
            logger.warning(f"{LOG_TAG} 字体大小不符 {name}: 期望 {size}，实际 {len(data)}")
            return False
        actual = hashlib.sha256(data).hexdigest()
        if actual.lower() != digest.lower():
            logger.warning(f"{LOG_TAG} 字体校验失败 {name}: sha256 不匹配")
            return False

        tmp = target_dir / (name + ".download")
        try:
            tmp.write_bytes(data)
            tmp.replace(target_dir / name)
        except OSError as exc:
            logger.warning(f"{LOG_TAG} 字体写入失败 {name}: {exc}")
            tmp.unlink(missing_ok=True)
            return False
        logger.info(f"{LOG_TAG} 字体已下载: {name}")
        return True

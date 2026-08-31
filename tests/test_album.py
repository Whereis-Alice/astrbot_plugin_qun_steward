"""群相册指令的参数解析与上传回复文案。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_qun_steward.features.album import service
from astrbot_plugin_qun_steward.features.album.service import AlbumFeature


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # 只有指令本身：相册和条数都交给配置/默认值决定
        ("上传群相册", ("", None)),
        ("上传群相册   ", ("", None)),
        # 只给相册名
        ("上传群相册 日常", ("日常", None)),
        # 只给数字，视为不指定相册
        ("上传群相册 10", ("", 10)),
        # 相册名 + 条数
        ("上传群相册 日常 10", ("日常", 10)),
        # 相册名里带空格
        ("上传群相册 群友 怪话 20", ("群友 怪话", 20)),
        ("上传群相册 群友 怪话", ("群友 怪话", None)),
        # 结尾不是纯数字就整段当相册名
        ("上传群相册 日常 十张", ("日常 十张", None)),
        ("上传群相册 2026年", ("2026年", None)),
    ],
)
def test_parse_args(message: str, expected: tuple[str, int | None]) -> None:
    assert AlbumFeature.parse_args(message) == expected


def test_parse_args_handles_empty_input() -> None:
    assert AlbumFeature.parse_args("") == ("", None)


class _FlowAlbum(AlbumFeature):
    """只保留 _upload_flow 需要的最小依赖，用来校验上传成功后的回复文案。"""

    def __init__(self, tmp_dir: Path) -> None:
        # 故意不调父类 __init__：这里只需要 _upload_flow 用到的那几个属性
        self._default_cache = {}
        self._config = SimpleNamespace(album_dir=tmp_dir)
        self.actions: list[str] = []

    @property
    def config(self) -> SimpleNamespace:
        return self._config

    @property
    def backup_enabled(self) -> bool:
        return False

    def _configured_album(self, group_id: str) -> str:
        return "金句"

    async def _build_image(self, event: object, count: int | None) -> bytes:
        return b"\x89PNG\r\n\x1a\n"

    async def _upload_with_retry(self, *args: object, **kwargs: object) -> str:
        return "napcat"

    async def log(self, event: object, action: str, **kwargs: object) -> None:
        self.actions.append(action)


@pytest.mark.asyncio
async def test_upload_is_silent_on_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """上传成功时 QQ 自带相册卡片，插件应当静默（返回空串）。"""

    async def fake_find_album(event: object, group_id: object, name: str) -> dict[str, str]:
        return {"album_id": "a1", "album_name": name}

    monkeypatch.setattr(service, "find_album", fake_find_album)
    feature = _FlowAlbum(tmp_path)

    reply = await feature._upload_flow(object(), "12345", "", None)

    assert reply == ""
    assert feature.actions == ["album_upload"]


@pytest.mark.asyncio
async def test_upload_reports_when_album_was_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """相册是插件顺手新建的，客户端不会提示，这一条要说出来。"""

    async def fake_find_album(event: object, group_id: object, name: str) -> None:
        return None

    async def fake_create_album(event: object, group_id: object, name: str) -> dict[str, str]:
        return {"album_id": "a2", "album_name": name}

    monkeypatch.setattr(service, "find_album", fake_find_album)
    monkeypatch.setattr(service, "create_album", fake_create_album)
    feature = _FlowAlbum(tmp_path)

    reply = await feature._upload_flow(object(), "12345", "", None)

    assert reply == "已新建相册【金句】并上传成功。"

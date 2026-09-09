"""引用群文件下载的安全与异步解析测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot.core.message.components import File, Reply
from astrbot_plugin_qun_steward.features.base import FeatureContext
from astrbot_plugin_qun_steward.features.files import FilesFeature


def _feature(tmp_path: Path) -> FilesFeature:
    config = SimpleNamespace(file_dir=tmp_path)
    ctx = FeatureContext(
        context=None,  # type: ignore[arg-type]
        config=config,  # type: ignore[arg-type]
        store=None,  # type: ignore[arg-type]
        permissions=None,  # type: ignore[arg-type]
        audit=None,  # type: ignore[arg-type]
        undo=None,  # type: ignore[arg-type]
        groups=None,  # type: ignore[arg-type]
        db=None,  # type: ignore[arg-type]
        to_image=None,  # type: ignore[arg-type]
    )
    return FilesFeature(ctx)


class _Event:
    def __init__(self, segment: File) -> None:
        self.message_obj = SimpleNamespace(message=[Reply(id="r1", chain=[segment])])


@pytest.mark.asyncio
async def test_file_get_file_is_used_before_compatibility_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    segment = File("report.zip", file="internal-file-id", url="https://example.test/report.zip")
    calls: list[bool] = []

    async def get_file(_self: File, *, allow_return_url: bool = False) -> str:
        calls.append(allow_return_url)
        return "data:application/octet-stream;base64,SGVsbG8="

    monkeypatch.setattr(File, "get_file", get_file)
    feature = _feature(tmp_path)
    result = await feature._download_quoted(_Event(segment), "report.zip")
    assert result is not None
    assert calls == [True]
    assert result.read_bytes() == b"Hello"


@pytest.mark.asyncio
async def test_internal_file_id_is_not_used_as_local_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    segment = File("report.zip", file="internal-file-id")

    async def get_file(_self: File, *, allow_return_url: bool = False) -> str:
        return ""

    monkeypatch.setattr(File, "get_file", get_file)
    feature = _feature(tmp_path)
    result = await feature._download_quoted(_Event(segment), "report.zip")
    assert result is None

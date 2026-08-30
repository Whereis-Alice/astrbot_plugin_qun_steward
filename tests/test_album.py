"""群相册指令的参数解析。"""

from __future__ import annotations

import pytest
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

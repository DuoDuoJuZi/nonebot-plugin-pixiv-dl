from tempfile import TemporaryDirectory

import nonebot
import pytest

# @Author: DuoDuoJuZi
# @Date: 2026-09-29
with TemporaryDirectory(prefix="pixiv-tests-") as runtime_directory:
    nonebot.init(
        driver="~none",
        localstore_data_dir=f"{runtime_directory}/data",
        localstore_cache_dir=f"{runtime_directory}/cache",
    )
    assert nonebot.load_plugin("nonebot_plugin_pixiv_dl") is not None


@pytest.fixture(autouse=True)
def isolate_preferences(monkeypatch, tmp_path):
    """为每项测试提供独立的 LocalStore 数据目录与缓存目录

    Args:
        monkeypatch: 临时替换本地存储路径的 pytest 工具
        tmp_path: 当前测试独占的本地目录
    """
    from nonebot_plugin_pixiv_dl import commands
    from nonebot_plugin_pixiv_dl.preferences import PreferenceStore

    monkeypatch.setattr(commands.store, "BASE_DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(commands.store, "BASE_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(
        commands, "preferences",
        PreferenceStore(commands.store.get_data_dir("nonebot_plugin_pixiv_dl")),
    )
    monkeypatch.setattr(
        commands, "ugoira_cache_dir", commands.store.get_cache_dir("nonebot_plugin_pixiv_dl")
    )


@pytest.fixture(autouse=True)
def clear_search_sessions():
    """隔离每项测试的内存分页状态，测试结束后释放会话引用

    Yields:
        允许当前测试使用独立的分页记录集合
    """
    from nonebot_plugin_pixiv_dl.pagination import sessions

    sessions.clear()
    yield
    sessions.clear()

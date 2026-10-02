import nonebot
import pytest

# @Author: DuoDuoJuZi
# @Date: 2026-09-29
nonebot.init(driver="~none")


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

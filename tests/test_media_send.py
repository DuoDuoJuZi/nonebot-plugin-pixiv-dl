import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock

import httpcore
import httpx
import pytest
from nonebot.adapters.onebot.v11 import ActionFailed, Bot, NetworkError
from test_ugoira_commands import make_event

from nonebot_plugin_pixiv_dl import message, ugoira


# @Author: DuoDuoJuZi
# @Date: 2026-10-03
@pytest.mark.parametrize("kind", ["asyncio", "httpx", "httpcore", "cause", "context", "cycle"])
def test_timeout_exception_chain(kind: str) -> None:
    """识别底层及多层包装的超时，异常链循环不会阻塞

    Args:
        kind: 当前使用的超时类型或异常链包装方式
    """
    timeout = {
        "asyncio": asyncio.TimeoutError(),
        "httpx": httpx.ReadTimeout("回执超时"),
        "httpcore": httpcore.ReadTimeout("回执超时"),
    }.get(kind, httpx.ReadTimeout("回执超时"))
    error = timeout
    if kind in ("cause", "context", "cycle"):
        error = NetworkError("HTTP request failed")
        inner = RuntimeError("底层请求包装")
        if kind == "context":
            error.__context__ = inner
            inner.__context__ = timeout
        else:
            error.__cause__ = inner
            inner.__cause__ = timeout
        if kind == "cycle":
            timeout.__context__ = error
    assert message.is_timeout_error(error)
    normal = NetworkError("连接被拒绝")
    normal.__cause__ = normal
    assert not message.is_timeout_error(normal)


@pytest.mark.parametrize("failure", ["action", "retcode", "status", "wrapped", "network"])
def test_media_result_distinguishes_failure_from_timeout(failure: str) -> None:
    """明确失败优先于无关异常上下文，回执超时不会触发重试

    Args:
        failure: 明确失败响应或包装超时的模拟场景
    """
    operation = AsyncMock(return_value={"message_id": 1})
    if failure == "action":
        error = ActionFailed(retcode=1200, status="failed")
        error.__context__ = httpx.ReadTimeout("之前的请求超时")
        operation.side_effect = error
    elif failure in ("retcode", "status"):
        operation.return_value = {"retcode": 1200} if failure == "retcode" else {"status": "failed"}
    else:
        error = NetworkError("HTTP request failed")
        if failure == "wrapped":
            error.__cause__ = httpx.ReadTimeout("当前请求超时")
        operation.side_effect = error
    result = asyncio.run(message._send_media(operation(), 42, "测试资源"))
    assert result is (
        message.MediaSendResult.UNKNOWN if failure == "wrapped" else message.MediaSendResult.FAILED
    )
    operation.assert_awaited_once()


@pytest.mark.parametrize("group", [False, True])
def test_real_bot_video_send_forwards_per_call_timeout(tmp_path: Path, group: bool) -> None:
    """通过当前 OneBot Bot 实现验证视频消息的单次超时确实传入 API

    Args:
        tmp_path: 当前测试独占的临时目录
        group: 是否验证群聊事件的真实路由
    """
    adapter = Mock()
    adapter.get_name.return_value = "OneBot V11"
    adapter._call_api = AsyncMock(return_value={"message_id": 1})
    bot = Bot(adapter, "10")
    path = tmp_path / "42_ugoira.mp4"
    path.write_bytes(b"mp4")
    result = asyncio.run(message.send_ugoira_video(bot, make_event(group), path, 42))
    assert result is message.MediaSendResult.SUCCESS
    adapter._call_api.assert_awaited_once()
    args, params = adapter._call_api.call_args
    assert args == (bot, "send_msg")
    assert params["_timeout"] == 180
    assert params["message_type"] == ("group" if group else "private")
    assert params["message"][0].type == "video"
    assert params["message"][0].data["file"] == path.resolve().as_uri()


def test_grace_period_and_shutdown_cleanup(monkeypatch) -> None:
    """超时资源在宽限期内保持存在，到期或正常关闭后均回收

    Args:
        monkeypatch: 将资源宽限期缩短为可快速验证的测试时间
    """
    async def scenario():
        """先等待定时清理，再验证关闭时能够回收尚未到期的目录"""
        directory = TemporaryDirectory(prefix="pixiv-test-grace-")
        path = Path(directory.name) / "42_ugoira.mp4"
        path.write_bytes(b"mp4")
        await ugoira.release_directory(directory, True)
        assert path.exists()
        await asyncio.gather(*list(ugoira.cleanup_tasks))
        assert not path.parent.exists()
        assert not ugoira.cleanup_tasks
        pending = TemporaryDirectory(prefix="pixiv-test-shutdown-")
        folder = Path(pending.name)
        (folder / "42_ugoira.zip").write_bytes(b"zip")
        await ugoira.release_directory(pending, True)
        assert folder.exists()
        await ugoira.cleanup_pending_directories()
        assert not folder.exists()
        assert not ugoira.cleanup_tasks

    assert ugoira.MEDIA_FILE_GRACE == 300
    monkeypatch.setattr(ugoira, "MEDIA_FILE_GRACE", 0.01)
    asyncio.run(scenario())

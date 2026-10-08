import asyncio
import threading
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from nonebot.adapters.onebot.v11 import (
    ActionFailed,
    GroupMessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from PIL import Image
from test_pixiv import ajax
from test_preview import image_bytes, png, preview_url
from test_ugoira import make_archive

from nonebot_plugin_pixiv_dl import commands, message, ugoira
from nonebot_plugin_pixiv_dl.config import Config
from nonebot_plugin_pixiv_dl.pixiv import PixivClient


# @Author: DuoDuoJuZi
# @Date: 2026-10-03
def meta_body(meta) -> dict:
    """将合成媒体元数据还原为 Pixiv 响应字段

    Args:
        meta: 用于真实网络解析流程测试的合成动图元数据

    Returns:
        含有原始字段名称的动图接口业务对象
    """
    return {
        "src": meta.src,
        "originalSrc": meta.original_src,
        "mime_type": meta.mime_type,
        "frames": [{"file": frame.file, "delay": frame.delay} for frame in meta.frames],
    }


@pytest.mark.parametrize("kind", ["image", "all", "related"])
@pytest.mark.parametrize(
    "failure", [None, "timeout", "decode", "blur", "missing", "original", "later", "disabled"]
)
def test_search_ugoira_static_preview_and_safe_failure(
    monkeypatch, kind: str, failure: str | None
) -> None:
    """验证搜索和相关作品只取静态缩略图，失败时保留元数据与编号

    Args:
        monkeypatch: 替换网络和注入预览故障的 pytest 工具
        kind: 图片搜索，聚合搜索或相关作品入口
        failure: 注入的缩略图故障或不可用场景，空值表示正常预览
    """
    requests = []
    source = Image.new("RGB", (300, 160), "white")
    source.paste("black", (0, 0, 150, 160))
    static = png(source)
    blurred = message.process_preview(static, True, 512)
    plain = message.process_preview(static, False, 512)
    owner_thread = threading.get_ident()
    forbidden = [AsyncMock() for _ in range(3)]
    for name, operation in zip(
        ("get_ugoira_meta", "download_ugoira_archive", "convert_to_mp4"), forbidden, strict=True
    ):
        monkeypatch.setattr(ugoira if name == "convert_to_mp4" else PixivClient, name, operation)
    ffmpeg = Mock()
    monkeypatch.setattr(ugoira, "get_ffmpeg_exe", ffmpeg)

    def process(data: bytes, is_r18: bool, max_edge: int) -> bytes:
        """检查静态缩略图处理离开主事件循环

        Args:
            data: 待编码的低清缩略图内容
            is_r18: 是否必须模糊作品预览
            max_edge: 预览最长边的像素上限

        Returns:
            已缩放并按限制级标记处理的静态 JPEG 内容

        Raises:
            OSError: 注入的模糊故障或图片无法解码
        """
        assert threading.get_ident() != owner_thread
        return message.process_preview(data, is_r18, max_edge)

    monkeypatch.setattr(commands, "process_preview", process)
    if failure == "blur":
        monkeypatch.setattr(Image.Image, "filter", Mock(side_effect=OSError("模拟模糊失败")))
    items = [
        {"id": 1, "illustType": 2, "xRestrict": 1, "url": preview_url(1)},
        {"id": 2, "illustType": 0, "url": preview_url(2)},
    ]
    if failure in ("missing", "original", "later"):
        items[0]["url"] = {
            "missing": None,
            "original": "https://i.pximg.net/img-original/1_p0.jpg",
            "later": preview_url(1).replace("_p0_", "_p1_"),
        }[failure]

    def handler(request: httpx.Request) -> httpx.Response:
        """只接受搜索和相关作品元数据以及接口提供的低清第一页地址

        Args:
            request: 用于断言网络来源的模拟 HTTP 请求

        Returns:
            搜索或推荐结果以及静态缩略图

        Raises:
            httpx.ReadTimeout: 当前场景要求动图缩略图下载失败
        """
        requests.append(str(request.url))
        if request.url.path == "/ajax/illust/99":
            return ajax({"illustId": 99, "illustType": 0})
        if request.url.path == "/ajax/illust/99/recommend/init":
            return ajax({"illusts": items})
        if "/ajax/search/" in request.url.path:
            route = request.url.path.split("/")[3]
            if route == "illustrations":
                assert request.url.params["type"] == "illust_and_ugoira"
                return ajax({"illust": {"data": items}})
            section, work_id = {"manga": ("manga", 3), "novels": ("novel", 4)}[route]
            return ajax({section: {"data": [{"id": work_id, "url": preview_url(work_id)}]}})
        assert str(request.url) in (preview_url(1), preview_url(2), preview_url(3))
        if str(request.url) == preview_url(1):
            if failure == "timeout":
                raise httpx.ReadTimeout("模拟缩略图超时", request=request)
            if failure == "decode":
                return httpx.Response(
                    200, content=b"invalid", headers={"content-type": "image/png"}
                )
        return httpx.Response(200, content=static, headers={"content-type": "image/png"})

    async def scenario() -> list:
        """执行真实客户端查询并核对预览失败不影响作品索引

        Returns:
            本次查询实际发送的合并转发分包
        """
        config = Config(pixiv_cookie="PHPSESSID=test", pixiv_search_preview=failure != "disabled")
        client = PixivClient(config, httpx.MockTransport(handler))
        bot = SimpleNamespace(
            self_id="10", send=AsyncMock(return_value={"message_id": 99}), delete_msg=AsyncMock()
        )
        event = SimpleNamespace(message_type="private", get_session_id=lambda: "1")
        forward = AsyncMock()
        monkeypatch.setattr(commands, "config", config)
        monkeypatch.setattr(commands, "client", client)
        monkeypatch.setattr(commands, "preview_semaphore", asyncio.Semaphore(4))
        monkeypatch.setattr(commands, "send_forward", forward)
        try:
            if kind == "related":
                await commands._run_related(bot, event, 99)
            else:
                await commands._run_search(bot, event, kind, "测试")
            bot.send.assert_awaited_once_with(event, "正在搜索")
            session = commands.sessions[commands.session_key(bot, event)]
            expected = [1, 2, 3, 4] if kind == "all" else [1, 2]
            assert list(session.results) == expected
            assert [reference.work_id for reference in session.results.values()] == expected
            assert session.results[1].kind == "image"
            return [call.args[2] for call in forward.await_args_list]
        finally:
            await client.close()

    packets = asyncio.run(scenario())
    for operation in [*forbidden, ffmpeg]:
        operation.assert_not_called()
    assert not any(
        "ugoira_meta" in url or ".zip" in url or "img-original" in url for url in requests
    )
    thumbnails = {url for url in requests if "pximg.net" in url}
    expected_thumbnails = {preview_url(index) for index in ([1, 2, 3] if kind == "all" else [1, 2])}
    if failure in ("missing", "original", "later"):
        expected_thumbnails.remove(preview_url(1))
    if failure == "disabled":
        expected_thumbnails.clear()
    assert thumbnails == expected_thumbnails
    assert len(packets[0]) == 2
    node = packets[0][0]
    if failure:
        assert isinstance(node.data["content"], str)
        content = node.data["content"]
    else:
        content = node.data["content"][0].data["text"]
        data = image_bytes(node)
        assert data == blurred
        assert data != plain
        with Image.open(BytesIO(data)) as image:
            assert image.format == "JPEG"
            assert image.size == (300, 160)
    assert content.startswith("[1]\n")
    assert "类型：图片（动图）" in content
    assert "PID：1" in content
    if failure != "disabled":
        assert packets[0][1].data["content"][0].data["text"].startswith("[2]\n")
        assert image_bytes(packets[0][1]) == plain
    if kind == "all":
        assert isinstance(packets[-1][0].data["content"], str)
        assert "小说 ID：4" in packets[-1][0].data["content"]


def make_event(group: bool):
    """构造可走真实群聊或私聊发送路由的 OneBot 事件

    Args:
        group: 是否构造群聊事件，False 表示私聊

    Returns:
        已通过适配器模型校验的消息事件
    """
    values = {
        "time": 0,
        "self_id": 10,
        "post_type": "message",
        "user_id": 20,
        "sub_type": "normal" if group else "friend",
        "message_type": "group" if group else "private",
        "message_id": 1,
        "message": "/px下载图片 42",
        "raw_message": "/px下载图片 42",
        "font": 0,
        "sender": {},
    }
    return GroupMessageEvent(**values, group_id=30) if group else PrivateMessageEvent(**values)


@pytest.mark.parametrize("kind,group", [("image", True), ("auto", False)])
@pytest.mark.parametrize(
    "failure",
    [
        None, "convert", "missing", "r18", "zip_send", "video_send",
        "zip_timeout", "video_timeout", "both_send", "both_timeout",
        "zip_send_video_timeout", "zip_timeout_video_send", "zip_timeout_convert",
    ],
)
def test_download_original_once_and_independent_outputs(
    monkeypatch,
    kind: str,
    group: bool,
    failure: str | None,
) -> None:
    """验证正式下载只取原始 ZIP 一次并独立处理文件与视频发送结果

    Args:
        monkeypatch: 替换网络，编码和 OneBot 接口的 pytest 工具
        kind: 图片下载或自动识别入口
        group: 是否验证群聊文件上传路由
        failure: 本次注入的发送，编码或限制级拒绝场景
    """
    archive_data, meta = make_archive()
    requests = []
    paths = []
    actions = []

    def handler(request):
        """仅允许一次原始 ZIP 请求，拒绝任何 src 或普通图片路径

        Args:
            request: 待检查来源及请求次数的 HTTP 请求

        Returns:
            动图详情，元数据或未经修改的原始 ZIP 内容
        """
        requests.append(str(request.url))
        if request.url.path == "/ajax/illust/42":
            return ajax({"illustId": 42, "illustType": 2, "title": r"../原作", "xRestrict": 1})
        if request.url.path == "/ajax/illust/42/ugoira_meta":
            return ajax(meta_body(meta))
        assert str(request.url) == meta.original_src
        assert requests.count(meta.original_src) == 1
        return httpx.Response(200, content=archive_data)

    async def convert(archive, received, output):
        """验证转换使用已发送的同一个原始 ZIP，且保留完整动图元数据

        Args:
            archive: 命令层传入的已下载原始资源路径
            received: 按原始顺序保留所有帧的动图元数据
            output: 编码完成后需要作为视频发送的本地路径

        Raises:
            UgoiraError: 当前场景要求模拟转换失败
        """
        actions.append("convert")
        assert archive.read_bytes() == archive_data
        assert received == meta
        assert archive.resolve() == paths[0].resolve()
        assert output.parent == archive.parent
        paths.append(output)
        assert output.name == "42_ugoira.mp4"
        if failure in ("convert", "missing", "zip_timeout_convert"):
            raise ugoira.UgoiraError("帧缺失" if failure == "missing" else "模拟编码失败")
        output.write_bytes(b"compatible-mp4")

    async def api(api_name, **kwargs):
        """在发送期间读取 ZIP 内容并验证真实群私聊路由

        Args:
            api_name: 实际调用的 OneBot 文件上传接口
            kwargs: 包含目标会话，文件路径及回执超时的接口参数

        Raises:
            ActionFailed: 当前场景模拟 ZIP 明确上传失败
            ReadTimeout: 当前场景模拟 ZIP 发送结果未确认
        """
        actions.append("zip")
        assert api_name == ("upload_group_file" if group else "upload_private_file")
        assert kwargs["group_id" if group else "user_id"] == (30 if group else 20)
        assert kwargs["_timeout"] == 180
        assert kwargs["name"] == "42_ugoira.zip"
        assert set(kwargs) == {"group_id" if group else "user_id", "file", "name", "_timeout"}
        assert any(isinstance(action, str) and "类型：图片（动图）" in action for action in actions)
        archive = Path(kwargs["file"])
        assert archive.is_absolute()
        assert archive.name == "42_ugoira.zip"
        paths.append(archive)
        assert archive.read_bytes() == archive_data
        assert ".." not in archive.name
        if failure in ("zip_send", "both_send", "zip_send_video_timeout"):
            raise ActionFailed(retcode=1200, status="failed", message="模拟文件上传失败")
        if failure in (
            "zip_timeout", "both_timeout", "zip_timeout_video_send", "zip_timeout_convert"
        ):
            raise httpx.ReadTimeout("模拟文件回执超时")

    async def send(event, content, **kwargs):
        """记录视频消息并确认本地文件在发送期间仍然存在

        Args:
            event: 触发下载的真实群聊或私聊事件
            content: 待发送的状态文本或视频消息段
            kwargs: 仅视频消息使用的 API 回执超时配置

        Returns:
            可供状态撤回使用的消息标识

        Raises:
            ActionFailed: 当前场景要求视频明确发送失败
            ReadTimeout: 当前场景要求视频发送结果未确认
        """
        actions.append(content)
        if isinstance(content, MessageSegment):
            assert content.type == "video"
            assert content.data["file"] == paths[-1].resolve().as_uri()
            assert paths[-1].read_bytes() == b"compatible-mp4"
            assert kwargs == {"_timeout": 180}
            if failure in ("video_send", "both_send", "zip_timeout_video_send"):
                raise ActionFailed(retcode=1200, status="failed", message="模拟视频发送失败")
            if failure in ("video_timeout", "both_timeout", "zip_send_video_timeout"):
                raise httpx.ReadTimeout("模拟视频回执超时")
        else:
            assert not kwargs
        return {"message_id": len(actions)}

    async def scenario():
        """使用真实客户端检查 R18 拦截，三态结果及超时后的延迟清理"""
        config = Config(
            pixiv_cookie="PHPSESSID=test",
            pixiv_r18=failure != "r18",
        )
        client = PixivClient(config, httpx.MockTransport(handler))
        monkeypatch.setattr(commands, "config", config)
        monkeypatch.setattr(commands, "client", client)
        monkeypatch.setattr(ugoira, "convert_to_mp4", convert)
        monkeypatch.setattr(ugoira, "MEDIA_FILE_GRACE", 0.01)
        bot = SimpleNamespace(self_id="10", send=send, call_api=api, delete_msg=AsyncMock())
        try:
            await commands._run_download(bot, make_event(group), kind, 42)
            if failure and "timeout" in failure:
                assert paths[0].exists()
                assert ugoira.cleanup_tasks
                if failure != "zip_timeout_convert":
                    assert paths[-1].exists()
                await asyncio.gather(*list(ugoira.cleanup_tasks))
            else:
                assert all(not path.exists() for path in paths)
        finally:
            await client.close()

    asyncio.run(scenario())
    assert all(not path.exists() for path in paths)
    assert meta.src not in requests
    assert actions.count("zip") <= 1
    assert sum(isinstance(action, MessageSegment) for action in actions) <= 1
    if failure == "r18":
        assert len(requests) == 1
        assert actions[-1] == "已关闭 R18，无法下载该作品"
    else:
        assert requests.count(meta.original_src) == 1
        assert actions.index("zip") < actions.index("正在生成视频") < actions.index("convert")
        if failure in ("convert", "missing", "video_send"):
            assert actions[-1].startswith("原始帧 ZIP 已发送，但 MP4")
            assert "下载失败" != actions[-1]
        elif failure is None:
            assert actions[-1].type == "video"
        else:
            expected = {
                "zip_send": "MP4 已发送，但原始帧 ZIP 发送失败",
                "zip_timeout": "MP4 已发送，原始帧 ZIP 发送结果未确认，请检查聊天记录",
                "video_timeout": "原始帧 ZIP 已发送，MP4 发送结果确认超时，请检查聊天记录",
                "both_send": "原始帧 ZIP 和 MP4 视频发送失败，请查看控制台日志",
                "both_timeout": "原始帧 ZIP 和 MP4 发送结果确认超时，请检查聊天记录",
                "zip_send_video_timeout": (
                    "原始帧 ZIP 发送失败，MP4 发送结果确认超时，请检查聊天记录"
                ),
                "zip_timeout_video_send": (
                    "原始帧 ZIP 发送结果未确认，MP4 视频发送失败，请检查聊天记录"
                ),
                "zip_timeout_convert": (
                    "原始帧 ZIP 发送结果未确认，请检查聊天记录，"
                    "MP4 生成失败，请查看控制台日志"
                ),
            }
            assert actions[-1] == expected[failure]


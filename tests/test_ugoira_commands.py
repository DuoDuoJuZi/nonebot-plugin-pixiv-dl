import asyncio
import threading
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment, PrivateMessageEvent
from PIL import Image
from test_pixiv import ajax
from test_preview import image_bytes, png, preview_url, run_search
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


@pytest.mark.parametrize("failure", [None, "metadata", "zip", "gif", "static", "blur"])
def test_search_ugoira_gif_and_safe_fallback(monkeypatch, failure: str | None) -> None:
    """通过真实搜索流程验证 src 来源，线程处理及限制级安全降级

    Args:
        monkeypatch: 替换网络和注入预览故障的 pytest 工具
        failure: 当前注入的预览失败阶段，空值表示动态预览成功
    """
    archive, meta = make_archive(3, (400, 200))
    requests = []
    source = Image.new("RGB", (300, 160), "white")
    source.paste("black", (0, 0, 150, 160))
    static = png(source)
    owner_thread = threading.get_ident()
    original_builder = ugoira.build_preview_gif

    def builder(*args):
        """检查 Pillow 操作离开主事件循环并按需注入失败

        Args:
            args: 动图 ZIP 路径，元数据和预览设置

        Returns:
            正常处理后的小尺寸 GIF 字节

        Raises:
            OSError: 当前测试要求 GIF 处理失败
        """
        assert threading.get_ident() != owner_thread
        if failure in ("gif", "static"):
            raise OSError("模拟 GIF 失败")
        return original_builder(*args)

    monkeypatch.setattr(ugoira, "build_preview_gif", builder)
    if failure == "blur":
        monkeypatch.setattr(Image.Image, "filter", Mock(side_effect=OSError("模拟模糊失败")))

    def handler(request):
        """仅允许动图请求元数据及 src，不允许搜索读取原始 ZIP

        Args:
            request: 用于断言网络来源的模拟 HTTP 请求

        Returns:
            混合搜索结果，动图资源或静态降级预览
        """
        requests.append(str(request.url))
        assert str(request.url) != meta.original_src
        if "/ajax/search/" in request.url.path:
            return ajax(
                {
                    "illust": {
                        "data": [
                            {"id": 1, "illustType": 2, "xRestrict": 1, "url": preview_url(1)},
                            {"id": 2, "illustType": 0, "url": preview_url(2)},
                        ]
                    }
                }
            )
        if request.url.path == "/ajax/illust/1/ugoira_meta":
            return ajax({} if failure == "metadata" else meta_body(meta))
        if str(request.url) == meta.src:
            return httpx.Response(200, content=b"invalid" if failure == "zip" else archive)
        assert str(request.url) in (preview_url(1), preview_url(2))
        return httpx.Response(
            200,
            content=b"invalid" if failure == "static" else static,
            headers={"content-type": "image/png"},
        )

    packets = asyncio.run(run_search(monkeypatch, handler))
    assert len(packets[0]) == 2
    assert sum("ugoira_meta" in url for url in requests) == 1
    node = packets[0][0]
    if failure in ("static", "blur"):
        assert isinstance(node.data["content"], str)
        assert "类型：图片（动图）" in node.data["content"]
    else:
        assert "类型：图片（动图）" in node.data["content"][0].data["text"]
        data = image_bytes(node)
        if failure:
            assert data == message.process_preview(static, True, 512)
            assert data != message.process_preview(static, False, 512)
        else:
            with Image.open(BytesIO(data)) as gif:
                assert gif.is_animated
                assert gif.size == (256, 128)


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
@pytest.mark.parametrize("failure", [None, "convert", "zip_send", "video_send", "r18", "missing"])
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
        group: 是否验证群聊文件转发路由
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
        """验证转换使用已发送的同一个原始 ZIP，且不套用预览配置

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
        if failure in ("convert", "missing"):
            raise ugoira.UgoiraError("帧缺失" if failure == "missing" else "模拟编码失败")
        output.write_bytes(b"lossless-mp4")

    async def api(name, **kwargs):
        """在发送期间读取 ZIP 内容并验证真实群私聊路由

        Args:
            name: 实际调用的 OneBot 文件合并转发接口
            kwargs: 包含目标会话及文件节点的接口参数

        Raises:
            RuntimeError: 当前场景模拟 ZIP 上传失败
        """
        actions.append("zip")
        assert name == ("send_group_forward_msg" if group else "send_private_forward_msg")
        assert kwargs["group_id" if group else "user_id"] == (30 if group else 20)
        nodes = kwargs["messages"]
        assert "类型：图片（动图）" in nodes[0].data["content"]
        segment = nodes[1].data["content"][0]
        assert segment.type == "file"
        archive = Path(segment.data["file"])
        paths.append(archive)
        assert archive.read_bytes() == archive_data
        assert ".." not in archive.name
        if failure == "zip_send":
            raise RuntimeError("模拟文件上传失败")

    async def send(event, content):
        """记录视频消息并确认本地文件在发送期间仍然存在

        Args:
            event: 触发下载的真实群聊或私聊事件
            content: 待发送的状态文本或视频消息段

        Returns:
            可供状态撤回使用的消息标识

        Raises:
            RuntimeError: 当前场景要求视频上传失败
        """
        actions.append(content)
        if isinstance(content, MessageSegment):
            assert content.type == "video"
            assert content.data["file"] == paths[-1].resolve().as_uri()
            assert paths[-1].read_bytes() == b"lossless-mp4"
            if failure == "video_send":
                raise RuntimeError("模拟视频发送失败")
        return {"message_id": len(actions)}

    async def scenario():
        """使用真实客户端完成命令编排并检查 R18 提前拦截"""
        config = Config(
            pixiv_cookie="PHPSESSID=test",
            pixiv_r18=failure != "r18",
            pixiv_ugoira_preview_max_frames=1,
            pixiv_ugoira_preview_max_edge=64,
        )
        client = PixivClient(config, httpx.MockTransport(handler))
        monkeypatch.setattr(commands, "config", config)
        monkeypatch.setattr(commands, "client", client)
        monkeypatch.setattr(ugoira, "convert_to_mp4", convert)
        bot = SimpleNamespace(self_id="10", send=send, call_api=api, delete_msg=AsyncMock())
        try:
            await commands._run_download(bot, make_event(group), kind, 42)
        finally:
            await client.close()

    asyncio.run(scenario())
    assert all(not path.exists() for path in paths)
    assert meta.src not in requests
    if failure == "r18":
        assert len(requests) == 1
        assert actions[-1] == "已关闭 R18，无法下载该作品"
    else:
        assert requests.count(meta.original_src) == 1
        assert actions.index("zip") < actions.index("正在生成视频") < actions.index("convert")
        if failure in ("convert", "missing", "video_send"):
            assert actions[-1].startswith("原始帧 ZIP 已发送，但 MP4")
            assert "下载失败" != actions[-1]
        elif failure == "zip_send":
            assert actions[-1] == "MP4 已发送，但原始帧 ZIP 发送失败"
        else:
            assert actions[-1].type == "video"


def test_ugoira_preview_concurrency_is_separate_and_bounded(monkeypatch) -> None:
    """验证多个动图 GIF 串行处理且不会增加普通图片的详情请求

    Args:
        monkeypatch: 替换动图网络和预览处理的 pytest 工具
    """
    data, meta = make_archive()
    active = 0
    peak = 0

    async def handler(request):
        """响应多部动图搜索及各自的预览 ZIP

        Args:
            request: 本次搜索，元数据或预览资源请求

        Returns:
            对应请求的成功响应
        """
        if "/ajax/search/" in request.url.path:
            return ajax({"illust": {"data": [{"id": i, "illustType": 2} for i in range(3)]}})
        if request.url.path.endswith("/ugoira_meta"):
            return ajax(meta_body(meta))
        assert str(request.url) == meta.src
        return httpx.Response(200, content=data)

    async def build(*args):
        """记录 GIF 编码同时占用的数量

        Args:
            args: 由命令层传入的预览源和处理配置

        Returns:
            用于转发节点构造的预览字节
        """
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return b"gif"

    monkeypatch.setattr(ugoira, "preview_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(ugoira, "build_preview", build)
    asyncio.run(run_search(monkeypatch, handler))
    assert peak == 1

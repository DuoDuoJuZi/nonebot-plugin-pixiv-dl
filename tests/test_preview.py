import asyncio
import base64
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from PIL import Image, ImageChops
from pydantic import ValidationError

from nonebot_plugin_pixiv_dl import commands, message
from nonebot_plugin_pixiv_dl.config import Config
from nonebot_plugin_pixiv_dl.pixiv import PixivClient, _search_artwork


# @Author: DuoDuoJuZi
# @Date: 2026-09-30
def preview_url(work_id: int = 1) -> str:
    """生成模拟搜索响应使用的第一页低清地址

    Args:
        work_id: 用于区分模拟预览请求的作品 ID

    Returns:
        包含 p0 标记的模拟 Pixiv 缩略图地址
    """
    return f"https://i.pximg.net/c/250x250_80_a2/custom-thumb/img/{work_id}_p0_custom1200.jpg"


def png(image: Image.Image) -> bytes:
    """将合成测试图像编码到内存

    Args:
        image: 用于验证预览处理的非敏感图像

    Returns:
        可由 Pillow 解码的 PNG 内容
    """
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def image_bytes(node) -> bytes:
    """提取搜索节点中发送的实际图片内容

    Args:
        node: 同时包含作品元数据和预览的合并转发节点

    Returns:
        从 OneBot 图片消息段还原的二进制内容
    """
    content = node.data["content"]
    assert [segment.type for segment in content] == ["text", "image"]
    value = content[1].data["file"]
    assert value.startswith("base64://")
    return base64.b64decode(value.removeprefix("base64://"))


async def run_search(monkeypatch, handler, kind: str = "image", **options) -> list:
    """通过模拟网络执行真实搜索流程并收集合并转发

    Args:
        monkeypatch: 用于临时替换请求客户端与消息接口的 pytest 工具
        handler: 为搜索 JSON 和图片请求提供响应的模拟传输函数
        kind: 本次请求的搜索分类，all 表示聚合搜索
        **options: 覆盖本次搜索的插件配置

    Returns:
        按发送顺序收集的合并转发分包列表
    """
    config = Config(pixiv_cookie="PHPSESSID=test", **options)
    client = PixivClient(config, httpx.MockTransport(handler))
    forward = AsyncMock()
    bot = SimpleNamespace(
        self_id="10",
        send=AsyncMock(return_value={"message_id": 99}),
        delete_msg=AsyncMock(),
    )
    event = SimpleNamespace(
        message_type="private", get_session_id=lambda: "1", get_user_id=lambda: "1"
    )
    monkeypatch.setattr(commands, "config", config)
    monkeypatch.setattr(commands, "client", client)
    monkeypatch.setattr(
        commands, "preview_semaphore", asyncio.Semaphore(config.pixiv_preview_concurrency)
    )
    monkeypatch.setattr(commands, "send_forward", forward)
    try:
        await commands._run_search(bot, event, kind, "测试")
        bot.send.assert_awaited_once_with(event, "正在搜索")
        bot.delete_msg.assert_awaited_once_with(message_id=99)
        assert forward.await_count > 0
        return [call.args[2] for call in forward.await_args_list]
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"url": preview_url(), "thumb": preview_url(2)}, preview_url()),
        (
            {"url": "https://i.pximg.net/img-original/1_p0.jpg", "thumb": preview_url()},
            preview_url(),
        ),
        ({"urls": {"thumbnail": preview_url()}}, preview_url()),
        ({"small": preview_url()}, preview_url()),
        ({"url": "https://i.pximg.net/img-original/1_p0.jpg"}, None),
        ({"url": "https://i.pximg.net/img-master/1_p0_master1200.jpg"}, None),
        ({"url": preview_url().replace("_p0_", "_p1_")}, None),
        ({"url": "https://example.org/thumb.jpg"}, None),
        ({"url": "https://[", "small": preview_url()}, preview_url()),
        ({"url": None, "urls": []}, None),
    ],
)
def test_preview_url_uses_api_candidates_and_skips_originals(
    fields: dict, expected: str | None
) -> None:
    """验证预览直接使用接口地址并跳过原图，常规图片和后续页面

    Args:
        fields: 模拟搜索响应中的图片地址字段
        expected: 应保留的低清地址，空值表示不应展示预览
    """
    work = _search_artwork({"id": "1", **fields}, "image")
    assert work.preview_url == expected


@pytest.mark.parametrize("kind", ["image", "manga"])
def test_search_downloads_only_p0_and_keeps_preview_in_metadata_node(
    monkeypatch, kind: str
) -> None:
    """验证插画与多页漫画直接下载搜索缩略图并按作品数量分包

    Args:
        monkeypatch: 用于临时替换请求客户端与消息接口的 pytest 工具
        kind: 本次验证的插画或漫画搜索分类
    """
    requests = []
    data = png(Image.new("RGB", (1024, 512), "blue"))
    route, section = ("illustrations", "illust") if kind == "image" else ("manga", "manga")

    def handler(request: httpx.Request) -> httpx.Response:
        """仅响应一次分类搜索和对应作品的第一页缩略图

        Args:
            request: 待验证路径与请求头的模拟 HTTP 请求

        Returns:
            搜索元数据或合成预览内容
        """
        requests.append(str(request.url))
        if request.url.host == "www.pixiv.net":
            assert request.url.path == f"/ajax/search/{route}/测试"
            items = [
                {
                    "id": str(index),
                    "title": f"作品 {index}",
                    "illustType": 0 if kind == "image" else 1,
                    "pageCount": 80,
                    "url": preview_url(index),
                }
                for index in range(1, 4)
            ]
            return httpx.Response(200, json={"error": False, "body": {section: {"data": items}}})
        assert str(request.url) in [preview_url(index) for index in range(1, 4)]
        assert request.headers["referer"] == "https://www.pixiv.net/"
        assert request.headers["user-agent"]
        assert "cookie" not in request.headers
        return httpx.Response(200, content=data, headers={"content-type": "image/png"})

    packets = asyncio.run(run_search(monkeypatch, handler, kind, pixiv_forward_max_messages=2))
    assert [len(packet) for packet in packets] == [3, 2]
    nodes = [node for packet in packets for node in packet[:-1]]
    for index, node in enumerate(nodes, 1):
        assert f"PID：{index}\n" in node.data["content"][0].data["text"]
        with Image.open(BytesIO(image_bytes(node))) as image:
            assert image.format == "JPEG"
            assert image.size == (512, 256)
    assert len(requests) == 4
    assert all("img-original" not in url for url in requests)


def test_aggregate_previews_leave_novels_as_metadata(monkeypatch) -> None:
    """验证聚合搜索保持三个分类转发且小说不下载封面

    Args:
        monkeypatch: 用于临时替换请求客户端与消息接口的 pytest 工具
    """
    requests = []
    data = png(Image.new("RGB", (64, 32), "green"))

    def handler(request: httpx.Request) -> httpx.Response:
        """为三个搜索分类提供带封面字段的模拟响应

        Args:
            request: 待验证分类路径或缩略图地址的 HTTP 请求

        Returns:
            对应分类的搜索结果或合成图片
        """
        requests.append(str(request.url))
        if request.url.host == "www.pixiv.net":
            route = request.url.path.split("/")[3]
            section, work_id = {
                "illustrations": ("illust", 1),
                "manga": ("manga", 2),
                "novels": ("novel", 3),
            }[route]
            item = {"id": str(work_id), "url": preview_url(work_id)}
            return httpx.Response(200, json={"error": False, "body": {section: {"data": [item]}}})
        assert str(request.url) in (preview_url(1), preview_url(2))
        return httpx.Response(200, content=data, headers={"content-type": "image/png"})

    packets = asyncio.run(run_search(monkeypatch, handler, "all"))
    assert [packet[0].data["nickname"] for packet in packets] == [
        "Pixiv 图片",
        "Pixiv 漫画",
        "Pixiv 小说",
    ]
    assert image_bytes(packets[0][0])
    assert image_bytes(packets[1][0])
    assert isinstance(packets[2][0].data["content"], str)
    assert len(requests) == 5


def test_r18_uses_xrestrict_and_sends_decodable_blurred_bytes(monkeypatch) -> None:
    """验证限制级作品使用轻度模糊并保留局部对比度，输出可解码的 JPEG

    Args:
        monkeypatch: 用于临时替换请求客户端与消息接口的 pytest 工具
    """
    source = Image.new("RGB", (1024, 512), "white")
    for left in range(0, 1024, 32):
        source.paste("black", (left, 0, left + 16, 512))
    data = png(source)

    def handler(request: httpx.Request) -> httpx.Response:
        """提供仅通过 xRestrict 标记的限制级搜索结果

        Args:
            request: 用于区分搜索 JSON 和预览图片的 HTTP 请求

        Returns:
            限制级元数据或非敏感条纹图像
        """
        if request.url.host == "www.pixiv.net":
            item = {"id": "1", "tags": [], "xRestrict": 1, "url": preview_url()}
            return httpx.Response(200, json={"error": False, "body": {"illust": {"data": [item]}}})
        return httpx.Response(200, content=data, headers={"content-type": "image/png"})

    packets = asyncio.run(run_search(monkeypatch, handler))
    processed = image_bytes(packets[0][0])
    unblurred = message.process_preview(data, False, 512)
    assert processed != data
    assert processed != unblurred
    assert "R18：是" in packets[0][0].data["content"][0].data["text"]
    with Image.open(BytesIO(processed)) as blurred, Image.open(BytesIO(unblurred)) as plain:
        assert blurred.format == "JPEG"
        assert blurred.size == (512, 256)
        assert ImageChops.difference(blurred, plain).getbbox() is not None
        darkest, brightest = blurred.convert("L").crop((32, 32, 480, 224)).getextrema()
        assert brightest - darkest > 100


def test_preview_composites_transparency_on_white_without_upscaling() -> None:
    """验证透明预览转 JPEG 使用白底且小图不会被放大"""
    source = Image.new("RGBA", (80, 40), (255, 0, 0, 0))
    processed = message.process_preview(png(source), False, 512)
    with Image.open(BytesIO(processed)) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"
        assert image.size == (80, 40)
        assert image.getpixel((40, 20)) == (255, 255, 255)


@pytest.mark.parametrize("failure", ["timeout", "decode", "blur"])
def test_preview_failure_keeps_metadata_and_never_sends_raw_r18(monkeypatch, failure: str) -> None:
    """验证单张预览请求或处理失败仅影响图片且不泄露限制级原预览

    Args:
        monkeypatch: 用于临时替换请求客户端与消息接口的 pytest 工具
        failure: 当前注入的请求超时，解码错误或模糊错误
    """
    data = png(Image.new("RGB", (64, 32), "red"))
    warning = Mock()
    monkeypatch.setattr(commands.logger, "warning", warning)
    if failure == "blur":
        monkeypatch.setattr(message.Image.Image, "filter", Mock(side_effect=OSError("模糊失败")))

    def handler(request: httpx.Request) -> httpx.Response:
        """令限制级作品的预览失败并保留另一部普通作品

        Args:
            request: 待注入异常或返回合成图像的 HTTP 请求

        Returns:
            两部作品的元数据或对应预览内容

        Raises:
            httpx.ReadTimeout: 当前注入超时且请求限制级预览
        """
        if request.url.host == "www.pixiv.net":
            items = [
                {"id": str(index), "xRestrict": 1 if index == 1 else 0, "url": preview_url(index)}
                for index in (1, 2)
            ]
            return httpx.Response(200, json={"error": False, "body": {"illust": {"data": items}}})
        if str(request.url) == preview_url(1):
            if failure == "timeout":
                raise httpx.ReadTimeout("模拟超时", request=request)
            if failure == "decode":
                return httpx.Response(
                    200, content=b"invalid-image", headers={"content-type": "image/png"}
                )
        return httpx.Response(200, content=data, headers={"content-type": "image/png"})

    packets = asyncio.run(run_search(monkeypatch, handler))
    assert len(packets[0]) == 3
    assert isinstance(packets[0][0].data["content"], str)
    assert "PID：1" in packets[0][0].data["content"]
    assert image_bytes(packets[0][1])
    warning.assert_called_once()
    assert warning.call_args.args[1:3] == (1, preview_url(1))


@pytest.mark.parametrize(
    ("fields", "enabled"),
    [
        ({"url": preview_url()}, False),
        ({}, True),
        ({"url": "https://i.pximg.net/img-original/1_p0.jpg"}, True),
        ({"url": "https://i.pximg.net/img-master/1_p0_master1200.jpg"}, True),
        ({"url": preview_url().replace("_p0_", "_p1_")}, True),
    ],
)
def test_unavailable_or_disabled_preview_makes_no_image_request(
    monkeypatch, fields: dict, enabled: bool
) -> None:
    """验证预览关闭或低清地址缺失时没有图片及详情请求

    Args:
        monkeypatch: 用于临时替换请求客户端与消息接口的 pytest 工具
        fields: 当前搜索结果提供的图片地址字段
        enabled: 是否允许本次搜索下载预览
    """
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        """仅接受一次搜索请求并提供无可用预览的结果

        Args:
            request: 待验证不发生图片及详情查询的 HTTP 请求

        Returns:
            包含当前地址字段的作品搜索结果
        """
        requests.append(str(request.url))
        assert request.url.path == "/ajax/search/illustrations/测试"
        return httpx.Response(
            200, json={"error": False, "body": {"illust": {"data": [{"id": "1", **fields}]}}}
        )

    packets = asyncio.run(run_search(monkeypatch, handler, pixiv_search_preview=enabled))
    assert len(requests) == 1
    assert isinstance(packets[0][0].data["content"], str)


@pytest.mark.parametrize("illust_type", [0, 2])
def test_preview_concurrency_is_bounded_and_preserves_result_order(
    monkeypatch, illust_type: int
) -> None:
    """验证普通图片与动图共用并发上限且下载完成顺序不会改变作品节点顺序

    Args:
        monkeypatch: 用于临时替换请求客户端与消息接口的 pytest 工具
        illust_type: 奇数编号作品的 Pixiv 类型，用于检查混合动图预览
    """
    active = 0
    peak = 0
    completed = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """使用不同延迟响应预览并记录实际并发数量

        Args:
            request: 待返回搜索元数据或延迟图片内容的 HTTP 请求

        Returns:
            有序搜索结果或带作品标记色的合成图片
        """
        nonlocal active, peak
        if request.url.host == "www.pixiv.net":
            items = [
                {
                    "id": str(index),
                    "url": preview_url(index),
                    "illustType": illust_type if index % 2 else 0,
                }
                for index in range(1, 9)
            ]
            return httpx.Response(200, json={"error": False, "body": {"illust": {"data": items}}})
        work_id = int(request.url.path.rsplit("/", 1)[1].split("_", 1)[0])
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep((9 - work_id) * 0.01)
            completed.append(work_id)
            data = png(Image.new("RGB", (64, 32), (work_id * 20, 0, 0)))
            return httpx.Response(200, content=data, headers={"content-type": "image/png"})
        finally:
            active -= 1

    packets = asyncio.run(run_search(monkeypatch, handler))
    assert peak == 4
    assert completed != list(range(1, 9))
    for index, node in enumerate(packets[0][:-1], 1):
        assert f"PID：{index}\n" in node.data["content"][0].data["text"]
        with Image.open(BytesIO(image_bytes(node))) as image:
            assert abs(image.getpixel((32, 16))[0] - index * 20) <= 3


def test_preview_config_defaults_and_limits() -> None:
    """验证预览与分包默认配置及对应上限，拒绝越界值"""
    config = Config()
    assert config.pixiv_search_preview is True
    assert config.pixiv_preview_concurrency == 4
    assert config.pixiv_preview_max_edge == 512
    assert config.pixiv_forward_max_messages == 20
    assert Config(pixiv_forward_max_messages=99).pixiv_forward_max_messages == 99
    for options in (
        {"pixiv_preview_concurrency": 0},
        {"pixiv_preview_concurrency": 17},
        {"pixiv_preview_max_edge": 63},
        {"pixiv_preview_max_edge": 1025},
        {"pixiv_forward_max_messages": 1},
        {"pixiv_forward_max_messages": 100},
    ):
        with pytest.raises(ValidationError):
            Config(**options)


@pytest.mark.parametrize("kind", ["image", "manga", "novel", "all"])
def test_next_page_preserves_previews_blur_metadata_and_packets(monkeypatch, kind: str) -> None:
    """验证图片和动图翻页共用静态预览处理，小说保持元数据并按作品分包

    Args:
        monkeypatch: 临时替换外部网络与 OneBot 发送接口的 pytest 工具
        kind: 本次验证的单分类或聚合搜索模式
    """
    source = Image.new("RGB", (1024, 512), "white")
    for left in range(0, 1024, 32):
        source.paste("black", (left, 0, left + 16, 512))
    data = png(source)
    requests = []
    actions = []
    packets = []
    kinds = ["image", "manga", "novel"] if kind == "all" else [kind]

    def handler(request: httpx.Request) -> httpx.Response:
        """提供两页不同作品并拒绝原图与额外详情查询

        Args:
            request: 搜索分页请求或低清预览请求

        Returns:
            当前 Pixiv 页元数据或用于检查模糊的合成图片
        """
        requests.append(request)
        if request.url.host == "www.pixiv.net":
            assert request.url.path.startswith("/ajax/search/")
            route = request.url.path.split("/")[3]
            section, illust_type = {
                "illustrations": ("illust", 0),
                "manga": ("manga", 1),
                "novels": ("novel", 0),
            }[route]
            page = int(request.url.params["p"])
            items = [
                {
                    "id": page * 10 + index,
                    "title": f"作品 {index}",
                    "userName": "作者",
                    "illustType": 2 if route == "illustrations" and index == 1 else illust_type,
                    "pageCount": 22,
                    "url": preview_url(page * 10 + index),
                    "xRestrict": index == 1,
                    "seriesId": 99,
                    "seriesTitle": "系列",
                    "description": "简介",
                }
                for index in (1, 2, 3)
            ]
            return httpx.Response(
                200, json={"error": False, "body": {section: {"data": items, "total": 120}}}
            )
        assert "/c/250x250_80_a2/" in request.url.path
        assert "_p0_" in request.url.path
        assert "cookie" not in request.headers
        return httpx.Response(200, content=data, headers={"content-type": "image/png"})

    async def send(event, content):
        """收集状态与页码提示的发送顺序

        Args:
            event: 触发测试搜索的消息上下文
            content: 状态或分页提示文本

        Returns:
            用于撤回状态提示的模拟消息标识
        """
        actions.append(("message", content))
        return {"message_id": 77}

    async def recall(*, message_id):
        """记录状态消息撤回顺序

        Args:
            message_id: 待撤回的状态提示标识
        """
        actions.append(("recall", message_id))

    async def forward(bot, event, packet):
        """收集实际构造的搜索转发节点

        Args:
            bot: 发送消息的模拟机器人
            event: 触发搜索的聊天环境
            packet: 当前分类的一组合并转发节点
        """
        actions.append(("forward", len(packet)))
        packets.append(packet)

    async def scenario():
        """执行真实客户端首批查询与下一页并检查实际发送内容"""
        config = Config(
            pixiv_cookie="PHPSESSID=test", pixiv_search_limit=3, pixiv_forward_max_messages=2
        )
        client = PixivClient(config, httpx.MockTransport(handler))
        bot = SimpleNamespace(self_id="10", send=send, delete_msg=recall)
        event = SimpleNamespace(
            message_type="private", get_session_id=lambda: "1", get_user_id=lambda: "1"
        )
        monkeypatch.setattr(commands, "config", config)
        monkeypatch.setattr(commands, "client", client)
        monkeypatch.setattr(commands, "preview_semaphore", asyncio.Semaphore(4))
        monkeypatch.setattr(commands, "send_forward", forward)
        try:
            await commands._run_search(bot, event, kind, "测试")
            packets.clear()
            actions.clear()
            await commands._run_next(bot, event)
        finally:
            await client.close()

    asyncio.run(scenario())
    assert actions[:2] == [("message", "正在搜索"), ("recall", 77)]
    assert [len(packet) for packet in packets] == [3, 2] * len(kinds)
    assert all(action[0] == "forward" for action in actions[2:])
    for index, category in enumerate(kinds):
        first, last = packets[index * 2 : index * 2 + 2]
        assert first[-1].data["content"] == "[提示]\n本次结果还有后续分包，将自动发送"
        assert last[-1].data["content"] == f"[提示]\n{message.KIND_NAMES[category]}已是最后一页"
        nodes = first[:-1] + last[:-1]
        assert all(
            node.data["nickname"] == f"Pixiv {message.KIND_NAMES[category]}" for node in nodes
        )
        if category == "novel":
            assert all(isinstance(node.data["content"], str) for node in nodes)
            assert "小说 ID：21" in nodes[0].data["content"]
            assert "系列 ID：99" in nodes[0].data["content"]
        else:
            assert "PID：21" in nodes[0].data["content"][0].data["text"]
            assert "作品总页数：22" in nodes[0].data["content"][0].data["text"]
            if category == "image":
                assert "类型：图片（动图）" in nodes[0].data["content"][0].data["text"]
            blurred = image_bytes(nodes[0])
            assert blurred == message.process_preview(data, True, 512)
            assert blurred != message.process_preview(data, False, 512)
            with Image.open(BytesIO(blurred)) as image:
                assert image.format == "JPEG"
                assert image.size == (512, 256)
    search_requests = [request for request in requests if request.url.host == "www.pixiv.net"]
    assert [request.url.params["p"] for request in search_requests] == ["1"] * len(kinds) + [
        "2"
    ] * len(kinds)

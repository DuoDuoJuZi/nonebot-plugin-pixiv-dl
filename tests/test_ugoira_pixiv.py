import asyncio
from pathlib import Path

import httpx
import pytest
from test_pixiv import ajax, make_client
from test_ugoira import make_archive

from nonebot_plugin_pixiv_dl.pixiv import PixivAPIError, PixivNetworkError, PixivResourceError


# @Author: DuoDuoJuZi
# @Date: 2026-10-03
def metadata() -> dict:
    """构造符合 Pixiv Web AJAX 字段名称的动图元数据

    Returns:
        带有不同延时帧及两种 ZIP 地址的接口业务对象
    """
    return {
        "src": "https://i.pximg.net/preview.zip",
        "originalSrc": "https://i.pximg.net/original.zip",
        "mime_type": "image/jpeg",
        "frames": [{"file": "000001.jpg", "delay": 40}, {"file": "000000.jpg", "delay": 80}],
    }


def test_search_types_and_detail_preserve_ugoira() -> None:
    """验证图片搜索保留插画与动图，漫画独立过滤且详情保留原始分类"""
    seen = []

    def handler(request):
        """提供混合分类搜索结果及动图详情

        Args:
            request: 待检查查询参数和路径的模拟 HTTP 请求

        Returns:
            含有 Pixiv 原始作品分类的模拟响应
        """
        seen.append(request)
        if request.url.path == "/ajax/illust/2":
            return ajax({"illustId": 2, "illustType": 2, "title": "动图"})
        key = "illust" if "/illustrations/" in request.url.path else "manga"
        return ajax({key: {"data": [{"id": i, "illustType": i} for i in range(3)]}})

    async def scenario():
        """执行两个搜索入口和详情入口并验证实际分类"""
        client = make_client(handler)
        try:
            images = await client.search_artworks("测试", 20)
            manga = await client.search_manga("测试", 20)
            detail = await client.get_illust(2)
            assert [work.illust_type for work in images.items] == [0, 2]
            assert [work.illust_type for work in manga.items] == [1]
            assert detail.type == "image"
            assert detail.is_ugoira
            assert images.items[1].is_ugoira
        finally:
            await client.close()

    asyncio.run(scenario())
    assert seen[0].url.params["type"] == "illust_and_ugoira"
    assert seen[1].url.params["type"] == "manga"


def test_metadata_parses_original_order_and_delay() -> None:
    """验证动图元数据保留原始字段与非文件名排序的帧时序"""
    requests = []

    def handler(request):
        """记录实际请求的动图元数据路径

        Args:
            request: 待断言接口路径的 HTTP 请求

        Returns:
            包含完整字段的模拟动图元数据响应
        """
        requests.append(request)
        return ajax(metadata())

    async def scenario():
        """通过客户端解析元数据并检查所有核心字段"""
        client = make_client(handler)
        try:
            meta = await client.get_ugoira_meta(42)
            assert meta.src == metadata()["src"]
            assert meta.original_src == metadata()["originalSrc"]
            assert meta.mime_type == "image/jpeg"
            assert [(frame.file, frame.delay) for frame in meta.frames] == [
                ("000001.jpg", 40),
                ("000000.jpg", 80),
            ]
        finally:
            await client.close()

    asyncio.run(scenario())
    assert requests[0].url.path == "/ajax/illust/42/ugoira_meta"


@pytest.mark.parametrize(
    "field,value",
    [
        ("frames", []),
        ("frames", None),
        ("frames", [None]),
        ("frames", [{"file": "a.jpg", "delay": 10}] * 10001),
        ("src", "http://i.pximg.net/a.zip"),
        ("originalSrc", "https://example.org/a.zip"),
        ("src", "https://i.pximg.net.evil.org/a.zip"),
        ("src", "https://user:secret@i.pximg.net/a.zip"),
        ("src", "https://i.pximg.net:123/a.zip"),
        ("src", "https://["),
        ("mime_type", "text/html"),
    ],
)
def test_metadata_rejects_invalid_fields(field: str, value: object) -> None:
    """验证元数据拒绝不可信地址和异常帧列表

    Args:
        field: 当前替换的元数据字段
        value: 用于触发严格校验失败的无效值
    """

    async def scenario():
        """向真实解析入口提供无效字段并检查异常分类"""
        client = make_client(lambda request: ajax({**metadata(), field: value}))
        try:
            with pytest.raises(PixivAPIError, match="元数据无效"):
                await client.get_ugoira_meta(1)
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "filename",
    [
        "../foo",
        "/path/foo",
        r"C:\foo",
        r"a\b.jpg",
        "..",
        "a..jpg",
        "CON.jpg",
        "a'file.jpg",
        "a\n.jpg",
        "",
    ],
)
def test_metadata_rejects_unsafe_frame_names(filename: str) -> None:
    """验证路径穿越和平台特殊帧文件名均被拒绝

    Args:
        filename: 当前待检查的危险帧文件名
    """
    test_metadata_rejects_invalid_fields("frames", [{"file": filename, "delay": 10}])


@pytest.mark.parametrize("delay", [0, -1, 1.5, "40", True, None])
def test_metadata_rejects_invalid_delays(delay: object) -> None:
    """验证毫秒延时必须为严格正整数

    Args:
        delay: 当前待检查的无效停留时间
    """
    test_metadata_rejects_invalid_fields("frames", [{"file": "a.jpg", "delay": delay}])


class ChunkStream(httpx.AsyncByteStream):
    """以多个分块提供资源并禁止一次性读取整个 HTTP 响应"""

    def __init__(self, data: bytes, fail: bool = False) -> None:
        """保存模拟流式资源及中途失败开关

        Args:
            data: 需要按块传输的原始 ZIP 字节
            fail: 是否在首个分块后注入读取超时
        """
        self.data = data
        self.fail = fail

    async def __aiter__(self):
        """逐块提供数据或在传输中途模拟失败

        Yields:
            保持原始顺序的 ZIP 数据分块

        Raises:
            httpx.ReadTimeout: 本次模拟流要求在首块后超时
        """
        for start in range(0, len(self.data), 31):
            yield self.data[start : start + 31]
            if self.fail:
                raise httpx.ReadTimeout("模拟超时")


@pytest.mark.parametrize("failure", [None, "timeout", "oversize", "empty", "redirect", "http"])
def test_archive_streams_bytes_and_cleans_partial_files(
    tmp_path: Path,
    monkeypatch,
    failure: str | None,
) -> None:
    """验证流式传输保持原始 ZIP 字节，失败时不留下部分文件

    Args:
        tmp_path: 当前测试独占的临时目录
        monkeypatch: 禁止响应整体读取的 pytest 工具
        failure: 需要注入的网络或资源故障，空值表示成功
    """
    data, _ = make_archive()
    requests = []

    def handler(request):
        """返回分块响应并检查共享 CDN 请求配置

        Args:
            request: 待校验请求头和域名的 HTTP 请求

        Returns:
            未预先读入内存的模拟流式响应
        """
        requests.append(request)
        assert request.headers["referer"] == "https://www.pixiv.net/"
        assert request.headers["user-agent"]
        assert "cookie" not in request.headers
        code = 302 if failure == "redirect" else 503 if failure == "http" else 200
        return httpx.Response(
            code,
            stream=ChunkStream(b"" if failure == "empty" else data, failure == "timeout"),
            headers={"location": "https://example.org/a.zip"},
        )

    async def forbid_read(self):
        """拒绝将完整响应提前读取到内存

        Raises:
            AssertionError: 下载代码尝试整体读取流式响应
        """
        raise AssertionError("ZIP 必须流式读取")

    monkeypatch.setattr(httpx.Response, "aread", forbid_read)
    target = tmp_path / "original.zip"

    async def scenario():
        """执行流式下载并检查成功资源与失败清理"""
        client = make_client(handler)
        try:
            if failure:
                error = PixivNetworkError if failure == "timeout" else PixivAPIError
                if failure == "oversize":
                    error = PixivResourceError
                with pytest.raises(error):
                    await client.download_ugoira_archive(
                        "https://i.pximg.net/original.zip",
                        target,
                        1 if failure == "oversize" else 2**30,
                    )
                assert not target.exists()
            else:
                await client.download_ugoira_archive("https://i.pximg.net/original.zip", target)
                assert target.read_bytes() == data
        finally:
            await client.close()

    asyncio.run(scenario())
    assert len(requests) == 1

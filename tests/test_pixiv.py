import asyncio
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from nonebot_plugin_pixiv_dl.config import Config
from nonebot_plugin_pixiv_dl.models import Novel
from nonebot_plugin_pixiv_dl.pixiv import (
    FixedIPBackend,
    FixedIPTransport,
    PixivAPIError,
    PixivAuthError,
    PixivClient,
    PixivNetworkError,
    PixivNotFoundError,
    PixivR18Error,
)


# @Author: DuoDuoJuZi
# @Date: 2026-09-29
def ajax(body: object, error: bool = False) -> httpx.Response:
    """构造 Pixiv Web AJAX 模拟响应

    Args:
        body: 模拟接口返回的业务数据
        error: 是否将响应标记为业务失败

    Returns:
        包含业务数据和错误标记的 HTTP 响应
    """
    return httpx.Response(200, json={"error": error, "message": "failure", "body": body})


def make_client(handler, **options) -> PixivClient:
    """创建通过模拟传输层请求数据的 Pixiv 客户端

    Args:
        handler: 接收模拟请求并生成响应的处理函数
        options: 覆盖默认测试配置的插件选项

    Returns:
        使用模拟传输层和占位 Cookie 的客户端
    """
    return PixivClient(
        Config(pixiv_cookie="test_cookie=placeholder", **options),
        transport=httpx.MockTransport(handler),
    )


def test_proxy_routing_configuration() -> None:
    """验证共享客户端的显式代理与环境代理配置"""
    with patch("nonebot_plugin_pixiv_dl.pixiv.httpx.AsyncClient") as factory:
        PixivClient(Config(pixiv_proxy="http://127.0.0.1:7890", pixiv_fixed_ip="203.0.113.1"))
        options = factory.call_args.kwargs
        assert options["proxy"] == "http://127.0.0.1:7890"
        assert options["mounts"] is None
        assert options["trust_env"] is False

        PixivClient(Config())
        options = factory.call_args.kwargs
        assert options["proxy"] is None
        assert options["trust_env"] is True


def test_search_categories_empty_results_and_r18_filter() -> None:
    """验证插画与漫画及小说搜索，以及空结果和限制级过滤"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        """根据搜索路径提供对应分类的模拟预览

        Args:
            request: 模拟传输层收到的 HTTP 请求

        Returns:
            对应分类的搜索响应，不包含图片内容
        """
        seen.append(request)
        if "/illustrations/" in request.url.path:
            items = [
                {
                    "id": "11",
                    "title": "画",
                    "userId": "2",
                    "userName": "作者",
                    "tags": ["tag"],
                    "pageCount": 2,
                    "xRestrict": 0,
                },
                {
                    "id": "12",
                    "title": "成人",
                    "userId": "2",
                    "userName": "作者",
                    "tags": [],
                    "xRestrict": 1,
                },
            ]
            return ajax({"illust": {"data": items, "total": 2}})
        if "/manga/" in request.url.path:
            return ajax(
                {"manga": {"data": [{"id": 21, "title": "漫画", "xRestrict": 0}], "total": 1}}
            )
        return ajax({"novel": {"data": [], "total": 0}})

    async def scenario() -> None:
        """通过同一模拟传输层验证三个分类的搜索结果"""
        client = make_client(handler, pixiv_r18=False)
        try:
            images = await client.search_artworks("初音", 20)
            manga = await client.search_manga("初音", 20)
            novels = await client.search_novels("初音", 20)
            assert [item.id for item in images.items] == [11]
            assert images.items[0].page_count == 2
            assert not images.items[0].is_r18
            assert [item.type for item in manga.items] == ["manga"]
            assert novels.items == []
            assert not any(page.has_next for page in (images, manga, novels))
        finally:
            await client.close()

    asyncio.run(scenario())
    assert len(seen) == 3
    assert all(request.url.params["mode"] == "safe" for request in seen)
    assert all(request.headers["referer"] == "https://www.pixiv.net/" for request in seen)
    assert all("test_cookie" in request.headers["cookie"] for request in seen)


def test_related_endpoints_types_limit_and_r18() -> None:
    """验证相关接口复用模型解析并保留分类顺序，过滤限制级内容且忽略后续页"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        """提供包含占位项及多种作品类型的首批相关推荐

        Args:
            request: 用于核对插画或小说路径及数量上限的请求

        Returns:
            包含限制级作品和后续页标识的模拟相关响应
        """
        seen.append(request)
        key = "novels" if "/novel/" in request.url.path else "illusts"
        return ajax({key: [
            {}, {"id": 9, "xRestrict": 1},
            *({"id": 11 + kind, "illustType": kind} for kind in (0, 1, 2)),
            {"id": 14},
        ], "nextIds": [100]})

    async def scenario():
        """查询两类相关接口并切换限制级开关核对结果"""
        client = make_client(handler, pixiv_r18=False)
        try:
            for kind in ("image", "manga", "novel"):
                items = await client.get_related(kind, 42, 3)
                assert [item.id for item in items] == [11, 12, 13]
                if kind == "novel":
                    assert all(isinstance(item, Novel) for item in items)
                else:
                    assert [item.type for item in items] == ["image", "manga", "image"]
                    assert items[2].is_ugoira
            client.config.pixiv_r18 = True
            assert [item.id for item in await client.get_related("image", 42, 3)] == [9, 11, 12]
        finally:
            await client.close()

    asyncio.run(scenario())
    assert [request.url.path for request in seen] == [
        f"/ajax/{route}/42/recommend/init" for route in ("illust", "illust", "novel", "illust")
    ]
    assert all(request.url.params["limit"] == "3" for request in seen)


def test_search_paginates_without_dropping_or_repeating_results() -> None:
    """验证搜索结果跨越每页 60 条的分页边界"""
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        """按页码提供首页 60 条及次页 3 条模拟预览

        Args:
            request: 模拟传输层收到的 HTTP 请求

        Returns:
            当前页的作品预览及搜索总数
        """
        page = int(request.url.params["p"])
        pages.append(page)
        start = 0 if page == 1 else 60
        count = 60 if page == 1 else 3
        items = [
            {"id": str(index), "title": str(index), "userId": 1, "xRestrict": 0}
            for index in range(start, start + count)
        ]
        return ajax({"illust": {"data": items, "total": 63}})

    async def scenario() -> None:
        """验证跨两页查询后仅保留请求数量的作品预览"""
        client = make_client(handler)
        try:
            results = await client.search_artworks("画", 61)
            assert [item.id for item in results.items] == list(range(61))
            assert results.page == 2
            assert results.has_next is False
        finally:
            await client.close()

    asyncio.run(scenario())
    assert pages == [1, 2]


def test_artwork_novel_series_and_original_image() -> None:
    """验证作品类型和原图地址，以及小说正文和系列分页"""
    offsets = []

    def handler(request: httpx.Request) -> httpx.Response:
        """按请求路径提供作品详情和图片模拟响应

        Args:
            request: 模拟传输层收到的 HTTP 请求

        Returns:
            对应接口的详情响应或原图字节响应

        Raises:
            AssertionError: 请求路径不属于当前测试支持的接口
        """
        path = request.url.path
        if path == "/ajax/illust/123":
            return ajax(
                {
                    "illustId": "123",
                    "illustType": 1,
                    "title": "章节",
                    "userId": "8",
                    "userName": "绘者",
                    "tags": {"tags": [{"tag": "标签"}]},
                    "pageCount": 2,
                    "xRestrict": 0,
                }
            )
        if path == "/ajax/illust/123/pages":
            return ajax(
                [
                    {"urls": {"original": "https://i.pximg.net/a.jpg"}},
                    {"urls": {"original": "https://i.pximg.net/b.jpg"}},
                ]
            )
        if path == "/touch/ajax/novel/details":
            return ajax(
                {
                    "novel_details": {
                        "id": 88,
                        "title": "第一章",
                        "user_id": 9,
                        "user_name": "写者",
                        "tags": ["文字"],
                        "x_restrict": 0,
                        "text": "第一行\n第二行",
                        "series": {"id": 99, "title": "系列"},
                    }
                }
            )
        if path == "/ajax/novel/series/99":
            return ajax(
                {
                    "title": "系列",
                    "userId": 9,
                    "userName": "写者",
                    "tags": ["文字"],
                    "xRestrict": 0,
                    "publishedContentCount": 32,
                }
            )
        if path == "/ajax/novel/series_content/99":
            offset = int(request.url.params["last_order"])
            offsets.append(offset)
            ids = range(offset + 1, min(offset + 31, 33))
            items = [{"id": id_, "title": str(id_), "userId": 9} for id_ in ids]
            return ajax(
                {
                    "page": {"seriesContents": [{"id": id_} for id_ in ids]},
                    "thumbnails": {"novel": items},
                }
            )
        if request.url.host == "i.pximg.net":
            assert "cookie" not in request.headers
            assert request.headers["referer"] == "https://www.pixiv.net/"
            return httpx.Response(200, content=b"image", headers={"content-type": "image/jpeg"})
        raise AssertionError(path)

    async def scenario() -> None:
        """通过共享模拟客户端验证详情查询和原图下载"""
        client = make_client(handler)
        try:
            artwork = await client.get_illust(123)
            urls = await client.get_illust_pages(123)
            novel = await client.get_novel(88)
            series = await client.get_novel_series(99, 31)
            image = await client.download_image(urls[0])
            assert artwork.type == "manga"
            assert artwork.tags == ["标签"]
            assert urls == ["https://i.pximg.net/a.jpg", "https://i.pximg.net/b.jpg"]
            assert novel.content == "第一行\n第二行"
            assert novel.series_id == 99
            assert series.total == 32
            assert [chapter.id for chapter in series.chapters] == list(range(1, 32))
            assert image == b"image"
        finally:
            await client.close()

    asyncio.run(scenario())
    assert offsets == [0, 30]


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(404), PixivNotFoundError),
        (httpx.Response(403), PixivAuthError),
        (httpx.Response(503), PixivAPIError),
        (httpx.Response(200, content=b"{"), PixivAPIError),
        (ajax({}, error=True), PixivAPIError),
        (
            httpx.Response(
                200,
                json={"error": True, "message": "作品が見つかりませんでした", "body": None},
            ),
            PixivNotFoundError,
        ),
        (
            httpx.Response(200, json={"error": True, "message": "ログインしてください"}),
            PixivAuthError,
        ),
        ("timeout", PixivNetworkError),
    ],
)
def test_api_errors(response: httpx.Response | str, expected: type[Exception]) -> None:
    """验证资源缺失和认证失败等接口异常的分类

    Args:
        response: 模拟的 HTTP 响应或触发超时的标记
        expected: 当前失败场景应抛出的插件异常类型
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """提供失败响应或模拟读取超时

        Args:
            request: 模拟传输层收到的 HTTP 请求

        Returns:
            当前测试预设的失败 HTTP 响应

        Raises:
            httpx.ReadTimeout: 当前场景要求模拟请求超时
        """
        if response == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        return response

    async def scenario() -> None:
        """查询预设失败的作品详情并检查异常类型"""
        client = make_client(handler)
        try:
            with pytest.raises(expected):
                await client.get_illust(1)
        finally:
            await client.close()

    asyncio.run(scenario())


def test_r18_detail_rejected_when_disabled() -> None:
    """验证原图下载前按配置拦截限制级作品"""
    assert Config().pixiv_r18 is True

    def handler(request: httpx.Request) -> httpx.Response:
        """提供限制级插画的模拟详情

        Args:
            request: 模拟传输层收到的 HTTP 请求

        Returns:
            带有限制级标记的作品详情响应
        """
        return ajax({"illustId": 1, "illustType": 0, "xRestrict": 1})

    async def scenario() -> None:
        """验证关闭 R18 后拒绝限制级作品详情"""
        client = make_client(handler, pixiv_r18=False)
        try:
            with pytest.raises(PixivR18Error):
                await client.get_illust(1)
        finally:
            await client.close()

    asyncio.run(scenario())


def test_missing_cookie_is_auth_error_before_network() -> None:
    """验证 Cookie 缺失时在发送网络请求前拒绝操作"""

    def handler(request: httpx.Request) -> httpx.Response:
        """在缺少 Cookie 时捕获意外发出的网络请求

        Args:
            request: 模拟传输层收到的 HTTP 请求

        Raises:
            AssertionError: 缺少 Cookie 的客户端仍发出了请求
        """
        raise AssertionError(request.url)

    async def scenario() -> None:
        """在未配置 Cookie 时请求作品详情并检查认证异常"""
        client = PixivClient(Config(), transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(PixivAuthError):
                await client.get_illust(1)
        finally:
            await client.close()

    asyncio.run(scenario())


def test_fixed_ip_keeps_tls_hostname_verification() -> None:
    """验证固定 TCP 目标地址且保持 TLS 域名校验"""
    backend = FixedIPBackend("210.140.92.183")
    connect = AsyncMock(return_value="stream")
    backend.backend = SimpleNamespace(connect_tcp=connect)

    async def scenario() -> None:
        """验证固定 IP 仅影响 Pixiv 主站且证书校验保持开启"""
        assert await backend.connect_tcp("www.pixiv.net", 443) == "stream"
        assert await backend.connect_tcp("i.pximg.net", 443) == "stream"
        transport = FixedIPTransport("210.140.92.183")
        try:
            assert transport._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
            assert transport._pool._ssl_context.check_hostname
        finally:
            await transport.aclose()

    asyncio.run(scenario())
    assert connect.call_args_list[0].args[:2] == ("210.140.92.183", 443)
    assert connect.call_args_list[1].args[:2] == ("i.pximg.net", 443)


def test_image_rejects_untrusted_urls_and_redirects() -> None:
    """验证图片请求仅访问受信任的 Pixiv 图片域名"""

    def handler(request: httpx.Request) -> httpx.Response:
        """提供指向非 Pixiv 域名的模拟重定向

        Args:
            request: 模拟传输层收到的 HTTP 请求

        Returns:
            带有外部跳转地址的 HTTP 重定向响应
        """
        assert request.url.host == "i.pximg.net"
        return httpx.Response(302, headers={"location": "https://example.org/image.jpg"})

    async def scenario() -> None:
        """验证拒绝外部图片地址及图片服务器重定向"""
        client = make_client(handler)
        try:
            with pytest.raises(PixivAPIError):
                await client.download_image("https://example.org/image.jpg")
            with pytest.raises(PixivAPIError):
                await client.download_image("https://i.pximg.net/image.jpg")
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("metadata", "page", "count", "expected"),
    [
        ({"total": 61}, 1, 1, True),
        ({"total": 60}, 1, 60, False),
        ({"total": 120}, 2, 60, False),
        ({"total": 121}, 2, 1, True),
        ({"total": 999, "lastPage": 2}, 2, 60, False),
        ({"lastPage": 3}, 2, 1, True),
        ({"total": "121"}, 2, 1, True),
        ({"total": 0}, 1, 0, False),
        ({"total": 999}, 2, 0, False),
        ({}, 1, 60, True),
        ({}, 1, 59, False),
        ({}, 1, 1, False),
        ({}, 1, 0, False),
    ],
)
def test_search_page_uses_metadata_before_raw_count(
    metadata: dict, page: int, count: int, expected: bool
) -> None:
    """验证下一页优先依据分页元数据而不是配置上限或过滤后数量

    Args:
        metadata: 当前接口返回的总数或末页字段，空字典表示没有分页信息
        page: 当前请求的实际 Pixiv 页码
        count: 原始响应中的作品数量
        expected: 当前响应是否应允许继续下一页
    """
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        """记录请求并返回指定分页元数据

        Args:
            request: 含有真实页码参数的搜索请求

        Returns:
            包含原始数量和场景分页字段的模拟响应
        """
        requests.append(request)
        items = [{"id": index + 1} for index in range(count)]
        return ajax({"illust": {"data": items, **metadata}})

    async def scenario():
        """保持每批上限为 1 并验证分页不依赖展示数量"""
        client = make_client(handler)
        try:
            result = await client.search_artworks("测试", 1, page)
            assert result.page == page
            assert result.has_next is expected
            assert len(result.items) == min(count, 1)
        finally:
            await client.close()

    asyncio.run(scenario())
    assert [request.url.params["p"] for request in requests] == [str(page)]


def test_search_cursor_tracks_last_native_page_for_large_batch() -> None:
    """验证跨页补足现有数量上限后从实际下一页继续且不混用转发分包"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        """按实际页码生成互不重叠的模拟作品

        Args:
            request: 指定当前 Pixiv 页码的搜索请求

        Returns:
            每页 60 条且总计 180 条的搜索响应
        """
        page = int(request.url.params["p"])
        seen.append(page)
        items = [{"id": (page - 1) * 60 + index} for index in range(1, 61)]
        return ajax({"illust": {"data": items, "total": 180}})

    async def scenario():
        """先读取跨两页的现有搜索批次再查询后续一页"""
        client = make_client(handler)
        try:
            first = await client.search_artworks("测试", 61)
            assert first.page == 2
            assert first.has_next
            assert [item.id for item in first.items] == list(range(1, 62))
            following = await client.search_artworks("测试", 61, first.page + 1)
            assert following.page == 3
            assert not following.has_next
            assert following.items[0].id == 121
        finally:
            await client.close()

    asyncio.run(scenario())
    assert seen == [1, 2, 3]


@pytest.mark.parametrize("metadata", [{"total": "无效"}, {"lastPage": []}])
def test_search_rejects_malformed_pagination(metadata: dict) -> None:
    """验证错误分页信息不会被当作成功结果

    Args:
        metadata: 当前模拟响应中无效的分页字段
    """
    async def scenario():
        """请求带无效元数据的搜索页并检查异常分类"""
        client = make_client(lambda request: ajax({"illust": {"data": [], **metadata}}))
        try:
            with pytest.raises(PixivAPIError, match="分页信息无效"):
                await client.search_artworks("测试", 20, 2)
        finally:
            await client.close()

    asyncio.run(scenario())

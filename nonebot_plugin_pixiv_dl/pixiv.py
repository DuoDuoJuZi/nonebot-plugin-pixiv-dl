import html
import re
import ssl
from http.cookies import SimpleCookie
from urllib.parse import quote, urlparse

import httpcore
import httpx

from .config import Config
from .models import Artwork, Novel, NovelSeries, SearchPage

PIXIV_HOST = "www.pixiv.net"
PIXIV_URL = f"https://{PIXIV_HOST}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# @Author: DuoDuoJuZi
# @Date: 2026-09-29
class PixivError(Exception):
    """表示 Pixiv 操作失败的基础异常"""


class PixivNetworkError(PixivError):
    """表示 Pixiv 请求的连接或超时异常"""


class PixivAPIError(PixivError):
    """表示 HTTP 状态或 Pixiv 业务响应异常"""


class PixivAuthError(PixivAPIError):
    """表示 Pixiv Cookie 缺失或登录认证失败"""


class PixivNotFoundError(PixivAPIError):
    """表示请求的 Pixiv 资源不存在"""


class PixivR18Error(PixivError):
    """表示限制级作品下载被配置拦截"""


class FixedIPBackend(httpcore.AsyncNetworkBackend):
    """将 Pixiv 主站的 TCP 连接路由至固定 IP，保留请求域名"""

    def __init__(self, ip: str) -> None:
        """创建固定 IP 路由后端，由 AnyIO 管理底层连接

        Args:
            ip: 用于连接 Pixiv 主站的固定 IP 地址
        """
        self.ip = ip
        self.backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> httpcore.AsyncNetworkStream:
        """建立 TCP 连接，仅替换 Pixiv 主站的连接地址

        Args:
            host: 请求的目标域名，Pixiv 主站使用配置的固定 IP
            port: 目标服务的 TCP 端口
            timeout: 连接等待上限，单位为秒，空值表示不限制
            local_address: 发起连接时绑定的本地地址，空值表示由系统选择
            socket_options: 传递给底层套接字的连接选项

        Returns:
            已建立的异步网络连接流
        """
        return await self.backend.connect_tcp(
            self.ip if host == PIXIV_HOST else host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: object = None,
    ) -> httpcore.AsyncNetworkStream:
        """通过标准后端连接 Unix 套接字

        Args:
            path: 目标 Unix 套接字的本地路径
            timeout: 连接等待上限，单位为秒，空值表示不限制
            socket_options: 传递给底层套接字的连接选项

        Returns:
            已建立的异步套接字连接流
        """
        return await self.backend.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )


class FixedIPTransport(httpx.AsyncHTTPTransport):
    """通过自定义 httpcore 连接后端为 HTTPX 提供固定 IP 传输"""

    def __init__(self, ip: str) -> None:
        """创建固定 IP 连接池，启用 TLS 证书和域名校验

        Args:
            ip: 用于连接 Pixiv 主站的固定 IP 地址
        """
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=FixedIPBackend(ip),
        )


def _tags(value: object) -> list[str]:
    """统一解析搜索和详情接口返回的标签列表

    Args:
        value: 接口返回的标签列表或包含标签列表的对象

    Returns:
        规范化后的标签文本列表，结构不受支持时返回空列表
    """
    if isinstance(value, dict):
        value = value.get("tags", [])
    if not isinstance(value, list):
        return []
    return [
        str(item.get("tag", item.get("name", "")) if isinstance(item, dict) else item)
        for item in value
    ]


def _description(value: object) -> str:
    """清除简介中的 HTML 标签并还原字符实体

    Args:
        value: 接口返回的作品简介，可为空

    Returns:
        去除标签和首尾空白后的简介文本
    """
    return html.unescape(re.sub(r"<[^>]*>", "", str(value or ""))).strip()


def _preview_url(item: dict) -> str | None:
    """从搜索响应选择第一页低清地址，跳过原图和未裁剪的常规图片

    Args:
        item: 搜索接口返回的单个作品对象，可能包含多种图片尺寸

    Returns:
        接口提供的可信预览地址，没有可用低清地址时返回 None
    """
    urls = item.get("urls")
    urls = urls if isinstance(urls, dict) else {}
    candidates = [item.get("url")]
    for key in ("thumb", "thumbnail", "small"):
        candidates.extend((item.get(key), urls.get(key)))
    for url in candidates:
        if not isinstance(url, str):
            continue
        try:
            parsed = urlparse(url)
            if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".pximg.net"):
                continue
        except ValueError:
            continue
        path = parsed.path.lower()
        if "img-original" in path or ("img-master" in path and not path.startswith("/c/")):
            continue
        page = re.search(r"_p(\d+)(?:[_.]|$)", path)
        if page and int(page.group(1)) != 0:
            continue
        return url
    return None


def _search_artwork(item: dict, kind: str) -> Artwork:
    """将搜索结果转换为包含低清预览地址的插画或漫画模型

    Args:
        item: 搜索接口返回的单个作品预览对象
        kind: 预览所属的插画或漫画分类

    Returns:
        可用于消息展示的作品元数据和第一页预览地址
    """
    return Artwork(
        id=int(item["id"]),
        title=str(item.get("title", "")),
        user_id=int(item.get("userId", 0)),
        user_name=str(item.get("userName", "")),
        tags=_tags(item.get("tags")),
        type=kind,
        page_count=int(item.get("pageCount") or 1),
        x_restrict=int(item.get("xRestrict") or 0),
        preview_url=_preview_url(item),
    )


def _search_novel(item: dict) -> Novel:
    """将搜索或章节预览转换为小说模型

    Args:
        item: 接口返回的单个小说预览对象

    Returns:
        包含系列信息的小说元数据，不包含正文
    """
    series_id = item.get("seriesId")
    return Novel(
        id=int(item["id"]),
        title=str(item.get("title", "")),
        user_id=int(item.get("userId", 0)),
        user_name=str(item.get("userName", "")),
        tags=_tags(item.get("tags")),
        x_restrict=int(item.get("xRestrict") or 0),
        description=_description(item.get("description")),
        series_id=int(series_id) if series_id else None,
        series_title=str(item.get("seriesTitle") or ""),
    )


class PixivClient:
    """复用 HTTPX 客户端完成 Pixiv 分页查询和图片下载"""

    def __init__(self, config: Config, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """创建共享请求客户端，将 Cookie 限定于 Pixiv 主站并配置统一代理

        Args:
            config: 请求凭据和网络路由等插件配置
            transport: 可选的自定义传输层，用于测试时替代真实网络请求
        """
        self.config = config
        cookies = httpx.Cookies()
        parsed = SimpleCookie()
        parsed.load(config.pixiv_cookie)
        for name, morsel in parsed.items():
            cookies.set(name, morsel.value, domain=PIXIV_HOST)
        mounts = (
            {PIXIV_URL: FixedIPTransport(config.pixiv_fixed_ip)}
            if config.pixiv_fixed_ip and not config.pixiv_proxy and transport is None
            else None
        )
        self.http = httpx.AsyncClient(
            cookies=cookies,
            headers={"User-Agent": USER_AGENT, "Referer": f"{PIXIV_URL}/"},
            timeout=config.pixiv_timeout,
            follow_redirects=True,
            transport=transport,
            mounts=mounts,
            proxy=config.pixiv_proxy if transport is None else None,
            trust_env=transport is None and not mounts and not config.pixiv_proxy,
        )

    async def close(self) -> None:
        """在 NoneBot 关闭时释放共享连接池"""
        await self.http.aclose()

    async def _json(self, path: str, params: dict | None = None) -> object:
        """请求 Pixiv Web AJAX 接口并校验响应

        Args:
            path: 相对于 Pixiv 主站的接口路径
            params: 接口查询参数，空值表示不附加查询参数

        Returns:
            通过校验的 Pixiv 响应业务数据

        Raises:
            PixivAuthError: 未配置 Cookie 或 Pixiv 要求重新登录
            PixivNotFoundError: 请求的资源不存在或响应没有业务数据
            PixivNetworkError: 请求超时或网络连接失败
            PixivAPIError: HTTP 请求失败或 Pixiv 返回无效业务数据
        """
        if not self.config.pixiv_cookie:
            raise PixivAuthError("Pixiv Cookie is not configured")
        try:
            response = await self.http.get(f"{PIXIV_URL}{path}", params=params)
            if response.status_code in (401, 403):
                raise PixivAuthError(f"{path}: HTTP {response.status_code}")
            if response.status_code == 404:
                raise PixivNotFoundError(f"{path}: HTTP 404")
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException as exc:
            raise PixivNetworkError(f"{path}: timeout") from exc
        except httpx.RequestError as exc:
            raise PixivNetworkError(f"{path}: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise PixivAPIError(f"{path}: HTTP {exc.response.status_code}") from exc
        except ValueError as exc:
            raise PixivAPIError(f"{path}: invalid JSON") from exc
        if not isinstance(data, dict):
            raise PixivAPIError(f"{path}: invalid response")
        if data.get("error"):
            message = str(data.get("message") or "Pixiv returned an error")
            if any(
                marker in message.lower()
                for marker in ("not found", "不存在", "見つかりません", "存在しません")
            ):
                raise PixivNotFoundError(f"{path}: {message}")
            if any(
                marker in message.lower() for marker in ("login", "log in", "ログイン", "未登录")
            ):
                raise PixivAuthError(f"{path}: {message}")
            raise PixivAPIError(f"{path}: {message}")
        body = data.get("body")
        if body is None:
            raise PixivNotFoundError(f"{path}: empty body")
        return body

    async def _search(self, kind: str, word: str, limit: int, page: int = 1) -> SearchPage:
        """分页查询指定分类作品，按配置过滤限制级内容

        Args:
            kind: 请求的作品分类
            word: 用于匹配作品标签的搜索关键词
            limit: 本次最多返回的搜索结果数
            page: 本批查询起始的 Pixiv 页码，从 1 开始

        Returns:
            按接口顺序排列的限量预览，实际最后读取页码和后续页标记

        Raises:
            PixivAuthError: 未配置 Cookie 或 Pixiv 要求重新登录
            PixivNotFoundError: 请求的资源不存在或响应没有业务数据
            PixivNetworkError: 请求超时或网络连接失败
            PixivAPIError: HTTP 请求失败或 Pixiv 返回无效业务数据
        """
        if page < 1 or limit < 1:
            raise PixivAPIError("搜索页码与结果上限必须大于 0")
        route, key, work_type = {
            "image": ("illustrations", "illust", "illust"),
            "manga": ("manga", "manga", "manga"),
            "novel": ("novels", "novel", None),
        }[kind]
        result: list[Artwork] | list[Novel] = []
        while len(result) < limit:
            params = {
                "word": word,
                "order": "date_d",
                "mode": "all" if self.config.pixiv_r18 else "safe",
                "s_mode": "s_tag",
                "p": page,
                "lang": "zh",
            }
            if work_type:
                params["type"] = work_type
            body = await self._json(f"/ajax/search/{route}/{quote(word, safe='')}", params)
            if not isinstance(body, dict) or not isinstance(body.get(key), dict):
                raise PixivAPIError(f"/ajax/search/{route}: invalid result")
            section = body[key]
            items = section.get("data")
            if not isinstance(items, list):
                raise PixivAPIError(f"/ajax/search/{route}: invalid items")
            try:
                if section.get("lastPage") is not None:
                    has_next = page < int(section["lastPage"])
                elif section.get("total") is not None:
                    has_next = page * 60 < int(section["total"])
                else:
                    has_next = len(items) >= 60
            except (TypeError, ValueError) as exc:
                raise PixivAPIError(f"/ajax/search/{route}: 分页信息无效") from exc
            has_next = bool(items) and has_next
            for item in items:
                illust_type = item.get("illustType")
                if kind == "image" and illust_type in (1, 2):
                    continue
                if kind == "manga" and illust_type not in (None, 1):
                    continue
                model = _search_novel(item) if kind == "novel" else _search_artwork(item, kind)
                if self.config.pixiv_r18 or not model.is_r18:
                    result.append(model)
                    if len(result) == limit:
                        break
            if not has_next or len(result) == limit or len(items) < 60:
                break
            page += 1
        return SearchPage(result, page, has_next)

    async def search_artworks(self, word: str, limit: int, page: int = 1) -> SearchPage:
        """查询插画搜索预览

        Args:
            word: 用于匹配作品标签的搜索关键词
            limit: 本次最多返回的搜索结果数
            page: 本批查询起始的 Pixiv 页码，从 1 开始

        Returns:
            不超过指定数量的插画元数据，最后读取页码和后续页标记

        Raises:
            PixivAuthError: 未配置 Cookie 或 Pixiv 要求重新登录
            PixivNotFoundError: 请求的资源不存在或响应没有业务数据
            PixivNetworkError: 请求超时或网络连接失败
            PixivAPIError: HTTP 请求失败或 Pixiv 返回无效业务数据
        """
        return await self._search("image", word, limit, page)

    async def search_manga(self, word: str, limit: int, page: int = 1) -> SearchPage:
        """查询漫画搜索预览

        Args:
            word: 用于匹配作品标签的搜索关键词
            limit: 本次最多返回的搜索结果数
            page: 本批查询起始的 Pixiv 页码，从 1 开始

        Returns:
            不超过指定数量的漫画元数据，最后读取页码和后续页标记

        Raises:
            PixivAuthError: 未配置 Cookie 或 Pixiv 要求重新登录
            PixivNotFoundError: 请求的资源不存在或响应没有业务数据
            PixivNetworkError: 请求超时或网络连接失败
            PixivAPIError: HTTP 请求失败或 Pixiv 返回无效业务数据
        """
        return await self._search("manga", word, limit, page)

    async def search_novels(self, word: str, limit: int, page: int = 1) -> SearchPage:
        """查询小说搜索预览

        Args:
            word: 用于匹配作品标签的搜索关键词
            limit: 本次最多返回的搜索结果数
            page: 本批查询起始的 Pixiv 页码，从 1 开始

        Returns:
            不超过指定数量的小说元数据，最后读取页码和后续页标记

        Raises:
            PixivAuthError: 未配置 Cookie 或 Pixiv 要求重新登录
            PixivNotFoundError: 请求的资源不存在或响应没有业务数据
            PixivNetworkError: 请求超时或网络连接失败
            PixivAPIError: HTTP 请求失败或 Pixiv 返回无效业务数据
        """
        return await self._search("novel", word, limit, page)

    async def get_illust(self, work_id: int) -> Artwork:
        """查询作品详情并识别插画或漫画类型

        Args:
            work_id: 待查询作品的 Pixiv ID

        Returns:
            通过限制级校验的作品元数据

        Raises:
            PixivAuthError: 未配置 Cookie 或 Pixiv 要求重新登录
            PixivNotFoundError: 请求的资源不存在或响应没有业务数据
            PixivNetworkError: 请求超时或网络连接失败
            PixivAPIError: 响应结构无效或作品为暂不支持的动图
            PixivR18Error: 作品属于限制级内容且配置禁止下载
        """
        path = f"/ajax/illust/{work_id}"
        body = await self._json(path)
        if not isinstance(body, dict):
            raise PixivAPIError(f"{path}: invalid artwork")
        if "illustType" not in body:
            raise PixivAPIError(f"{path}: missing artwork type")
        illust_type = int(body["illustType"])
        if illust_type == 2:
            raise PixivAPIError(f"{path}: animated artworks are unsupported")
        artwork = Artwork(
            id=int(body.get("illustId") or work_id),
            title=str(body.get("title") or ""),
            user_id=int(body.get("userId") or 0),
            user_name=str(body.get("userName") or ""),
            tags=_tags(body.get("tags")),
            type="manga" if illust_type == 1 else "image",
            page_count=int(body.get("pageCount") or 1),
            x_restrict=int(body.get("xRestrict") or 0),
            description=_description(body.get("description")),
        )
        self._check_r18(artwork)
        return artwork

    async def get_illust_pages(self, work_id: int) -> list[str]:
        """查询插画或漫画的逐页原图地址

        Args:
            work_id: 待查询作品的 Pixiv ID

        Returns:
            按作品页序排列的原图地址列表

        Raises:
            PixivAuthError: 未配置 Cookie 或 Pixiv 要求重新登录
            PixivNotFoundError: 请求的资源不存在或响应没有业务数据
            PixivNetworkError: 请求超时或网络连接失败
            PixivAPIError: HTTP 请求失败或 Pixiv 返回无效业务数据
        """
        path = f"/ajax/illust/{work_id}/pages"
        body = await self._json(path)
        if not isinstance(body, list):
            raise PixivAPIError(f"{path}: invalid pages")
        urls = [item.get("urls", {}).get("original") for item in body]
        if not urls or any(not isinstance(url, str) for url in urls):
            raise PixivAPIError(f"{path}: missing original URL")
        return urls

    async def get_novel(self, novel_id: int) -> Novel:
        """通过 Pixiv 移动端接口查询小说详情及完整正文

        Args:
            novel_id: 待查询小说的 Pixiv ID

        Returns:
            通过限制级校验且包含完整正文的小说详情

        Raises:
            PixivAuthError: 未配置 Cookie 或 Pixiv 要求重新登录
            PixivNotFoundError: 请求的资源不存在或响应没有业务数据
            PixivNetworkError: 请求超时或网络连接失败
            PixivAPIError: HTTP 请求失败或 Pixiv 返回无效业务数据
            PixivR18Error: 小说属于限制级内容且配置禁止下载
        """
        path = "/touch/ajax/novel/details"
        body = await self._json(path, {"novel_id": novel_id})
        if not isinstance(body, dict) or not isinstance(body.get("novel_details"), dict):
            raise PixivAPIError(f"{path}: invalid novel")
        details = body["novel_details"]
        series = details.get("series") or {}
        if "text" not in details:
            raise PixivAPIError(f"{path}: missing novel text")
        novel = Novel(
            id=int(details.get("id") or novel_id),
            title=str(details.get("title") or ""),
            user_id=int(details.get("user_id") or 0),
            user_name=str(details.get("user_name") or ""),
            tags=_tags(details.get("tags")),
            x_restrict=int(details.get("x_restrict") or 0),
            description=_description(details.get("comment")),
            series_id=int(series["id"]) if series.get("id") else None,
            series_title=str(series.get("title") or ""),
            content=html.unescape(str(details["text"])),
        )
        self._check_r18(novel)
        return novel

    async def get_novel_series(self, series_id: int, max_chapters: int) -> NovelSeries:
        """查询小说系列元数据并分页获取有序章节预览

        Args:
            series_id: 待查询小说系列的 Pixiv ID
            max_chapters: 本次最多获取的系列章节数

        Returns:
            包含系列信息和指定数量范围内章节预览的系列模型

        Raises:
            PixivAuthError: 未配置 Cookie 或 Pixiv 要求重新登录
            PixivNotFoundError: 请求的资源不存在或响应没有业务数据
            PixivNetworkError: 请求超时或网络连接失败
            PixivAPIError: HTTP 请求失败或 Pixiv 返回无效业务数据
            PixivR18Error: 系列或任一章节属于限制级内容且配置禁止下载
        """
        path = f"/ajax/novel/series/{series_id}"
        info = await self._json(path)
        if not isinstance(info, dict):
            raise PixivAPIError(f"{path}: invalid series")
        series = NovelSeries(
            id=series_id,
            title=str(info.get("title") or ""),
            user_id=int(info.get("userId") or 0),
            user_name=str(info.get("userName") or ""),
            tags=_tags(info.get("tags")),
            x_restrict=int(info.get("xRestrict") or 0),
            total=int(info.get("publishedContentCount") or info.get("total") or 0),
        )
        self._check_r18(series)
        offset = 0
        while len(series.chapters) < min(series.total, max_chapters):
            content_path = f"/ajax/novel/series_content/{series_id}"
            body = await self._json(
                content_path, {"limit": 30, "last_order": offset, "order_by": "asc"}
            )
            if not isinstance(body, dict):
                raise PixivAPIError(f"{content_path}: invalid chapters")
            previews = body.get("thumbnails", {}).get("novel", [])
            entries = body.get("page", {}).get("seriesContents") or previews
            if not isinstance(previews, list) or not isinstance(entries, list):
                raise PixivAPIError(f"{content_path}: invalid chapter list")
            if not entries:
                break
            by_id = {str(item["id"]): item for item in previews}
            for entry in entries:
                item = by_id.get(str(entry.get("id")), entry)
                chapter = _search_novel(item)
                self._check_r18(chapter)
                series.chapters.append(chapter)
                if len(series.chapters) == max_chapters:
                    break
            offset += len(entries)
        return series

    async def download_image(self, url: str) -> bytes:
        """从 Pixiv 图片服务器下载缩略图或原图

        Args:
            url: Pixiv 图片域名下由接口提供的 HTTPS 图片地址

        Returns:
            非空的图片二进制内容

        Raises:
            PixivAPIError: 地址不可信或 HTTP 状态和图片内容无效
            PixivNetworkError: 图片请求超时或网络连接失败
        """
        parsed = urlparse(url)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".pximg.net"):
            raise PixivAPIError("image: untrusted CDN URL")
        try:
            response = await self.http.get(url, follow_redirects=False)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise PixivNetworkError(f"image {url}: timeout") from exc
        except httpx.RequestError as exc:
            raise PixivNetworkError(f"image {url}: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise PixivAPIError(f"image {url}: HTTP {exc.response.status_code}") from exc
        content_type = response.headers.get("content-type", "")
        if not response.content or (content_type and not content_type.startswith("image/")):
            raise PixivAPIError(f"image {url}: invalid image response")
        return response.content

    def _check_r18(self, work: Artwork | Novel | NovelSeries) -> None:
        """根据 R18 配置检查作品是否允许下载

        Args:
            work: 需要检查限制级标记的作品或小说系列

        Raises:
            PixivR18Error: 作品属于限制级内容且配置禁止下载
        """
        if work.is_r18 and not self.config.pixiv_r18:
            raise PixivR18Error(f"{work.id}: R18 is disabled")

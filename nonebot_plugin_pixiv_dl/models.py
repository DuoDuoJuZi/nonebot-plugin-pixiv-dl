from dataclasses import dataclass, field
from pathlib import PureWindowsPath
from urllib.parse import urlparse


# @Author: DuoDuoJuZi
# @Date: 2026-09-29
@dataclass(slots=True)
class Artwork:
    """保存插画，漫画及动图的原始分类，元数据和图片地址"""

    id: int
    title: str
    user_id: int
    user_name: str
    tags: list[str]
    type: str
    page_count: int
    x_restrict: int
    description: str = ""
    urls: list[str] = field(default_factory=list)
    preview_url: str | None = None
    illust_type: int = 0

    @property
    def is_ugoira(self) -> bool:
        """判断作品是否为归入图片分类的 Pixiv 动图

        Returns:
            原始作品类型为 2 时返回 True，否则返回 False
        """
        return self.illust_type == 2

    @property
    def is_r18(self) -> bool:
        """判断 Pixiv 是否将插画或漫画标记为限制级内容

        Returns:
            限制级标记大于 0 时返回 True，否则返回 False
        """
        return self.x_restrict > 0


@dataclass(slots=True)
class Novel:
    """保存小说搜索结果和 TXT 文件生成所需的元数据及正文"""

    id: int
    title: str
    user_id: int
    user_name: str
    tags: list[str]
    x_restrict: int
    description: str = ""
    series_id: int | None = None
    series_title: str = ""
    content: str = ""

    @property
    def is_r18(self) -> bool:
        """判断 Pixiv 是否将小说标记为限制级内容

        Returns:
            限制级标记大于 0 时返回 True，否则返回 False
        """
        return self.x_restrict > 0


@dataclass(slots=True)
class NovelSeries:
    """保存小说系列元数据和按章节顺序排列的预览列表"""

    id: int
    title: str
    user_id: int
    user_name: str
    tags: list[str]
    x_restrict: int
    total: int
    chapters: list[Novel] = field(default_factory=list)

    @property
    def is_r18(self) -> bool:
        """判断 Pixiv 是否将小说系列标记为限制级内容

        Returns:
            限制级标记大于 0 时返回 True，否则返回 False
        """
        return self.x_restrict > 0


@dataclass(slots=True)
class SearchPage:
    """保存本批搜索预览，最后读取的 Pixiv 页码和后续页标记"""

    items: list[Artwork] | list[Novel]
    page: int
    has_next: bool


def validate_cdn_url(url: str) -> str:
    """校验资源地址属于可信的 Pixiv HTTPS 图片域名

    Args:
        url: Pixiv 接口提供的资源地址

    Returns:
        通过域名和协议校验的原始地址

    Raises:
        ValueError: 地址不是可信的 HTTPS 资源或带有认证信息
    """
    if not isinstance(url, str) or any(char.isspace() for char in url):
        raise ValueError("Pixiv CDN 地址无效")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not (parsed.hostname or "").endswith(".pximg.net")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or "\\" in url
    ):
        raise ValueError("Pixiv CDN 地址不可信")
    return url


@dataclass(slots=True, frozen=True)
class UgoiraFrame:
    """保存并校验单帧安全文件名和原始毫秒停留时间"""

    file: str
    delay: int

    def __post_init__(self) -> None:
        """拒绝路径穿越，平台特殊文件名及非正整数帧延时

        Raises:
            ValueError: 帧文件名不安全或停留时间不是正整数
        """
        if (
            not isinstance(self.file, str)
            or not self.file
            or self.file != self.file.strip(" .")
            or ".." in self.file
            or any(char in self.file for char in '/\\:*?"<>|\'')
            or any(ord(char) < 32 for char in self.file)
            or PureWindowsPath(self.file).is_reserved()
        ):
            raise ValueError("Ugoira 帧文件名不安全")
        if type(self.delay) is not int or self.delay <= 0:
            raise ValueError("Ugoira 帧延时必须为正整数毫秒")


@dataclass(slots=True, frozen=True)
class UgoiraMeta:
    """保存可信的预览与原始 ZIP 地址，以及按动画时序排列的全部帧"""

    src: str
    original_src: str
    mime_type: str
    frames: list[UgoiraFrame]

    def __post_init__(self) -> None:
        """校验资源地址及完整帧序列，拒绝异常规模和重复文件名

        Raises:
            ValueError: 地址不可信，帧数超出 1 至 10000 或帧列表无效
        """
        validate_cdn_url(self.src)
        validate_cdn_url(self.original_src)
        if not isinstance(self.frames, list) or not 1 <= len(self.frames) <= 10000:
            raise ValueError("Ugoira 帧数必须为 1 至 10000")
        if not all(isinstance(frame, UgoiraFrame) for frame in self.frames):
            raise ValueError("Ugoira 帧列表无效")
        if len({frame.file.casefold() for frame in self.frames}) != len(self.frames):
            raise ValueError("Ugoira 帧文件名重复")
        if self.mime_type not in ("image/jpeg", "image/png"):
            raise ValueError("Ugoira 帧格式不受支持")

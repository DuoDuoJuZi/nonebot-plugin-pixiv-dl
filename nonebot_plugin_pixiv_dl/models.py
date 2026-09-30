from dataclasses import dataclass, field


# @Author: DuoDuoJuZi
# @Date: 2026-09-29
@dataclass(slots=True)
class Artwork:
    """保存插画和漫画的元数据，搜索预览地址及下载图片地址"""

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

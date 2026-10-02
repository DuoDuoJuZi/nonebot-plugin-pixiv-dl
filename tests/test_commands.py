import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from nonebot_plugin_pixiv_dl import commands
from nonebot_plugin_pixiv_dl.models import Artwork, Novel, NovelSeries, SearchPage
from nonebot_plugin_pixiv_dl.pixiv import PixivNotFoundError


# @Author: DuoDuoJuZi
# @Date: 2026-09-29
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/px搜索图片 初音", ("image", "初音")),
        ("/px搜索漫画 初音", ("manga", "初音")),
        ("/px搜索小说 初音", ("novel", "初音")),
        ("/px搜索 初音", ("all", "初音")),
        (
            "/px搜索图片ゼンレスゾーンゼロ 5000users入り",
            ("image", "ゼンレスゾーンゼロ 5000users入り"),
        ),
        ("/px搜索图片绝区零", ("image", "绝区零")),
        ("/px搜索绝区零", ("all", "绝区零")),
        ("/px搜索 图片", ("all", "图片")),
        ("/px搜索图片", None),
        ("/px搜索   ", None),
    ],
)
def test_search_commands(text: str, expected: tuple[str, str] | None) -> None:
    """验证搜索命令兼容空格省略且分类不会被短命令截断

    Args:
        text: 待解析的搜索命令样例
        expected: 预期的分类与关键词，空值表示样例应被拒绝
    """
    assert commands.parse_search(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/px下载 123", ("auto", 123)),
        ("/px下载123", ("auto", 123)),
        ("/px下载图片 123", ("image", 123)),
        ("/px下载图片123", ("image", 123)),
        ("/px下载漫画 123", ("manga", 123)),
        ("/px下载漫画123", ("manga", 123)),
        ("/px下载小说 123", ("novel", 123)),
        ("/px下载小说123", ("novel", 123)),
        ("/px下载图片 abc", None),
    ],
)
def test_download_commands(text: str, expected: tuple[str, int] | None) -> None:
    """验证下载命令兼容空格省略并正确提取分类

    Args:
        text: 待解析的下载命令样例
        expected: 预期的分类与作品 ID，空值表示样例应被拒绝
    """
    assert commands.parse_download(text) == expected


def test_aggregate_search_sends_three_separate_forward_records(monkeypatch) -> None:
    """验证聚合搜索先撤回状态提示再发送三个分类的聊天记录

    Args:
        monkeypatch: 用于临时替换客户端方法和配置的 pytest 工具
    """
    actions = []

    class FakeBot:
        """记录聚合搜索中的提示消息和撤回操作"""

        self_id = "10"

        async def send(self, event, content):
            """记录搜索提示消息并提供可撤回的模拟消息标识

            Args:
                event: 触发命令的消息事件，决定回复会话
                content: 待记录的状态提示或错误提示

            Returns:
                包含模拟消息标识的发送结果
            """
            actions.append(("message", content))
            return {"message_id": 77}

        async def delete_msg(self, *, message_id):
            """记录搜索状态提示的撤回操作

            Args:
                message_id: 待撤回状态消息的 OneBot 消息标识
            """
            actions.append(("recall", message_id))

    async def search_kind(kind, word, page):
        """为指定搜索分类提供一条模拟预览

        Args:
            kind: 请求的作品分类
            word: 用于匹配作品标签的搜索关键词
            page: 本次请求的 Pixiv 搜索页码

        Returns:
            包含该分类单个作品和无后续页标记的搜索结果
        """
        assert word == "初音"
        if kind == "novel":
            return SearchPage([Novel(3, "小说", 2, "作者", [], 0)], page, False)
        return SearchPage([Artwork(1, kind, 2, "作者", [], kind, 1, 0)], page, False)

    async def forward(bot, event, packet):
        """记录搜索合并转发的分类名称

        Args:
            bot: 用于调用 OneBot 消息接口的机器人实例
            event: 触发命令的消息事件，决定回复会话
            packet: 按展示顺序排列的合并转发节点
        """
        actions.append(("forward", packet[0].data["nickname"]))

    monkeypatch.setattr(commands, "_search_kind", search_kind)
    monkeypatch.setattr(commands, "send_forward", forward)
    event = SimpleNamespace(message_type="private", get_session_id=lambda: "1")
    asyncio.run(commands._run_search(FakeBot(), event, "all", "初音"))
    assert actions == [
        ("message", "正在搜索"),
        ("recall", 77),
        ("forward", "Pixiv 图片"),
        ("forward", "Pixiv 漫画"),
        ("forward", "Pixiv 小说"),
    ]


def test_auto_download_uses_api_not_id_shape(monkeypatch) -> None:
    """验证插画接口返回 404 后按小说接口识别资源

    Args:
        monkeypatch: 用于临时替换客户端方法和配置的 pytest 工具
    """
    calls = []

    async def get_illust(work_id):
        """模拟查询作品时插画资源不存在

        Args:
            work_id: 待查询作品的 Pixiv ID

        Raises:
            PixivNotFoundError: 该作品 ID 没有对应插画
        """
        calls.append(("artwork", work_id))
        raise PixivNotFoundError("missing")

    async def get_novel(work_id):
        """提供与查询 ID 对应的模拟小说

        Args:
            work_id: 待查询作品的 Pixiv ID

        Returns:
            具有查询 ID 的小说元数据
        """
        calls.append(("novel", work_id))
        return Novel(work_id, "小说", 2, "作者", [], 0)

    monkeypatch.setattr(
        commands.client,
        "get_illust",
        get_illust,
    )
    monkeypatch.setattr(commands.client, "get_novel", get_novel)
    target = asyncio.run(commands._get_download_target("auto", 123))
    assert isinstance(target, Novel)
    assert calls == [("artwork", 123), ("novel", 123)]


@pytest.mark.parametrize("work_type", ["image", "manga"])
def test_artwork_download_recalls_status_then_sends_forward(monkeypatch, work_type: str) -> None:
    """验证图片下载在撤回状态提示后按页打包发送

    Args:
        monkeypatch: 用于临时替换客户端方法和配置的 pytest 工具
        work_type: 本次测试使用的插画或漫画分类
    """
    actions = []
    work = Artwork(9, "作品", 2, "作者", [], work_type, 3, 0)

    class FakeBot:
        """记录图片下载流程中的提示消息和撤回顺序"""

        self_id = "10"

        async def send(self, event, content):
            """记录下载开始提示或错误提示

            Args:
                event: 触发命令的消息事件，决定回复会话
                content: 待记录的状态提示或错误提示

            Returns:
                包含模拟消息标识的发送结果
            """
            actions.append(("message", content))
            return {"message_id": 5}

        async def delete_msg(self, *, message_id):
            """记录图片发送前的状态提示撤回操作

            Args:
                message_id: 待撤回状态消息的 OneBot 消息标识
            """
            actions.append(("recall", message_id))

    async def target(kind, work_id):
        """模拟按下载命令查询到插画或漫画资源

        Args:
            kind: 请求的作品分类
            work_id: 待查询作品的 Pixiv ID

        Returns:
            当前测试使用的作品详情
        """
        return work

    async def pages(work_id):
        """提供当前作品的模拟原图地址

        Args:
            work_id: 待查询作品的 Pixiv ID

        Returns:
            按页序排列的三个原图地址
        """
        return [f"https://i.pximg.net/{index}.jpg" for index in range(3)]

    async def image(url):
        """记录图片下载请求并提供模拟图片内容

        Args:
            url: 本次请求的 Pixiv 原图地址

        Returns:
            用于消息构造测试的图片字节内容
        """
        actions.append(("download", url))
        return b"image"

    async def forward(bot, event, packet):
        """记录每个图片合并转发的节点数量

        Args:
            bot: 用于调用 OneBot 消息接口的机器人实例
            event: 触发命令的消息事件，决定回复会话
            packet: 按展示顺序排列的合并转发节点
        """
        actions.append(("forward", len(packet)))

    monkeypatch.setattr(commands, "_get_download_target", target)
    monkeypatch.setattr(commands.client, "get_illust_pages", pages)
    monkeypatch.setattr(commands.client, "download_image", image)
    monkeypatch.setattr(commands, "send_forward", forward)
    monkeypatch.setattr(commands.config, "pixiv_forward_max_messages", 3)
    asyncio.run(commands._run_download(FakeBot(), SimpleNamespace(), work_type, 9))
    assert actions[0] == ("message", "正在下载")
    assert [item[0] for item in actions[1:4]] == ["download"] * 3
    assert actions[4] == ("recall", 5)
    assert actions[5:] == [("forward", 3), ("forward", 2)]


def test_novel_series_download_writes_ordered_files_and_cleans_them(monkeypatch) -> None:
    """验证系列章节顺序和 TXT 生成，以及状态撤回与临时文件清理

    Args:
        monkeypatch: 用于临时替换客户端方法和配置的 pytest 工具
    """
    actions = []
    paths = []
    first = Novel(1, "入口", 2, "作者", [], 0, series_id=55, series_title="系列")
    series = NovelSeries(
        55,
        "系列",
        2,
        "作者",
        [],
        0,
        3,
        chapters=[Novel(9, "九", 2, "作者", [], 0), Novel(10, "十", 2, "作者", [], 0)],
    )

    class FakeBot:
        """记录小说下载流程中的提示消息和撤回操作"""

        self_id = "10"

        async def send(self, event, content):
            """记录小说下载提示消息

            Args:
                event: 触发命令的消息事件，决定回复会话
                content: 待记录的状态提示或错误提示

            Returns:
                包含模拟消息标识的发送结果
            """
            actions.append(("message", content))
            return {"message_id": 5}

        async def delete_msg(self, *, message_id):
            """记录小说下载状态提示的撤回操作

            Args:
                message_id: 待撤回状态消息的 OneBot 消息标识
            """
            actions.append(("recall", message_id))

    async def target(kind, work_id):
        """模拟下载命令查询到系列中的小说

        Args:
            kind: 请求的作品分类
            work_id: 待查询作品的 Pixiv ID

        Returns:
            具有系列关联信息的入口小说
        """
        return first

    async def get_series(series_id, max_chapters):
        """提供含两个有序章节预览的模拟小说系列

        Args:
            series_id: 待查询小说系列的 Pixiv ID
            max_chapters: 本次最多获取的系列章节数

        Returns:
            当前测试使用的系列元数据及章节预览
        """
        assert series_id == 55
        return series

    async def get_novel(work_id):
        """提供带有正文的模拟章节详情

        Args:
            work_id: 待查询作品的 Pixiv ID

        Returns:
            正文中标明章节 ID 的小说详情
        """
        return Novel(work_id, str(work_id), 2, "作者", [], 0, content=f"正文 {work_id}")

    async def forward(bot, event, packet):
        """在临时文件清理前检查小说转发节点及正文

        Args:
            bot: 用于调用 OneBot 消息接口的机器人实例
            event: 触发命令的消息事件，决定回复会话
            packet: 按展示顺序排列的合并转发节点
        """
        actions.append(("forward", packet[0].data["content"]))
        paths.extend(Path(node.data["content"][0].data["file"]) for node in packet[1:])
        assert [path.read_text(encoding="utf-8").splitlines()[-1] for path in paths] == [
            "正文 9",
            "正文 10",
        ]

    monkeypatch.setattr(commands, "_get_download_target", target)
    monkeypatch.setattr(commands.client, "get_novel_series", get_series)
    monkeypatch.setattr(commands.client, "get_novel", get_novel)
    monkeypatch.setattr(commands, "send_forward", forward)
    asyncio.run(commands._run_download(FakeBot(), SimpleNamespace(), "novel", 1))
    assert [path.name[:2] for path in paths] == ["01", "02"]
    assert actions[0] == ("message", "正在下载")
    assert "系列总章节：3" in actions[1][1]
    assert "本次下载：2" in actions[1][1]
    assert actions[2] == ("recall", 5)
    assert all(not path.exists() for path in paths)

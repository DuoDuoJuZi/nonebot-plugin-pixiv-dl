import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

from nonebot_plugin_pixiv_dl import commands, pagination
from nonebot_plugin_pixiv_dl.config import Config
from nonebot_plugin_pixiv_dl.models import Artwork, Novel, SearchPage
from nonebot_plugin_pixiv_dl.pixiv import PixivClient, PixivNetworkError


# @Author: DuoDuoJuZi
# @Date: 2026-10-02
class SearchHarness:
    """通过真实命令处理器验证分页与编号下载，使用模拟时钟与可控搜索结果"""

    def __init__(self, monkeypatch) -> None:
        """隔离查询，下载与消息接口并安装可直接推进的时钟

        Args:
            monkeypatch: 临时替换客户端和时钟的 pytest 工具
        """
        self.now = 1000.0
        self.calls = []
        self.ends = {}
        self.failures = {}
        self.gates = {}
        self.send = AsyncMock(return_value={"message_id": 77})
        self.recall = AsyncMock()
        self.forward = AsyncMock()
        self.download = AsyncMock()
        monkeypatch.setattr(pagination, "monotonic", lambda: self.now)
        monkeypatch.setattr(commands, "_search_kind", self.search)
        monkeypatch.setattr(commands, "send_forward", self.forward)
        monkeypatch.setattr(commands, "_run_download", self.download)
        monkeypatch.setattr(commands.config, "pixiv_search_preview", False)

    async def search(self, kind: str, word: str, page: int) -> SearchPage:
        """记录真实请求页码并按当前场景返回成功或失败

        Args:
            kind: 本次请求的搜索分类
            word: 本次使用的搜索关键词
            page: 本次向客户端传入的实际页码

        Returns:
            带有页码和可配置后续页标记的模拟搜索结果

        Raises:
            Exception: 当前请求命中场景预设的异常
        """
        self.calls.append((kind, word, page))
        if gate := self.gates.get((word, kind, page)):
            entered, release = gate
            entered.set()
            await release.wait()
        await asyncio.sleep(0)
        if error := self.failures.get((kind, page)):
            raise error
        item = (
            Novel(page, "小说", 2, "作者", [], 0)
            if kind == "novel"
            else Artwork(page, "画作", 2, "作者", [], kind, 1, 0)
        )
        return SearchPage([item], page, page < self.ends.get(kind, 10))

    async def run(
        self, text: str, user: int = 1, group: int | None = 100, bot_id: str = "10"
    ) -> None:
        """构造真实 OneBot 事件并交给搜索，分页，下载或相关查询处理器

        Args:
            text: 包含命令和可选关键词的用户消息
            user: 用于验证用户隔离的 QQ 号
            group: 用于验证群聊隔离的群号，空值表示私聊
            bot_id: 用于验证机器人隔离的登录账号
        """
        fields = {
            "time": 0,
            "self_id": int(bot_id),
            "post_type": "message",
            "sub_type": "normal" if group else "friend",
            "user_id": user,
            "message_type": "group" if group else "private",
            "message_id": 1,
            "message": text,
            "raw_message": text,
            "font": 0,
            "sender": {},
        }
        event = (
            GroupMessageEvent(**fields, group_id=group) if group else PrivateMessageEvent(**fields)
        )
        bot = SimpleNamespace(self_id=bot_id, send=self.send, delete_msg=self.recall)
        if commands.DOWNLOAD_RE.fullmatch(text):
            handler = commands.handle_download
        elif commands.RELATED_RE.fullmatch(text):
            handler = commands.handle_related
        elif commands.NEXT_RE.fullmatch(text):
            handler = commands.handle_next
        else:
            handler = commands.handle_search
        await handler(bot, event)

    def session(self, user: int = 1, group: int | None = 100, bot_id: str = "10"):
        """读取指定测试上下文的内存会话

        Args:
            user: 发起搜索的 QQ 号
            group: 搜索所在的群号，空值表示私聊
            bot_id: 处理搜索的机器人账号

        Returns:
            对应用户与聊天环境的分页记录
        """
        key = (
            (bot_id, "group", f"group_{group}_{user}") if group else (bot_id, "private", str(user))
        )
        return pagination.sessions[key]

    def last_message(self) -> str:
        """读取最近一次发送的普通消息

        Returns:
            用于验证错误提示与分页提示的消息文本
        """
        return self.send.call_args.args[1]


@pytest.fixture
def harness(monkeypatch) -> SearchHarness:
    """提供每项测试独立使用的命令测试环境

    Args:
        monkeypatch: 用于隔离时钟与外部接口的 pytest 工具

    Returns:
        可执行命令并检查请求和会话的模拟环境
    """
    return SearchHarness(monkeypatch)


@pytest.mark.parametrize("kind", ["", "图片", "漫画", "小说"])
def test_next_commands_do_not_match_search(kind: str) -> None:
    """验证分页命令不会被普通搜索 matcher 抢占

    Args:
        kind: 当前验证的分类分支，空字符串表示通用下一页
    """
    text = f"/px搜索{kind}下一页"
    assert commands.NEXT_RE.fullmatch(text)
    assert not commands.SEARCH_RE.search(text)
    assert commands.parse_search(text) is None
    assert not commands.NEXT_RE.fullmatch(text + " 关键词")
    assert commands.next_matcher.priority < commands.search_matcher.priority
    assert commands.parse_search(f"/px搜索{kind} 下一页") is not None


@pytest.mark.parametrize(
    ("kind", "next_command"),
    [("图片", "/px搜索下一页"), ("小说", "/px搜索小说下一页"), ("漫画", "/px搜索漫画下一页")],
)
def test_single_category_pages(harness, kind: str, next_command: str) -> None:
    """验证单分类连续翻页保留旧编号并复用下载入口

    Args:
        harness: 记录请求与状态的命令测试环境
        kind: 初次搜索使用的分类名称
        next_command: 本次连续翻页使用的命令
    """

    async def scenario():
        """执行同一分类的连续翻页后下载第一页和第三页结果"""
        await harness.run(f"/px搜索{kind} 关键词")
        await harness.run(next_command)
        await harness.run("/px搜索下一页")
        await harness.run("/px下载 1")
        await harness.run("/px下载3")

    asyncio.run(scenario())
    assert harness.calls == [(commands.KINDS[kind], "关键词", page) for page in (1, 2, 3)]
    assert harness.forward.call_args.args[2][-1].data["content"] == (
        f"[提示]\n{kind}还有下一页，可使用 /px搜索下一页 继续搜索"
    )
    assert all(call.args[1] == "正在搜索" for call in harness.send.await_args_list)
    assert harness.recall.await_count == 3
    assert harness.forward.await_count == 3
    assert harness.session().results == {
        index: pagination.SearchResultRef(commands.KINDS[kind], index) for index in (1, 2, 3)
    }
    assert harness.session().next_result_index == 4
    assert [call.args[2:] for call in harness.download.await_args_list] == [
        (commands.KINDS[kind], 1),
        (commands.KINDS[kind], 3),
    ]
    assert [
        call.args[2][0].data["content"].splitlines()[0]
        for call in harness.forward.await_args_list
    ] == ["[1]", "[2]", "[3]"]


@pytest.mark.parametrize(
    ("capacity", "count", "packet_sizes"),
    [(20, 20, [21]), (99, 99, [100]), (20, 45, [21, 21, 6]), (99, 100, [100, 2])],
)
@pytest.mark.parametrize("has_next", [False, True])
def test_search_packets_reserve_status_node(
    harness, monkeypatch, capacity: int, count: int, packet_sizes: list[int], has_next: bool
) -> None:
    """验证搜索容量只计算作品，每包尾部状态不影响连续编号和作品引用

    Args:
        harness: 记录请求与状态的命令测试环境
        monkeypatch: 替换搜索结果及分包容量的 pytest 工具
        capacity: 每包允许展示的作品数量
        count: 本次搜索实际返回的作品数量
        packet_sizes: 含尾部状态节点的各包预期总节点数
        has_next: 当前分类是否仍有后续 Pixiv 页
    """
    items = [
        Artwork(index, "画作", 2, "作者", [], "image", 1, 0) for index in range(101, 101 + count)
    ]
    monkeypatch.setattr(commands.config, "pixiv_forward_max_messages", capacity)
    monkeypatch.setattr(
        commands, "_search_kind", AsyncMock(return_value=SearchPage(items, 1, has_next))
    )
    asyncio.run(harness.run("/px搜索图片 关键词"))
    packets = [call.args[2] for call in harness.forward.await_args_list]
    assert [len(packet) for packet in packets] == packet_sizes
    assert all(len(packet) <= 100 for packet in packets)
    assert all(
        packet[-1].data["content"] == "[提示]\n本次结果还有后续分包，将自动发送"
        for packet in packets[:-1]
    )
    assert packets[-1][-1].data["content"] == (
        "[提示]\n图片还有下一页，可使用 /px搜索下一页 继续搜索"
        if has_next else "[提示]\n图片已是最后一页"
    )
    assert [node.data["content"].splitlines()[0] for packet in packets for node in packet[:-1]] == [
        f"[{index}]" for index in range(1, count + 1)
    ]
    assert harness.session().results == {
        index: pagination.SearchResultRef("image", item.id) for index, item in enumerate(items, 1)
    }
    assert harness.session().next_result_index == count + 1
    assert [call.args[1] for call in harness.send.await_args_list] == ["正在搜索"]


def test_result_numbers_commit_only_sent_packets(harness, monkeypatch) -> None:
    """验证部分分包失败和空分类不占用编号，已发送结果下载时不持有会话锁

    Args:
        harness: 记录请求与状态的命令测试环境
        monkeypatch: 用于提供多分包搜索结果的 pytest 工具
    """
    items = [Artwork(index, "画作", 2, "作者", [], "image", 1, 0) for index in range(10, 15)]
    monkeypatch.setattr(commands.config, "pixiv_forward_max_messages", 2)
    monkeypatch.setattr(
        commands, "_search_kind", AsyncMock(side_effect=[
            SearchPage(items, 1, True),
            SearchPage([], 1, False),
            SearchPage([Novel(30, "小说", 2, "作者", [], 0)], 1, False),
        ])
    )

    async def forward(bot, event, packet):
        """在发送完成前检查尚未登记编号，并模拟第二个分包发送失败

        Args:
            bot: 用于发送当前分包的机器人实例
            event: 决定搜索会话的消息事件
            packet: 当前准备发送的搜索节点列表

        Raises:
            RuntimeError: 第二个分包模拟发送失败
        """
        session = harness.session()
        assert packet[0].data["content"].startswith(f"[{session.next_result_index}]\n")
        assert packet[-1].data["content"].startswith("[提示]\n")
        assert len(packet) == (2 if harness.forward.await_count == 3 else 3)
        assert len(session.results) == session.next_result_index - 1
        if harness.forward.await_count == 2:
            raise RuntimeError("模拟发送失败")

    async def download(bot, event, kind, work_id):
        """验证下载入口使用索引或显式 ID 且搜索会话锁已经释放

        Args:
            bot: 用于发送下载结果的机器人实例
            event: 决定下载所在聊天环境的消息事件
            kind: 编号对应的作品分类
            work_id: 编号对应的 Pixiv ID
        """
        assert not harness.session().lock.locked()

    harness.forward.side_effect = forward
    harness.download.side_effect = download

    async def scenario():
        """下载成功发送的编号，缺失编号与显式分类始终按 ID 处理"""
        await harness.run("/px搜索 关键词")
        assert harness.session().results == {
            1: pagination.SearchResultRef("image", 10),
            2: pagination.SearchResultRef("image", 11),
            3: pagination.SearchResultRef("novel", 30),
        }
        assert harness.session().next_result_index == 4
        sent = harness.send.await_count
        await harness.run("/px下载 1")
        await harness.run("/px下载 4")
        for name in commands.KINDS:
            await harness.run(f"/px下载{name} 1")
        assert harness.send.await_count == sent

    asyncio.run(scenario())
    assert [call.args[2:] for call in harness.download.await_args_list] == [
        ("image", 10), ("auto", 4), ("image", 1), ("manga", 1), ("novel", 1)
    ]
    assert [
        call.args[2][0].data["content"].splitlines()[0]
        for call in harness.forward.await_args_list
    ] == ["[1]", "[3]", "[3]"]


def test_related_results_replace_context_and_can_be_followed(harness, monkeypatch) -> None:
    """验证索引相关查询不重复识别类型，新结果可连续查询与下载且从 1 编号

    Args:
        harness: 记录请求与状态的命令测试环境
        monkeypatch: 替换相关查询与类型识别接口的 pytest 工具
    """
    items = [
        Artwork(21, "图片", 2, "作者", [], "image", 1, 0),
        Artwork(22, "漫画", 2, "作者", [], "manga", 1, 0),
    ]
    novel = Novel(31, "小说", 2, "作者", [], 0)
    related = AsyncMock(side_effect=[items, items[:1], [novel]])
    target = AsyncMock(return_value=novel)
    monkeypatch.setattr(commands.client, "get_related", related)
    monkeypatch.setattr(commands, "_get_download_target", target)

    async def scenario():
        """从搜索索引连续查询相关作品，再使用未命中索引的小说 ID"""
        await harness.run("/px搜索漫画 关键词")
        old = harness.session()
        harness.now += 10
        await harness.run("/px相关1")
        session = harness.session()
        assert session is not old
        assert session.results == {
            1: pagination.SearchResultRef("image", 21),
            2: pagination.SearchResultRef("manga", 22),
        }
        assert session.expires_at == harness.now + 60
        assert not session.cursors
        await harness.run("/px下载 2")
        assert harness.download.call_args.args[2:] == ("manga", 22)
        await harness.run("/px相关 2")
        target.assert_not_awaited()
        await harness.run("/px相关 999")
        target.assert_awaited_once_with("auto", 999)
        assert harness.session().results == {1: pagination.SearchResultRef("novel", 31)}
        assert harness.session().next_result_index == 2

    asyncio.run(scenario())
    assert [call.args for call in related.await_args_list] == [
        ("manga", 1, commands.config.pixiv_search_limit),
        ("manga", 22, commands.config.pixiv_search_limit),
        ("novel", 999, commands.config.pixiv_search_limit),
    ]
    assert all(call.args[1] == "正在搜索" for call in harness.send.await_args_list)
    assert [len(call.args[2]) for call in harness.forward.await_args_list[1:]] == [2, 1, 1]
    assert all(
        not node.data["content"].startswith("[提示]")
        for call in harness.forward.await_args_list[1:] for node in call.args[2]
    )


@pytest.mark.parametrize("failure", ["request", "empty", "first_packet", "later_packet"])
def test_related_failure_keeps_last_visible_context(harness, monkeypatch, failure: str) -> None:
    """验证相关查询失败保留旧会话，部分发送成功时只登记已展示结果

    Args:
        harness: 记录请求与状态的命令测试环境
        monkeypatch: 替换相关查询接口及分包上限的 pytest 工具
        failure: 本次模拟的请求失败，空结果或分包发送失败
    """
    items = [Artwork(index, "画作", 2, "作者", [], "image", 1, 0) for index in (20, 21, 22)]
    related = AsyncMock(return_value=[] if failure == "empty" else items)
    if failure == "request":
        related.side_effect = PixivNetworkError("模拟超时")
    monkeypatch.setattr(commands.client, "get_related", related)
    monkeypatch.setattr(commands.config, "pixiv_forward_max_messages", 2)

    async def scenario():
        """在已有搜索记录上发起失败的相关查询并检查保留的会话"""
        await harness.run("/px搜索图片 关键词")
        old = harness.session()
        if failure.endswith("packet"):
            harness.forward.side_effect = (
                [None, RuntimeError("模拟发送失败")]
                if failure == "later_packet" else [RuntimeError("模拟发送失败")]
            )
        await harness.run("/px相关 1")
        if failure == "later_packet":
            assert harness.session() is not old
            assert harness.session().results == {
                1: pagination.SearchResultRef("image", 20),
                2: pagination.SearchResultRef("image", 21),
            }
            assert harness.session().next_result_index == 3
        else:
            assert harness.session() is old
        expected = "没有找到相关作品" if failure == "empty" else "相关作品获取失败"
        assert harness.last_message() == expected

    asyncio.run(scenario())


@pytest.mark.parametrize("from_aggregate", [False, True])
def test_locked_category_cannot_switch(harness, from_aggregate: bool) -> None:
    """验证单分类搜索和聚合收窄后均拒绝切换分类

    Args:
        harness: 记录请求与状态的命令测试环境
        from_aggregate: 是否先聚合搜索再锁定小说
    """

    async def scenario():
        """确认不匹配分类不会发送状态提示或发起查询"""
        await harness.run("/px搜索 关键词" if from_aggregate else "/px搜索小说 关键词")
        if from_aggregate:
            await harness.run("/px搜索小说下一页")
        calls = harness.calls.copy()
        recall_count = harness.recall.await_count
        for kind in ("图片", "漫画"):
            await harness.run(f"/px搜索{kind}下一页")
            assert "锁定为小说" in harness.last_message()
        assert harness.calls == calls
        assert harness.recall.await_count == recall_count
        assert list(harness.session().cursors) == ["novel"]

    asyncio.run(scenario())


@pytest.mark.parametrize("ended", [False, True])
def test_aggregate_next_skips_exhausted_categories(harness, ended: bool) -> None:
    """验证聚合翻页跳过耗尽分类并按发送顺序共享连续编号

    Args:
        harness: 记录请求与状态的命令测试环境
        ended: 是否让漫画在第 1 页耗尽
    """
    if ended:
        harness.ends["manga"] = 1

    async def scenario():
        """查询聚合前两批并检查提示中的可用分支"""
        await harness.run("/px搜索 关键词")
        assert [call.args[2][-1].data["content"] for call in harness.forward.await_args_list] == [
            "[提示]\n图片还有下一页，可使用 /px搜索下一页 或 /px搜索图片下一页",
            "[提示]\n漫画已是最后一页" if ended else (
                "[提示]\n漫画还有下一页，可使用 /px搜索下一页 或 /px搜索漫画下一页"
            ),
            "[提示]\n小说还有下一页，可使用 /px搜索下一页 或 /px搜索小说下一页",
        ]
        await harness.run("/px搜索下一页")

    asyncio.run(scenario())
    kinds = ["image", "novel"] if ended else ["image", "manga", "novel"]
    assert harness.calls == [
        *((kind, "关键词", 1) for kind in commands.KINDS.values()),
        *((kind, "关键词", 2) for kind in kinds),
    ]
    session = harness.session()
    assert session.kind == "all"
    assert all(call.args[1] == "正在搜索" for call in harness.send.await_args_list)
    references = [
        *(pagination.SearchResultRef(kind, 1) for kind in commands.KINDS.values()),
        *(pagination.SearchResultRef(kind, 2) for kind in kinds),
    ]
    assert session.results == dict(enumerate(references, 1))
    assert [
        call.args[2][0].data["content"].splitlines()[0]
        for call in harness.forward.await_args_list
    ] == [f"[{index}]" for index in range(1, len(references) + 1)]
    assert {kind: cursor.page for kind, cursor in session.cursors.items()} == {
        "image": 2,
        "manga": 1 if ended else 2,
        "novel": 2,
    }
    assert [call.args[2][0].data["nickname"] for call in harness.forward.await_args_list] == [
        "Pixiv 图片",
        "Pixiv 漫画",
        "Pixiv 小说",
        *(f"Pixiv {commands.KIND_NAMES[k]}" for k in kinds),
    ]


@pytest.mark.parametrize("aggregate_next", [False, True])
def test_aggregate_selects_novel_from_current_page(harness, aggregate_next: bool) -> None:
    """验证分类锁定后连续编号且保留全部分类已展示结果的下载入口

    Args:
        harness: 记录请求与状态的命令测试环境
        aggregate_next: 是否先完成一次聚合通用翻页
    """

    async def scenario():
        """锁定小说并翻页后仍能下载最初三个分类的结果"""
        await harness.run("/px搜索 关键词")
        if aggregate_next:
            await harness.run("/px搜索下一页")
        await harness.run("/px搜索小说下一页")
        assert harness.session().kind == "novel"
        assert list(harness.session().cursors) == ["novel"]
        await harness.run("/px搜索下一页")
        for index in (1, 2, 3):
            await harness.run(f"/px下载 {index}")

    asyncio.run(scenario())
    start = 3 if aggregate_next else 2
    assert harness.calls[-2:] == [("novel", "关键词", start), ("novel", "关键词", start + 1)]
    assert harness.forward.call_args.args[2][-1].data["content"] == (
        "[提示]\n小说还有下一页，可使用 /px搜索下一页 继续搜索"
    )
    assert [call.args[2:] for call in harness.download.await_args_list] == [
        (kind, 1) for kind in commands.KINDS.values()
    ]
    count = 8 if aggregate_next else 5
    assert list(harness.session().results) == list(range(1, count + 1))
    assert harness.session().results[count] == pagination.SearchResultRef("novel", start + 1)


@pytest.mark.parametrize("failure", ["exhausted", "timeout"])
def test_unsuccessful_selection_does_not_lock(harness, failure: str) -> None:
    """验证分类无下一页或查询失败时保持原有聚合模式

    Args:
        harness: 记录请求与状态的命令测试环境
        failure: 分类选择失败的原因
    """
    if failure == "exhausted":
        harness.ends["novel"] = 1
    else:
        harness.failures[("novel", 2)] = PixivNetworkError("模拟超时")

    async def scenario():
        """尝试不可用小说分支后仍允许继续图片分支"""
        await harness.run("/px搜索 关键词")
        await harness.run("/px搜索小说下一页")
        assert harness.session().kind == "all"
        assert harness.session().cursors["novel"].page == 1
        if failure == "exhausted":
            assert "小说已经没有下一页" in harness.last_message()
            assert len(harness.calls) == 3
        await harness.run("/px搜索图片下一页")
        assert harness.session().kind == "image"

    asyncio.run(scenario())
    assert harness.calls[-1] == ("image", "关键词", 2)


@pytest.mark.parametrize(
    "context",
    [{"user": 2}, {"group": 200}, {"group": None}, {"bot_id": "20"}],
)
def test_search_contexts_are_isolated(harness, context: dict) -> None:
    """验证用户，群聊，私聊和机器人之间不能共享分页及结果编号

    Args:
        harness: 记录请求与状态的命令测试环境
        context: 与原搜索不同的身份或聊天环境
    """

    async def scenario():
        """跨上下文下载静默使用 ID 并确认搜索使用各自的关键词和页码"""
        await harness.run("/px搜索图片 AAA")
        sent = harness.send.await_count
        await harness.run("/px下载1", **context)
        assert harness.download.call_args.args[2:] == ("auto", 1)
        assert harness.send.await_count == sent
        await harness.run("/px搜索下一页", **context)
        assert "没有可继续的搜索记录" in harness.last_message()
        assert len(harness.calls) == 1
        await harness.run("/px搜索图片 BBB", **context)
        await harness.run("/px搜索下一页")
        await harness.run("/px搜索下一页", **context)

    asyncio.run(scenario())
    assert harness.calls == [
        ("image", "AAA", 1),
        ("image", "BBB", 1),
        ("image", "AAA", 2),
        ("image", "BBB", 2),
    ]


@pytest.mark.parametrize("kind", ["", "图片", "漫画", "小说"])
def test_missing_session_does_not_search(harness, kind: str) -> None:
    """验证任何翻页命令在没有记录时均不请求 Pixiv

    Args:
        harness: 记录请求与状态的命令测试环境
        kind: 当前验证的命令分类
    """
    asyncio.run(harness.run(f"/px搜索{kind}下一页"))
    assert not harness.calls
    assert "没有可继续的搜索记录" in harness.last_message()
    harness.recall.assert_not_awaited()


@pytest.mark.parametrize("fails", [False, True])
def test_new_search_replaces_previous_intent(harness, fails: bool) -> None:
    """验证新搜索重置编号且失败后不会恢复旧搜索记录

    Args:
        harness: 记录请求与状态的命令测试环境
        fails: 是否让新搜索在网络层失败
    """

    async def scenario():
        """为旧会话积累编号后发起新搜索并检查编号重置与后续翻页"""
        await harness.run("/px搜索图片 AAA")
        await harness.run("/px搜索下一页")
        assert len(harness.session().results) == 2
        if fails:
            harness.failures[("image", 1)] = PixivNetworkError("模拟超时")
        await harness.run("/px搜索图片 BBB")
        if not fails:
            assert harness.session().results == {1: pagination.SearchResultRef("image", 1)}
            assert harness.session().next_result_index == 2
            sent = harness.send.await_count
            await harness.run("/px下载 2")
            assert harness.download.call_args.args[2:] == ("auto", 2)
            assert harness.send.await_count == sent
        await harness.run("/px搜索下一页")
        if fails:
            assert "没有可继续的搜索记录" in harness.last_message()
            assert not pagination.sessions
        else:
            assert harness.calls[-1] == ("image", "BBB", 2)

    asyncio.run(scenario())
    assert ("image", "AAA", 3) not in harness.calls


@pytest.mark.parametrize("age", [59, 60, 61])
def test_fixed_session_lifetime(harness, age: int) -> None:
    """验证有效期使用单调时钟且到达 60 秒即停止请求

    Args:
        harness: 记录请求与状态的命令测试环境
        age: 首次成功后推进的模拟秒数
    """

    async def scenario():
        """推进模拟时间后请求下一页"""
        await harness.run("/px搜索图片 关键词")
        harness.now += age
        await harness.run("/px搜索下一页")

    asyncio.run(scenario())
    if age < 60:
        assert harness.calls[-1] == ("image", "关键词", 2)
    else:
        assert len(harness.calls) == 1
        assert "已超时" in harness.last_message()
        assert not pagination.sessions
        assert harness.recall.await_count == 1


def test_next_page_never_renews_expiry(harness) -> None:
    """验证翻页和编号下载均不延长首次成功后的 60 秒期限

    Args:
        harness: 记录请求与状态的命令测试环境
    """

    async def scenario():
        """在第 30 秒翻页与下载后于第 60 秒静默改按 ID 下载"""
        await harness.run("/px搜索图片 关键词")
        expires_at = harness.session().expires_at
        harness.now += 30
        await harness.run("/px搜索下一页")
        await harness.run("/px下载 1")
        harness.download.assert_awaited_once()
        assert harness.session().expires_at == expires_at
        harness.now += 30
        await harness.run("/px下载 1")
        assert harness.download.call_args.args[2:] == ("auto", 1)
        assert not pagination.sessions

    asyncio.run(scenario())
    assert [page for _, _, page in harness.calls] == [1, 2]
    assert "已超时" not in harness.last_message()


@pytest.mark.parametrize("aggregate", [False, True])
def test_failed_page_keeps_cursor_and_partial_success(harness, aggregate: bool) -> None:
    """验证失败分类不登记编号且重试原目标页，成功分类独立推进

    Args:
        harness: 记录请求与状态的命令测试环境
        aggregate: 是否同时查询三个分类
    """

    async def scenario():
        """令漫画第 2 页失败一次后再继续搜索"""
        await harness.run("/px搜索 关键词" if aggregate else "/px搜索漫画 关键词")
        harness.failures[("manga", 2)] = PixivNetworkError("模拟超时")
        await harness.run("/px搜索下一页")
        session = harness.session()
        assert session.cursors["manga"].page == 1
        assert pagination.SearchResultRef("manga", 2) not in session.results.values()
        assert session.next_result_index == (6 if aggregate else 2)
        if aggregate:
            assert session.cursors["image"].page == session.cursors["novel"].page == 2
            assert harness.forward.await_count == 5
        harness.failures.clear()
        await harness.run("/px搜索下一页")

    asyncio.run(scenario())
    assert [page for kind, _, page in harness.calls if kind == "manga"] == [1, 2, 2]
    if aggregate:
        assert harness.calls[-3:] == [
            ("image", "关键词", 3),
            ("manga", "关键词", 2),
            ("novel", "关键词", 3),
        ]


@pytest.mark.parametrize("end_page", [1, 2, 10])
def test_concurrent_next_requests_are_serial(harness, end_page: int) -> None:
    """验证快速重复翻页不会重复请求同一页且耗尽后停止查询

    Args:
        harness: 记录请求与状态的命令测试环境
        end_page: 模拟当前分类允许查询的最后页码
    """
    harness.ends["image"] = end_page

    async def scenario():
        """同时发送两条通用下一页命令"""
        await harness.run("/px搜索图片 关键词")
        await asyncio.gather(harness.run("/px搜索下一页"), harness.run("/px搜索下一页"))

    asyncio.run(scenario())
    assert [page for _, _, page in harness.calls] == list(range(1, min(end_page, 3) + 1))
    if end_page < 3:
        assert "已经没有下一页" in harness.last_message()


@pytest.mark.parametrize("queued_command", ["/px搜索下一页", "/px下载 1"])
def test_waiting_for_lock_checks_expiry_again(harness, queued_command: str) -> None:
    """验证翻页或编号下载等待锁后重新检查有效期

    Args:
        harness: 记录请求与状态的命令测试环境
        queued_command: 在翻页持锁期间等待的分页或编号下载命令
    """

    async def scenario():
        """让第 2 页持锁并在命令排队后推进到失效时刻"""
        await harness.run("/px搜索图片 关键词")
        harness.now += 59
        entered, release = asyncio.Event(), asyncio.Event()
        harness.gates[("关键词", "image", 2)] = (entered, release)
        first = asyncio.create_task(harness.run("/px搜索下一页"))
        await entered.wait()
        second = asyncio.create_task(harness.run(queued_command))
        await asyncio.sleep(0)
        harness.now += 2
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())
    assert [page for _, _, page in harness.calls] == [1, 2]
    if queued_command == "/px搜索下一页":
        assert "已超时" in harness.last_message()
        harness.download.assert_not_awaited()
    else:
        assert harness.download.call_args.args[2:] == ("auto", 1)
        assert "已超时" not in harness.last_message()


def test_slow_old_search_cannot_overwrite_new_search(harness) -> None:
    """验证较早发起但较晚完成的搜索不会覆盖新关键词

    Args:
        harness: 记录请求与状态的命令测试环境
    """

    async def scenario():
        """挂起旧搜索后先完成新搜索并检查后续翻页"""
        entered, release = asyncio.Event(), asyncio.Event()
        harness.gates[("AAA", "image", 1)] = (entered, release)
        old = asyncio.create_task(harness.run("/px搜索图片 AAA"))
        await entered.wait()
        await asyncio.wait_for(harness.run("/px搜索图片 BBB"), 1)
        release.set()
        await old
        await harness.run("/px搜索下一页")

    asyncio.run(scenario())
    assert harness.calls[-1] == ("image", "BBB", 2)
    assert harness.forward.await_count == 2
    assert harness.recall.await_count == 3


def test_different_users_do_not_share_lock(harness) -> None:
    """验证一个用户的慢请求不阻塞另一个用户搜索

    Args:
        harness: 记录请求与状态的命令测试环境
    """

    async def scenario():
        """挂起用户 A 的翻页并要求用户 B 能独立完成搜索"""
        await harness.run("/px搜索图片 AAA")
        entered, release = asyncio.Event(), asyncio.Event()
        harness.gates[("AAA", "image", 2)] = (entered, release)
        old = asyncio.create_task(harness.run("/px搜索下一页"))
        await entered.wait()
        try:
            await asyncio.wait_for(harness.run("/px搜索图片 BBB", user=2), 1)
        finally:
            release.set()
            await old

    asyncio.run(scenario())
    assert harness.session().cursors["image"].page == 2
    assert harness.session(user=2).cursors["image"].page == 1


@pytest.mark.parametrize("operation", ["search", "next"])
def test_expired_records_are_pruned(harness, operation: str) -> None:
    """验证创建与读取会话都会清理其他过期记录

    Args:
        harness: 记录请求与状态的命令测试环境
        operation: 触发清理的搜索或翻页操作
    """

    async def scenario():
        """让旧用户记录过期后由新用户触发清理"""
        await harness.run("/px搜索图片 AAA")
        harness.now += 61
        await harness.run("/px搜索图片 BBB" if operation == "search" else "/px搜索下一页", user=2)

    asyncio.run(scenario())
    assert ("10", "group", "group_100_1") not in pagination.sessions


def test_aggregate_all_exhausted_does_not_request(harness) -> None:
    """验证聚合全部耗尽后不会重复请求或发送状态提示

    Args:
        harness: 记录请求与状态的命令测试环境
    """
    harness.ends = dict.fromkeys(commands.KINDS.values(), 1)

    async def scenario():
        """完成无后续页的聚合搜索后再次尝试翻页"""
        await harness.run("/px搜索 关键词")
        await harness.run("/px搜索下一页")

    asyncio.run(scenario())
    assert len(harness.calls) == 3
    assert harness.recall.await_count == 1
    assert "已经没有下一页" in harness.last_message()


def test_pagination_error_logs_context(harness, monkeypatch) -> None:
    """验证分页失败日志包含重试所需的上下文且不记录响应内容

    Args:
        harness: 记录请求与状态的命令测试环境
        monkeypatch: 用于收集日志参数的 pytest 工具
    """
    warning = Mock()
    monkeypatch.setattr(commands.logger, "warning", warning)
    harness.failures[("novel", 2)] = PixivNetworkError("模拟超时")

    async def scenario():
        """在聚合会话中模拟小说分页请求超时"""
        await harness.run("/px搜索 关键词")
        await harness.run("/px搜索小说下一页")

    asyncio.run(scenario())
    assert warning.call_args.args[1:] == (
        "关键词",
        "novel",
        1,
        2,
        ("10", "group", "group_100_1"),
        True,
        "模拟超时",
    )


@pytest.mark.parametrize("first_packet_fails", [False, True])
def test_expiry_starts_after_first_packet_success(
    harness, monkeypatch, first_packet_fails: bool
) -> None:
    """验证首包成功前不计时，后续分包与聚合分类不延长固定有效期

    Args:
        harness: 记录请求与状态的命令测试环境
        monkeypatch: 替换预览处理及分包容量的 pytest 工具
        first_packet_fails: 是否让图片首包失败并从漫画首包成功时开始计时
    """
    first_sent_at = None

    async def search(kind, word, page):
        """复用可控搜索请求并让图片结果超过单包容量

        Args:
            kind: 当前请求的作品分类
            word: 用于匹配模拟搜索等待条件的关键词
            page: 当前请求的 Pixiv 页码

        Returns:
            图片含三个作品且其他分类含一个作品的模拟搜索结果
        """
        result = await harness.search(kind, word, page)
        items = (
            [Artwork(index, "画作", 2, "作者", [], "image", 1, 0) for index in range(10, 13)]
            if kind == "image" else result.items
        )
        return SearchPage(items, result.page, result.has_next)

    async def previews(items):
        """模拟预览处理超过有效期长度且此时尚未开始计时

        Args:
            items: 等待预览处理的图片与漫画搜索结果

        Returns:
            无可用图片内容的预览集合
        """
        assert harness.session().expires_at is None
        harness.now += 61
        return {}

    async def forward(bot, event, packet):
        """推进发送耗时并验证仅首次发送成功建立有效期

        Args:
            bot: 用于发送当前分包的机器人实例
            event: 决定搜索会话的消息事件
            packet: 包含作品和尾部状态的待发送节点

        Raises:
            RuntimeError: 当前场景要求图片首包发送失败
        """
        nonlocal first_sent_at
        assert harness.session().expires_at == (
            None if first_sent_at is None else first_sent_at + 60
        )
        harness.now += 5
        if first_packet_fails and harness.forward.await_count == 1:
            raise RuntimeError("模拟发送失败")
        if first_sent_at is None:
            first_sent_at = harness.now

    monkeypatch.setattr(commands.config, "pixiv_forward_max_messages", 2)
    monkeypatch.setattr(commands, "_search_kind", search)
    monkeypatch.setattr(commands, "_search_previews", previews)
    harness.forward.side_effect = forward

    async def scenario():
        """在请求与预览及发送阶段推进时间并检查首包成功后的固定有效期"""
        entered, release = asyncio.Event(), asyncio.Event()
        harness.gates[("关键词", "image", 1)] = (entered, release)
        first = asyncio.create_task(harness.run("/px搜索 关键词"))
        await entered.wait()
        assert harness.session().expires_at is None
        harness.now += 61
        release.set()
        await first
        assert first_sent_at is not None
        assert harness.session().expires_at == first_sent_at + 60
        assert harness.now > first_sent_at

    asyncio.run(scenario())
    assert harness.forward.await_count == (3 if first_packet_fails else 4)


@pytest.mark.parametrize("empty", [False, True])
def test_initial_search_without_sent_results_releases_session(
    harness, monkeypatch, empty: bool
) -> None:
    """验证空结果或全部发送失败不留下无有效期的会话，也不创建纯提示分包

    Args:
        harness: 记录请求与状态的命令测试环境
        monkeypatch: 替换空结果查询的 pytest 工具
        empty: 是否返回空结果，否则模拟合并转发发送失败
    """
    if empty:
        monkeypatch.setattr(
            commands, "_search_kind", AsyncMock(return_value=SearchPage([], 1, False))
        )
    else:
        harness.forward.side_effect = RuntimeError("模拟发送失败")
    asyncio.run(harness.run("/px搜索图片 关键词"))
    assert not pagination.sessions
    assert harness.forward.await_count == (0 if empty else 1)
    assert harness.last_message() == ("没有搜索到相关内容" if empty else "图片搜索结果发送失败")


@pytest.mark.parametrize("queued_command", ["/px搜索下一页", "/px下载 1"])
def test_queued_request_cannot_use_replaced_session(harness, queued_command: str) -> None:
    """验证排队中的分页或编号下载不能使用已被替换的搜索记录

    Args:
        harness: 记录请求与状态的命令测试环境
        queued_command: 等待旧会话锁的分页或编号下载命令
    """

    async def scenario():
        """在旧会话有执行和排队请求时发起新搜索"""
        await harness.run("/px搜索图片 AAA")
        entered, release = asyncio.Event(), asyncio.Event()
        harness.gates[("AAA", "image", 2)] = (entered, release)
        first = asyncio.create_task(harness.run("/px搜索下一页"))
        await entered.wait()
        queued = asyncio.create_task(harness.run(queued_command))
        await asyncio.sleep(0)
        await harness.run("/px搜索图片 BBB")
        release.set()
        await asyncio.gather(first, queued)
        if queued_command == "/px搜索下一页":
            assert "已更新" in harness.last_message()
            harness.download.assert_not_awaited()
        else:
            assert harness.download.call_args.args[2:] == ("auto", 1)
            assert "已更新" not in harness.last_message()
        await harness.run("/px搜索下一页")

    asyncio.run(scenario())
    assert harness.calls == [
        ("image", "AAA", 1),
        ("image", "AAA", 2),
        ("image", "BBB", 1),
        ("image", "BBB", 2),
    ]


def test_concurrent_selection_then_generic_next_respects_lock(harness) -> None:
    """验证分类选择执行中排队的通用翻页使用收窄后的模式

    Args:
        harness: 记录请求与状态的命令测试环境
    """

    async def scenario():
        """挂起小说第 2 页并在其后排队通用翻页"""
        await harness.run("/px搜索 关键词")
        entered, release = asyncio.Event(), asyncio.Event()
        harness.gates[("关键词", "novel", 2)] = (entered, release)
        selected = asyncio.create_task(harness.run("/px搜索小说下一页"))
        await entered.wait()
        generic = asyncio.create_task(harness.run("/px搜索下一页"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(selected, generic)

    asyncio.run(scenario())
    assert harness.calls[-2:] == [("novel", "关键词", 2), ("novel", "关键词", 3)]
    assert len(harness.calls) == 5


def test_cancelled_initial_search_releases_pending_session(harness) -> None:
    """验证初次搜索取消后不会残留没有有效期的记录

    Args:
        harness: 记录请求与状态的命令测试环境
    """

    async def scenario():
        """在首次网络请求挂起时取消任务"""
        entered, release = asyncio.Event(), asyncio.Event()
        harness.gates[("关键词", "image", 1)] = (entered, release)
        first = asyncio.create_task(harness.run("/px搜索图片 关键词"))
        await entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

    asyncio.run(scenario())
    assert not pagination.sessions
    harness.recall.assert_awaited_once()


@pytest.mark.parametrize("broken", ["model", "metadata"])
def test_parse_failure_does_not_advance_cursor(harness, monkeypatch, broken: str) -> None:
    """验证真实客户端的数据解析失败同样不会提前推进游标

    Args:
        harness: 记录请求与状态的命令测试环境
        monkeypatch: 用于替换搜索客户端的 pytest 工具
        broken: 当前模拟的作品字段错误或分页元数据错误
    """
    pages = []
    fail = True

    def handler(request: httpx.Request) -> httpx.Response:
        """为第 2 页注入可恢复的解析错误

        Args:
            request: 含有当前搜索页码的 HTTP 请求

        Returns:
            首次第 2 页数据损坏且重试恢复的模拟响应
        """
        page = int(request.url.params["p"])
        pages.append(page)
        invalid = page == 2 and fail
        item = {"id": "无效" if invalid and broken == "model" else page}
        total = "无效" if invalid and broken == "metadata" else 180
        return httpx.Response(
            200, json={"error": False, "body": {"illust": {"data": [item], "total": total}}}
        )

    async def scenario():
        """查询数据损坏的下一页后恢复响应并重试相同页码"""
        nonlocal fail
        client = PixivClient(Config(pixiv_cookie="PHPSESSID=test"), httpx.MockTransport(handler))
        monkeypatch.setattr(
            commands, "_search_kind", lambda kind, word, page: client.search_artworks(word, 1, page)
        )
        try:
            await harness.run("/px搜索图片 关键词")
            await harness.run("/px搜索下一页")
            assert harness.session().cursors["image"].page == 1
            fail = False
            await harness.run("/px搜索下一页")
            assert harness.session().cursors["image"].page == 2
        finally:
            await client.close()

    asyncio.run(scenario())
    assert pages == [1, 2, 2]

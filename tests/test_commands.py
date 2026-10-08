import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent
from nonebot.permission import SUPERUSER

from nonebot_plugin_pixiv_dl import commands, pagination
from nonebot_plugin_pixiv_dl.models import Artwork, ContentPolicy, Novel, NovelSeries, SearchPage
from nonebot_plugin_pixiv_dl.pixiv import PixivNotFoundError
from nonebot_plugin_pixiv_dl.preferences import PreferenceError, PreferenceStore


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
        ("/px下载结果 3", None),
    ],
)
def test_download_commands(text: str, expected: tuple[str, int] | None) -> None:
    """验证下载命令兼容空格省略并正确提取分类

    Args:
        text: 待解析的下载命令样例
        expected: 预期的分类与作品 ID，空值表示样例应被拒绝
    """
    assert commands.parse_download(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/px相关 3", 3),
        ("/px相关3", 3),
        ("/px相关 003 ", 3),
        ("/px相关 0", None),
        ("/px相关 -1", None),
        ("/px相关 1.5", None),
        ("/px相关 abc", None),
        ("/px相关", None),
        ("/px相关 3 图片", None),
    ],
)
def test_related_commands(monkeypatch, text: str, expected: int | None) -> None:
    """验证相关查询兼容空格省略且仅接受正整数，不会命中下载命令

    Args:
        monkeypatch: 用于替换编号解析后续流程的 pytest 工具
        text: 包含合法或非法结果编号的命令文本
        expected: 预期传入查询流程的编号，空值表示拒绝该命令
    """
    run = AsyncMock()
    monkeypatch.setattr(commands, "_run_related", run)
    bot = SimpleNamespace()
    event = SimpleNamespace(get_plaintext=lambda: text)
    asyncio.run(commands.handle_related(bot, event))
    assert not commands.DOWNLOAD_RE.fullmatch(text)
    assert bool(commands.RELATED_RE.fullmatch(text)) == (expected is not None)
    if expected is None:
        run.assert_not_awaited()
    else:
        run.assert_awaited_once_with(bot, event, expected)


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

    async def search_kind(kind, word, page, policy):
        """为指定搜索分类提供一条模拟预览

        Args:
            kind: 请求的作品分类
            word: 用于匹配作品标签的搜索关键词
            page: 本次请求的 Pixiv 搜索页码
            policy: 当前请求独立的内容策略

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
    event = SimpleNamespace(
        message_type="private", get_session_id=lambda: "1", get_user_id=lambda: "1"
    )
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
    event = SimpleNamespace(message_type="private", get_user_id=lambda: "1")
    asyncio.run(commands._run_download(FakeBot(), event, work_type, 9))
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
    event = SimpleNamespace(message_type="private", get_user_id=lambda: "1")
    asyncio.run(commands._run_download(FakeBot(), event, "novel", 1))
    assert [path.name[:2] for path in paths] == ["01", "02"]
    assert actions[0] == ("message", "正在下载")
    assert "系列总章节：3" in actions[1][1]
    assert "本次下载：2" in actions[1][1]
    assert actions[2] == ("recall", 5)
    assert all(not path.exists() for path in paths)


def preference_context(text="/px设置", user=20, group=30, bot_id="10", superusers=()):
    """构造包含真实用户身份与原生超级用户配置的命令环境

    Args:
        text: 当前事件携带的完整命令
        user: 实际发送消息的 QQ 号
        group: 当前群号，None 表示私聊
        bot_id: 接收命令的机器人 QQ 号
        superusers: 配置中允许管理群设置的用户标识集合

    Returns:
        记录发送操作的机器人替身与真实 OneBot 消息事件
    """
    bot = Bot(
        SimpleNamespace(
            config=SimpleNamespace(superusers=set(superusers)), get_name=lambda: "OneBot V11"
        ), bot_id,
    )
    bot.send = AsyncMock(return_value={"message_id": 1})
    bot.delete_msg = AsyncMock()
    fields = {
        "time": 0, "self_id": int(bot_id), "post_type": "message", "user_id": user,
        "sub_type": "normal" if group else "friend",
        "message_type": "group" if group else "private", "message_id": 1,
        "message": text, "raw_message": text, "font": 0, "sender": {},
    }
    event = GroupMessageEvent(**fields, group_id=group) if group else PrivateMessageEvent(**fields)
    return bot, event


@pytest.mark.parametrize("superuser", [False, True])
@pytest.mark.parametrize("group", [None, 30])
@pytest.mark.parametrize("role", ["member", "admin", "owner"])
def test_group_settings_use_native_superuser(superuser, group, role):
    """验证原生 SUPERUSER 权限覆盖群私聊且群身份不授予管理权

    Args:
        superuser: 发送者是否出现在机器人超级用户配置中
        group: 命令所在群号，None 表示私聊
        role: 当前发送者的群成员身份
    """
    bot, event = preference_context(
        "/px群R18 999 关", group=group, superusers={"20"} if superuser else set()
    )
    if group:
        event.sender.role = role

    async def scenario():
        """同时检查匹配器权限和处理函数写入前的再次校验"""
        assert commands.SUPERUSER is SUPERUSER
        assert await commands.group_matcher.permission(bot, event) is superuser
        await commands.handle_group_settings(bot, event)
        settings = await commands.preferences.get("groups", "10", "999")
        assert settings["r18"] is (not superuser)
        if superuser:
            bot.send.assert_awaited_once_with(event, "群 999 R18：关")
            _, query = preference_context("/px群R18 999", group=group)
            await commands.handle_group_settings(bot, query)
            bot.send.assert_awaited_with(query, "群 999 R18：关")
        else:
            bot.send.assert_not_awaited()
            assert not (commands.preferences.directory / "groups.json").exists()

    asyncio.run(scenario())


def test_personal_preferences_only_address_sender():
    """验证个人设置跨群私聊共享，隔离机器人和用户并拒绝指定他人"""

    async def scenario():
        """保存两项独立偏好并通过多个聊天环境重新查询"""
        for text in ("/px设置 R18 关", "/px设置 AI 关"):
            bot, event = preference_context(text)
            await commands.handle_settings(bot, event)
            bot.send.assert_awaited_once_with(event, text.replace("/px设置 ", "个人设置："))
        for group in (None, 30, 99):
            bot, event = preference_context(group=group)
            await commands.handle_settings(bot, event)
            assert bot.send.call_args.args[1].startswith("个人设置：R18 关，AI 关")
            assert await commands._get_policy(bot, event) == ContentPolicy(False, False)
        for text in ("/px设置 21 R18 关", "/px设置 R18 关 21", "/px设置 AI 关 21"):
            bot, event = preference_context(text)
            await commands.handle_settings(bot, event)
            bot.send.assert_not_awaited()
        assert await commands.preferences.get("users", "10", "21") == {"r18": True, "ai": True}
        assert await commands.preferences.get("users", "11", "20") == {"r18": True, "ai": True}
        await commands.preferences.set("users", "10", "20", "ai", True)
        bot, event = preference_context()
        assert await commands._get_policy(bot, event) == ContentPolicy(False, True)

    asyncio.run(scenario())


@pytest.mark.parametrize("global_r18,group_r18,user_r18,ai", [
    (True, False, True, True), (True, False, True, False),
    (False, True, True, True), (False, True, True, False),
    (True, True, False, True), (True, True, True, False),
])
def test_effective_policy_respects_every_r18_limit(
    monkeypatch, global_r18, group_r18, user_r18, ai
):
    """验证任一 R18 限制都有效，超级用户也不例外且 AI 完全独立

    Args:
        monkeypatch: 替换全局配置的 pytest 工具
        global_r18: 插件级 R18 总开关
        group_r18: 当前群的 R18 设置
        user_r18: 当前用户的 R18 设置
        ai: 当前用户的 AI 接收设置
    """
    monkeypatch.setattr(commands.config, "pixiv_r18", global_r18)

    async def scenario():
        """分别计算同一超级用户在群聊和私聊中的有效策略"""
        await commands.preferences.set("groups", "10", "30", "r18", group_r18)
        await commands.preferences.set("users", "10", "20", "r18", user_r18)
        await commands.preferences.set("users", "10", "20", "ai", ai)
        for group in (None, 30):
            bot, event = preference_context(group=group, superusers={"20"})
            expected = global_r18 and user_r18 and (group is None or group_r18)
            assert await commands._get_policy(bot, event) == ContentPolicy(expected, ai)

    asyncio.run(scenario())


def test_preferences_persist_and_merge_concurrent_updates():
    """验证并发写入保留同一用户的独立字段，重建存储对象后仍保持设置"""

    async def scenario():
        """并发更新多机器人记录并模拟重启后从文件读取"""
        store = commands.preferences
        await asyncio.gather(
            *(store.set("users", "10", str(user), field, False)
              for user in range(20, 30) for field in ("r18", "ai")),
            store.set("groups", "10", "30", "r18", False),
            store.set("groups", "11", "30", "r18", True),
        )
        restarted = PreferenceStore(store.directory)
        for user in range(20, 30):
            assert await restarted.get("users", "10", str(user)) == {"r18": False, "ai": False}
        assert await restarted.get("groups", "10", "30") == {"r18": False}
        assert await restarted.get("groups", "11", "30") == {"r18": True}
        assert set(path.name for path in store.directory.iterdir()) == {"groups.json", "users.json"}
        assert store.directory != commands.ugoira_cache_dir

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["groups", "users"])
@pytest.mark.parametrize("damage", ["json", "shape", "unreadable"])
def test_broken_preferences_fail_closed(monkeypatch, name, damage):
    """验证文件损坏或不可读时保留原文件并阻止设置及内容请求

    Args:
        monkeypatch: 注入文件读取故障的 pytest 工具
        name: 本次损坏的群设置或个人设置文件
        damage: 非法 JSON，非法结构或无法读取的故障类型
    """
    path = commands.preferences.directory / f"{name}.json"
    original = "{" if damage == "json" else json.dumps({"10": {"30": {"r18": "false"}}})
    path.write_text(original, encoding="utf-8")
    if damage == "unreadable":
        monkeypatch.setattr(Path, "read_text", Mock(side_effect=PermissionError("模拟读取失败")))
    forward = AsyncMock()
    resource = AsyncMock()
    monkeypatch.setattr(commands, "send_forward", forward)
    monkeypatch.setattr(commands.client, "get_illust_pages", resource)
    monkeypatch.setattr(commands, "_get_download_target", AsyncMock(
        return_value=Artwork(1, "限制级", 1, "作者", [], "image", 1, 1)
    ))

    async def scenario():
        """检查故障不会恢复默认许可或覆盖旧设置"""
        with pytest.raises(PreferenceError):
            await commands.preferences.set(name, "10", "30", "r18", True)
        bot, event = preference_context()
        with pytest.raises(PreferenceError):
            await commands._get_policy(bot, event)
        await commands._run_search(bot, event, "image", "测试")
        await commands._run_download(bot, event, "image", 1)

    asyncio.run(scenario())
    assert path.read_bytes().decode("utf-8") == original
    forward.assert_not_awaited()
    resource.assert_not_awaited()


def test_failed_atomic_replace_keeps_old_preferences(monkeypatch):
    """验证原子替换失败不破坏旧限制且不遗留临时文件

    Args:
        monkeypatch: 注入磁盘替换失败的 pytest 工具
    """

    async def scenario():
        """先保存关闭状态，再模拟后续更新无法替换文件"""
        await commands.preferences.set("groups", "10", "30", "r18", False)
        path = commands.preferences.directory / "groups.json"
        original = path.read_bytes()
        with monkeypatch.context() as patch:
            patch.setattr(Path, "replace", Mock(side_effect=PermissionError("模拟替换失败")))
            with pytest.raises(PreferenceError):
                await commands.preferences.set("groups", "10", "30", "r18", True)
        assert path.read_bytes() == original
        assert list(path.parent.iterdir()) == [path]
        assert await commands.preferences.get("groups", "10", "30") == {"r18": False}

    asyncio.run(scenario())


@pytest.mark.parametrize("source", ["groups", "users"])
@pytest.mark.parametrize("text,kind,animated", [
    ("/px下载 1", "image", False), ("/px下载 42", "image", False),
    ("/px下载图片 42", "image", False), ("/px下载漫画 42", "manga", False),
    ("/px下载小说 42", "novel", False), ("/px下载 1", "image", True),
    ("/px下载图片 42", "image", True), ("/px下载 1", "novel", False),
])
def test_latest_r18_blocks_all_download_commands(monkeypatch, source, text, kind, animated):
    """验证最新群和个人限制覆盖显式下载与旧搜索编号，资源请求尚未开始

    Args:
        monkeypatch: 替换元数据与资源请求的 pytest 工具
        source: 本次关闭的群或个人 R18 设置
        text: 用户发送的下载命令
        kind: 当前编号或作品元数据的真实分类
        animated: 是否使用需要发送 ZIP 和 MP4 的动图
    """
    work = (
        Novel(42, "小说", 1, "作者", [], 1, series_id=55) if kind == "novel"
        else Artwork(42, "作品", 1, "作者", [], kind, 1, 1, illust_type=2 if animated else 0)
    )
    target = AsyncMock(return_value=work)
    monkeypatch.setattr(commands, "_get_download_target", target)
    forbidden = []
    for name in ("get_illust_pages", "get_ugoira_meta", "get_novel_series", "download_image"):
        operation = AsyncMock()
        monkeypatch.setattr(commands.client, name, operation)
        forbidden.append(operation)
    forward = AsyncMock()
    monkeypatch.setattr(commands, "send_forward", forward)
    bot, event = preference_context(text, superusers={"20"})

    async def scenario():
        """建立旧编号后关闭内容许可，直接交给正式命令处理器"""
        assert (await commands._get_policy(bot, event)).allow_r18
        session = pagination.SearchSession("旧搜索", kind, {})
        session.expires_at = pagination.monotonic() + 60
        session.results[1] = pagination.SearchResultRef(kind, 42)
        commands.sessions[commands.session_key(bot, event)] = session
        await commands.preferences.set(source, "10", "30" if source == "groups" else "20",
                                       "r18", False)
        await commands.handle_download(bot, event)
        assert target.call_args.args[1] == 42
        bot.send.assert_awaited_with(event, "已关闭 R18，无法下载该作品")

    asyncio.run(scenario())
    for operation in [forward, *forbidden]:
        operation.assert_not_awaited()


@pytest.mark.parametrize("restricted", ["series", "preview", "detail"])
def test_novel_series_checks_each_metadata_layer(monkeypatch, restricted):
    """验证小说系列和章节详情不能绕过当前群的限制

    Args:
        monkeypatch: 替换系列详情与文件生成的 pytest 工具
        restricted: 本次携带限制级标记的系列或章节层级
    """
    target = Novel(42, "入口", 1, "作者", [], 0, series_id=55)
    chapter = Novel(43, "章节", 1, "作者", [], int(restricted == "preview"))
    series = NovelSeries(55, "系列", 1, "作者", [], int(restricted == "series"), 1, [chapter])
    detail = AsyncMock(return_value=Novel(43, "正文", 1, "作者", [], 1, content="正文"))
    writer = Mock()
    forward = AsyncMock()
    monkeypatch.setattr(commands, "_get_download_target", AsyncMock(return_value=target))
    monkeypatch.setattr(commands.client, "get_novel_series", AsyncMock(return_value=series))
    monkeypatch.setattr(commands.client, "get_novel", detail)
    monkeypatch.setattr(commands, "write_novel_file", writer)
    monkeypatch.setattr(commands, "send_forward", forward)
    bot, event = preference_context()

    async def scenario():
        """关闭群许可后下载普通入口章节所属系列"""
        await commands.preferences.set("groups", "10", "30", "r18", False)
        await commands._run_download(bot, event, "novel", 42)

    asyncio.run(scenario())
    assert detail.await_count == int(restricted == "detail")
    writer.assert_not_called()
    forward.assert_not_awaited()
    bot.send.assert_awaited_with(event, "已关闭 R18，无法下载该作品")


@pytest.mark.parametrize("change_during_download", [False, True])
def test_explicit_ai_download_and_live_r18_check(monkeypatch, change_during_download):
    """验证个人隐藏 AI 不限制主动下载且下载期间关闭 R18 会阻止发送

    Args:
        monkeypatch: 替换资源下载与发送的 pytest 工具
        change_during_download: 是否在图片下载完成前关闭当前群的 R18
    """
    work = Artwork(42, "AI 作品", 1, "作者", [], "image", 1, 1, ai_type=2)
    bot, event = preference_context("/px下载图片 42")
    forward = AsyncMock()
    monkeypatch.setattr(commands, "_get_download_target", AsyncMock(return_value=work))
    monkeypatch.setattr(commands.client, "get_illust_pages", AsyncMock(return_value=["图片地址"]))
    monkeypatch.setattr(commands, "send_forward", forward)

    async def download(url):
        """在可控资源等待期间修改最新权限

        Args:
            url: 用于验证原图下载已开始的模拟资源地址

        Returns:
            用于构造转发消息的模拟图片内容
        """
        if change_during_download:
            await commands.preferences.set("groups", "10", "30", "r18", False)
        return b"image"

    monkeypatch.setattr(commands.client, "download_image", download)

    async def scenario():
        """关闭个人 AI 后执行显式作品下载"""
        await commands.preferences.set("users", "10", "20", "ai", False)
        await commands.handle_download(bot, event)

    asyncio.run(scenario())
    assert forward.await_count == (0 if change_during_download else 1)

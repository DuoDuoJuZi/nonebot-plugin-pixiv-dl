import asyncio
from dataclasses import dataclass, field
from time import monotonic

from nonebot.adapters.onebot.v11 import Bot, MessageEvent

SEARCH_SESSION_TTL = 60


# @Author: DuoDuoJuZi
# @Date: 2026-10-02
@dataclass(slots=True)
class SearchCursor:
    """保存单个分类最后成功读取的 Pixiv 页码和后续页标记"""

    page: int = 0
    has_next: bool = False


@dataclass(slots=True, frozen=True)
class SearchResultRef:
    """保存已发送搜索结果的作品分类和 Pixiv ID"""

    kind: str
    work_id: int


@dataclass(slots=True)
class SearchSession:
    """保存搜索意图，分类游标及已发送结果引用，首次成功后固定存活 60 秒"""

    word: str
    kind: str
    cursors: dict[str, SearchCursor]
    expires_at: float | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    results: dict[int, SearchResultRef] = field(default_factory=dict)
    next_result_index: int = 1

    def expired(self) -> bool:
        """判断已建立的分页记录是否达到固定失效时间

        Returns:
            达到失效时间时返回 True，首次搜索尚未完成时返回 False
        """
        return self.expires_at is not None and monotonic() >= self.expires_at


sessions: dict[tuple[str, str, str], SearchSession] = {}


def session_key(bot: Bot, event: MessageEvent) -> tuple[str, str, str]:
    """组合 OneBot 机器人身份和包含用户的聊天环境标识

    Args:
        bot: 发起搜索的 OneBot 机器人实例
        event: 群聊标识包含群号与用户号，私聊标识包含用户号

    Returns:
        区分机器人，消息类型和用户所在聊天环境的内存索引
    """
    return bot.self_id, event.message_type, event.get_session_id()


def prune_sessions() -> None:
    """在创建或读取记录时清理过期会话，无需后台计时任务"""
    for key, session in list(sessions.items()):
        if session.expired():
            del sessions[key]

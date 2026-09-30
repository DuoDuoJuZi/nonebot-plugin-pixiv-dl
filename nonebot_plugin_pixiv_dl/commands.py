import asyncio
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from nonebot import get_driver, get_plugin_config, logger, on_regex
from nonebot.adapters.onebot.v11 import Bot, MessageEvent

from .config import Config
from .message import (
    KIND_NAMES,
    build_artwork_forward,
    build_novel_forward,
    build_search_forward,
    format_novel,
    format_series,
    process_preview,
    send_forward,
    write_novel_file,
)
from .models import Artwork, Novel
from .pixiv import PixivAPIError, PixivClient, PixivNotFoundError, PixivR18Error

SEARCH_RE = re.compile(r"^/px搜索(?!(?:图片|漫画|小说)\s*$)(图片|漫画|小说)?\s*(\S.*?)\s*$")
DOWNLOAD_RE = re.compile(r"^/px下载(图片|漫画|小说)?\s*(\d+)\s*$")
KINDS = {"图片": "image", "漫画": "manga", "小说": "novel"}

config = get_plugin_config(Config)
client = PixivClient(config)
preview_semaphore = asyncio.Semaphore(config.pixiv_preview_concurrency)
get_driver().on_shutdown(client.close)

search_matcher = on_regex(SEARCH_RE.pattern, priority=10, block=True)
download_matcher = on_regex(DOWNLOAD_RE.pattern, priority=10, block=True)


# @Author: DuoDuoJuZi
# @Date: 2026-09-29
def parse_search(text: str) -> tuple[str, str] | None:
    """解析搜索命令，兼容分类与关键词之间省略空格的写法

    Args:
        text: 用户发送的完整搜索命令文本

    Returns:
        搜索分类与非空关键词，命令格式不匹配时返回 None
    """
    match = SEARCH_RE.fullmatch(text)
    if not match:
        return None
    return KINDS.get(match.group(1), "all"), match.group(2).strip()


def parse_download(text: str) -> tuple[str, int] | None:
    """解析下载命令，保留用户指定的分类或自动识别标记

    Args:
        text: 用户发送的完整下载命令文本

    Returns:
        下载分类与作品 ID，命令格式不匹配时返回 None
    """
    match = DOWNLOAD_RE.fullmatch(text)
    if not match:
        return None
    return KINDS.get(match.group(1), "auto"), int(match.group(2))


async def _recall(bot: Bot, status: dict) -> None:
    """撤回处理中提示，撤回失败时记录日志并继续发送结果

    Args:
        bot: 用于调用 OneBot 消息接口的机器人实例
        status: 发送处理中提示时返回的消息信息，包含待撤回的消息标识
    """
    try:
        await bot.delete_msg(message_id=status["message_id"])
    except Exception:
        logger.warning("Pixiv 状态消息撤回失败", exc_info=True)


async def _search_kind(kind: str, word: str) -> list[Artwork] | list[Novel]:
    """调用指定分类的 Pixiv 搜索接口

    Args:
        kind: 请求的作品分类
        word: 用于匹配作品标签的搜索关键词

    Returns:
        符合配置数量上限的该分类作品预览

    Raises:
        PixivAuthError: 未配置 Cookie 或 Pixiv 要求重新登录
        PixivNotFoundError: 请求的资源不存在或响应没有业务数据
        PixivNetworkError: 请求超时或网络连接失败
        PixivAPIError: HTTP 请求失败或 Pixiv 返回无效业务数据
    """
    method = {
        "image": client.search_artworks,
        "manga": client.search_manga,
        "novel": client.search_novels,
    }[kind]
    return await method(word, config.pixiv_search_limit)


async def _search_previews(items: list[Artwork]) -> dict[int, bytes]:
    """有限并发下载搜索预览，单张失败时保留对应作品的元数据

    Args:
        items: 按搜索顺序排列的插画或漫画元数据

    Returns:
        按作品 ID 保存的成功处理预览，关闭预览时返回空字典
    """
    if not config.pixiv_search_preview:
        return {}

    async def load(item: Artwork) -> bytes | None:
        """下载并处理单部作品的预览，限制级处理失败时不返回原始图片

        Args:
            item: 包含预览地址与限制级标记的作品元数据

        Returns:
            已处理的 JPEG 内容，地址缺失或处理失败时返回 None
        """
        if not item.preview_url:
            return None
        try:
            async with preview_semaphore:
                data = await client.download_image(item.preview_url)
                return await asyncio.to_thread(
                    process_preview, data, item.is_r18, config.pixiv_preview_max_edge
                )
        except Exception as exc:
            logger.warning(
                "Pixiv 预览处理失败，作品 ID={}，URL={}，异常={}",
                item.id,
                item.preview_url,
                exc,
            )
            return None

    previews = await asyncio.gather(*(load(item) for item in items))
    return {
        item.id: data
        for item, data in zip(items, previews, strict=True)
        if data is not None
    }


async def _run_search(bot: Bot, event: MessageEvent, kind: str, word: str) -> None:
    """查询元数据和低清预览并撤回状态提示，按分类发送合并转发

    Args:
        bot: 用于调用 OneBot 消息接口的机器人实例
        event: 触发命令的消息事件，决定回复会话
        kind: 搜索分类，all 表示分别查询插画，漫画和小说
        word: 用于匹配作品标签的搜索关键词
    """
    status = await bot.send(event, "正在搜索")
    recalled = False
    try:
        kinds = ["image", "manga", "novel"] if kind == "all" else [kind]
        results = await asyncio.gather(*(_search_kind(item, word) for item in kinds))
        previews = await _search_previews(
            [item for items in results for item in items if isinstance(item, Artwork)]
        )
        await _recall(bot, status)
        recalled = True
        if all(not items for items in results):
            await bot.send(event, "没有搜索到相关内容")
            return
        for current, items in zip(kinds, results, strict=True):
            if not items:
                await bot.send(event, f"没有搜索到相关{KIND_NAMES[current]}")
                continue
            packets = build_search_forward(
                items, current, config.pixiv_forward_max_messages, int(bot.self_id), previews
            )
            for packet in packets:
                await send_forward(bot, event, packet)
    except Exception:
        logger.exception("Pixiv 搜索失败，分类={}，关键词={}", kind, word)
        if not recalled:
            await _recall(bot, status)
        await bot.send(event, "搜索失败")


async def _get_download_target(kind: str, work_id: int) -> Artwork | Novel:
    """查询 Pixiv 确定下载资源类型，自动模式下插画不存在时尝试小说

    Args:
        kind: 下载分类，auto 表示根据接口响应自动识别
        work_id: 待查询作品的 Pixiv ID

    Returns:
        可下载的插画或小说详情

    Raises:
        PixivAuthError: 未配置 Cookie 或 Pixiv 要求重新登录
        PixivNotFoundError: 请求的资源不存在或响应没有业务数据
        PixivNetworkError: 请求超时或网络连接失败
        PixivAPIError: 实际作品分类与指定分类不符或接口数据无效
        PixivR18Error: 作品属于限制级内容且配置禁止下载
    """
    if kind == "novel":
        return await client.get_novel(work_id)
    try:
        artwork = await client.get_illust(work_id)
    except PixivNotFoundError:
        if kind == "auto":
            return await client.get_novel(work_id)
        raise
    if kind != "auto" and artwork.type != kind:
        raise PixivAPIError(f"{work_id}: expected {kind}, got {artwork.type}")
    return artwork


async def _run_download(bot: Bot, event: MessageEvent, kind: str, work_id: int) -> None:
    """下载作品并以元数据首节点打包发送，发送结束后清理临时章节文件

    Args:
        bot: 用于调用 OneBot 消息接口的机器人实例
        event: 触发命令的消息事件，决定回复会话
        kind: 下载分类，auto 表示先查询作品的实际类型
        work_id: 待查询作品的 Pixiv ID
    """
    status = await bot.send(event, "正在下载")
    recalled = False
    try:
        target = await _get_download_target(kind, work_id)
        if isinstance(target, Novel):
            with TemporaryDirectory(prefix="nonebot-pixiv-") as directory:
                folder = Path(directory)
                if target.series_id:
                    series = await client.get_novel_series(
                        target.series_id, config.pixiv_novel_max_chapters
                    )
                    chapters = []
                    for item in series.chapters:
                        try:
                            chapters.append(await client.get_novel(item.id))
                        except Exception as exc:
                            raise PixivAPIError(
                                f"series {series.id}, chapter {item.id}: {exc}"
                            ) from exc
                    paths = [
                        write_novel_file(chapter, folder, index)
                        for index, chapter in enumerate(chapters, 1)
                    ]
                    metadata = format_series(series, len(paths))
                else:
                    paths = [write_novel_file(target, folder)]
                    metadata = format_novel(target)
                if not paths:
                    raise PixivAPIError(f"{work_id}: no novel chapters")
                packets = build_novel_forward(
                    metadata, paths, config.pixiv_forward_max_messages, int(bot.self_id)
                )
                for packet in packets:
                    await send_forward(bot, event, packet)
                await _recall(bot, status)
                recalled = True
        else:
            urls = await client.get_illust_pages(target.id)
            if target.type == "manga":
                urls = urls[: config.pixiv_download_max_pages]
            images = []
            for page, url in enumerate(urls, 1):
                try:
                    images.append(await client.download_image(url))
                except Exception as exc:
                    raise PixivAPIError(f"artwork {target.id}, page {page}: {exc}") from exc
            await _recall(bot, status)
            recalled = True
            packets = build_artwork_forward(
                target, images, config.pixiv_forward_max_messages, int(bot.self_id)
            )
            for packet in packets:
                await send_forward(bot, event, packet)
    except PixivR18Error:
        if not recalled:
            await _recall(bot, status)
        await bot.send(event, "已关闭 R18，无法下载该作品")
    except Exception:
        logger.exception("Pixiv 下载失败，分类={}，作品 ID={}", kind, work_id)
        if not recalled:
            await _recall(bot, status)
        await bot.send(event, "下载失败")


@search_matcher.handle()
async def handle_search(bot: Bot, event: MessageEvent) -> None:
    """解析 OneBot 消息并执行匹配的搜索命令

    Args:
        bot: 用于调用 OneBot 消息接口的机器人实例
        event: 触发命令的消息事件，决定回复会话
    """
    parsed = parse_search(event.get_plaintext())
    if parsed:
        await _run_search(bot, event, *parsed)


@download_matcher.handle()
async def handle_download(bot: Bot, event: MessageEvent) -> None:
    """解析 OneBot 消息并执行匹配的下载命令

    Args:
        bot: 用于调用 OneBot 消息接口的机器人实例
        event: 触发命令的消息事件，决定回复会话
    """
    parsed = parse_download(event.get_plaintext())
    if parsed:
        await _run_download(bot, event, *parsed)

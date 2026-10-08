import asyncio
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from nonebot import get_driver, get_plugin_config, logger, on_regex
from nonebot.adapters.onebot.v11 import Bot, MessageEvent

from . import pagination, ugoira
from .config import Config
from .message import (
    KIND_NAMES,
    MediaSendResult,
    build_artwork_forward,
    build_novel_forward,
    build_search_forward,
    format_novel,
    format_series,
    format_ugoira,
    format_ugoira_delivery,
    process_preview,
    send_forward,
    send_ugoira_video,
    send_ugoira_zip,
    write_novel_file,
)
from .models import Artwork, Novel, SearchPage
from .pagination import (
    SearchCursor,
    SearchResultRef,
    SearchSession,
    prune_sessions,
    session_key,
    sessions,
)
from .pixiv import PixivAPIError, PixivClient, PixivNotFoundError, PixivR18Error, PixivResourceError

SEARCH_RE = re.compile(
    r"^/px搜索(?!(?:图片|漫画|小说)?下一页\s*$)"
    r"(?!(?:图片|漫画|小说)\s*$)(图片|漫画|小说)?\s*(\S.*?)\s*$"
)
NEXT_RE = re.compile(r"^/px搜索(图片|漫画|小说)?下一页\s*$")
DOWNLOAD_RE = re.compile(r"^/px下载(图片|漫画|小说)?\s*(\d+)\s*$")
RELATED_RE = re.compile(r"^/px相关\s*(0*[1-9][0-9]*)\s*$")
KINDS = {"图片": "image", "漫画": "manga", "小说": "novel"}

config = get_plugin_config(Config)
client = PixivClient(config)
preview_semaphore = asyncio.Semaphore(config.pixiv_preview_concurrency)
get_driver().on_shutdown(client.close)
get_driver().on_shutdown(ugoira.cleanup_pending_directories)

search_matcher = on_regex(SEARCH_RE.pattern, priority=10, block=True)
next_matcher = on_regex(NEXT_RE.pattern, priority=9, block=True)
download_matcher = on_regex(DOWNLOAD_RE.pattern, priority=10, block=True)
related_matcher = on_regex(RELATED_RE.pattern, priority=10, block=True)


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
    """解析下载命令，保留显式分类或索引优先的自动模式标记

    Args:
        text: 用户发送的完整下载命令文本

    Returns:
        下载分类与输入数字，命令格式不匹配时返回 None
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


async def _search_kind(kind: str, word: str, page: int) -> SearchPage:
    """调用指定分类的 Pixiv 搜索接口

    Args:
        kind: 请求的作品分类
        word: 用于匹配作品标签的搜索关键词
        page: 本批查询起始的 Pixiv 页码

    Returns:
        符合配置数量上限的该分类预览及实际分页信息

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
    return await method(word, config.pixiv_search_limit, page)


async def _search_previews(items: list[Artwork]) -> dict[int, bytes]:
    """有限并发处理第一页静态缩略图，动图与普通图片共用预览流程

    Args:
        items: 按结果顺序排列的插画，动图或漫画元数据

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
            已处理的静态 JPEG 内容，没有缩略图或处理失败时返回 None
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
    return {item.id: data for item, data in zip(items, previews, strict=True) if data is not None}


def _pagination_hint(session: SearchSession) -> str:
    """根据独立分类游标生成真实可用的翻页提示

    Args:
        session: 保存当前模式和成功查询页码的分页记录

    Returns:
        当前页码和仍然允许使用的翻页命令，无剩余页时说明已结束
    """
    pages = "，".join(
        f"{KIND_NAMES[kind]}第 {cursor.page} 页"
        for kind, cursor in session.cursors.items()
        if cursor.page
    )
    available = [kind for kind, cursor in session.cursors.items() if cursor.has_next]
    if not available:
        return f"当前{pages}，已经没有下一页"
    names = "、".join(KIND_NAMES[kind] for kind in available)
    branches = " 或 ".join(f"/px搜索{KIND_NAMES[kind]}下一页" for kind in available)
    choice = "，选择分类成功后仅继续该分类" if session.kind == "all" else ""
    return (
        f"当前{pages}\n{names}还有下一页，可使用 /px搜索下一页 继续搜索，或使用 {branches}{choice}"
    )


async def _send_result_packets(
    bot: Bot,
    event: MessageEvent,
    session: SearchSession,
    items: list[Artwork] | list[Novel],
    previews: dict[int, bytes],
    previous: SearchSession | None,
) -> None:
    """逐包发送并登记结果，首包成功后才替换原会话

    Args:
        bot: 用于调用 OneBot 消息接口的机器人实例
        event: 决定结果所属用户与聊天环境的消息事件
        session: 当前持锁的目标结果会话
        items: 按接口原始顺序排列的作品元数据
        previews: 按作品 ID 保存的可用预览
        previous: 发送前应保持不变的会话，首次查询时可为空
    """
    key = session_key(bot, event)
    packets = build_search_forward(
        items, "all", config.pixiv_forward_max_messages, int(bot.self_id), previews,
        start_index=session.next_result_index,
    )
    offset = 0
    for packet in packets:
        if sessions.get(key) is not previous:
            return
        await send_forward(bot, event, packet)
        if sessions.get(key) is not previous:
            return
        for item in items[offset : offset + len(packet)]:
            kind = "novel" if isinstance(item, Novel) else item.type
            session.results[session.next_result_index] = SearchResultRef(kind, item.id)
            session.next_result_index += 1
        offset += len(packet)
        if session.expires_at is None:
            session.expires_at = pagination.monotonic() + pagination.SEARCH_SESSION_TTL
        sessions[key] = session
        previous = session


async def _search_and_send(
    bot: Bot,
    event: MessageEvent,
    session: SearchSession,
    targets: dict[str, int],
    selected: str | None = None,
) -> None:
    """共用搜索与预览发送流程，推进解析成功的游标并按发送成功的分包登记编号

    Args:
        bot: 用于调用 OneBot 消息接口的机器人实例
        event: 触发命令的消息事件，决定回复会话
        session: 当前持有独立锁的搜索记录
        targets: 每个待查询分类对应的实际起始页码
        selected: 显式选择的分页分类，成功后将聚合模式收窄至该分类
    """
    key = session_key(bot, event)
    status = None
    previous = {kind: session.cursors[kind].page for kind in targets}
    aggregate = session.kind == "all"
    try:
        status = await bot.send(event, "正在搜索")
        results = await asyncio.gather(
            *(_search_kind(kind, session.word, page) for kind, page in targets.items()),
            return_exceptions=True,
        )
        if sessions.get(key) is not session:
            return
        successful = {
            kind: result
            for kind, result in zip(targets, results, strict=True)
            if isinstance(result, SearchPage)
        }
        for kind, result in successful.items():
            session.cursors[kind] = SearchCursor(result.page, result.has_next)
        if successful and session.expires_at is None:
            session.expires_at = pagination.monotonic() + pagination.SEARCH_SESSION_TTL
        if selected in successful:
            session.kind = selected
            session.cursors = {selected: session.cursors[selected]}
        previews = await _search_previews(
            [
                item
                for result in successful.values()
                for item in result.items
                if isinstance(item, Artwork)
            ]
        )
        await _recall(bot, status)
        status = None
        if sessions.get(key) is not session:
            return
        if len(successful) == len(targets) and all(not page.items for page in successful.values()):
            await bot.send(event, "没有搜索到相关内容")
        else:
            for current, result in zip(targets, results, strict=True):
                if sessions.get(key) is not session:
                    return
                if isinstance(result, BaseException):
                    logger.warning(
                        "Pixiv 搜索分页失败，关键词={}，分类={}，当前页={}，目标页={}，"
                        "上下文={}，聚合={}，异常={}",
                        session.word,
                        current,
                        previous[current],
                        targets[current],
                        key,
                        aggregate,
                        str(result),
                    )
                    await bot.send(event, f"{KIND_NAMES[current]}搜索失败，请重试")
                    continue
                if not result.items:
                    await bot.send(event, f"没有搜索到相关{KIND_NAMES[current]}")
                    continue
                try:
                    await _send_result_packets(
                        bot, event, session, result.items, previews, session
                    )
                except Exception as exc:
                    logger.error(
                        "Pixiv 搜索结果发送失败，关键词={}，分类={}，当前页={}，目标页={}，"
                        "上下文={}，聚合={}，异常={}",
                        session.word,
                        current,
                        previous[current],
                        targets[current],
                        key,
                        aggregate,
                        type(exc).__name__,
                    )
                    await bot.send(event, f"{KIND_NAMES[current]}搜索结果发送失败")
        if (
            successful
            and sessions.get(key) is session
            and (
                any(cursor.has_next for cursor in session.cursors.values())
                or any(page > 1 for page in targets.values())
            )
        ):
            await bot.send(event, _pagination_hint(session))
    except Exception as exc:
        logger.error(
            "Pixiv 搜索失败，关键词={}，当前页={}，目标页={}，上下文={}，聚合={}，异常={}",
            session.word,
            previous,
            targets,
            key,
            aggregate,
            type(exc).__name__,
        )
        if status is not None:
            await _recall(bot, status)
            status = None
        await bot.send(event, "搜索失败")
    finally:
        if status is not None:
            await _recall(bot, status)


async def _run_search(bot: Bot, event: MessageEvent, kind: str, word: str) -> None:
    """以新的搜索意图替换旧会话，首次查询成功后开始固定有效期

    Args:
        bot: 用于调用 OneBot 消息接口的机器人实例
        event: 触发命令的消息事件，决定用户与聊天环境
        kind: 搜索分类，all 表示聚合搜索
        word: 用于匹配作品标签的搜索关键词
    """
    prune_sessions()
    key = session_key(bot, event)
    kinds = list(KINDS.values()) if kind == "all" else [kind]
    session = SearchSession(word, kind, {item: SearchCursor() for item in kinds})
    sessions[key] = session
    try:
        async with session.lock:
            await _search_and_send(bot, event, session, dict.fromkeys(kinds, 1))
    finally:
        if session.expires_at is None and sessions.get(key) is session:
            del sessions[key]


async def _run_next(bot: Bot, event: MessageEvent, selected: str | None = None) -> None:
    """串行处理当前用户的下一页请求，获得锁后重新检查有效期和分类

    Args:
        bot: 用于调用 OneBot 消息接口的机器人实例
        event: 触发命令的消息事件，决定用户与聊天环境
        selected: 指定继续的分类，空值表示继续当前模式中的全部可用分类
    """
    key = session_key(bot, event)
    session = sessions.get(key)
    prune_sessions()
    if session is None:
        await bot.send(event, "没有可继续的搜索记录，请先进行搜索")
        return
    async with session.lock:
        if session.expired():
            if sessions.get(key) is session:
                del sessions[key]
            await bot.send(event, "搜索记录已超时，请重新搜索")
            return
        if sessions.get(key) is not session:
            await bot.send(event, "搜索记录已更新，请重新发送下一页命令")
            return
        if selected and session.kind not in ("all", selected):
            await bot.send(
                event,
                f"当前分页搜索已锁定为{KIND_NAMES[session.kind]}，"
                "请使用 /px搜索下一页 继续搜索，或重新发起搜索",
            )
            return
        targets = {
            kind: cursor.page + 1
            for kind, cursor in session.cursors.items()
            if cursor.has_next and (selected is None or kind == selected)
        }
        if not targets:
            label = KIND_NAMES[selected] if selected else ""
            await bot.send(event, f"{label}已经没有下一页，请重新搜索")
            return
        await _search_and_send(bot, event, session, targets, selected)


async def resolve_result_reference(
    bot: Bot, event: MessageEvent, number: int
) -> SearchResultRef | None:
    """持锁解析当前有效结果编号，不发送提示或访问 Pixiv

    Args:
        bot: 用于调用 OneBot 消息接口的机器人实例
        event: 触发命令的消息事件，决定用户与聊天环境
        number: 用户输入的结果编号或 Pixiv ID

    Returns:
        命中的不可变作品引用，无有效编号时返回 None
    """
    key = session_key(bot, event)
    session = sessions.get(key)
    prune_sessions()
    if session is None:
        return None
    async with session.lock:
        if sessions.get(key) is not session:
            return None
        if session.expired():
            del sessions[key]
            return None
        return session.results.get(number)


async def _run_related(bot: Bot, event: MessageEvent, number: int) -> None:
    """按当前索引或 Pixiv ID 查询相关作品，发送成功后建立新的结果会话

    Args:
        bot: 用于调用 OneBot 消息接口的机器人实例
        event: 决定结果所属用户与聊天环境的消息事件
        number: 优先作为当前有效结果编号的正整数，否则作为 Pixiv ID
    """
    reference = await resolve_result_reference(bot, event, number)
    key = session_key(bot, event)
    previous = sessions.get(key)
    work_id = reference.work_id if reference else number
    status = None
    try:
        status = await bot.send(event, "正在搜索")
        if reference:
            kind = reference.kind
        else:
            work = await _get_download_target("auto", work_id)
            kind = "novel" if isinstance(work, Novel) else work.type
        items = await client.get_related(kind, work_id, config.pixiv_search_limit)
        previews = await _search_previews([item for item in items if isinstance(item, Artwork)])
        await _recall(bot, status)
        status = None
        if sessions.get(key) is not previous:
            return
        if not items:
            await bot.send(event, "没有找到相关作品")
            return
        session = SearchSession("", "all", {})
        async with session.lock:
            await _send_result_packets(bot, event, session, items, previews, previous)
    except Exception:
        logger.exception("Pixiv 相关作品获取失败，作品 ID={}", work_id)
        if status is not None:
            await _recall(bot, status)
            status = None
        await bot.send(event, "相关作品获取失败")
    finally:
        if status is not None:
            await _recall(bot, status)


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


async def _send_ugoira_notice(bot: Bot, event: MessageEvent, content: str) -> dict | None:
    """尽力发送动图文本提示，提示失败不改变资源本身的发送结果

    Args:
        bot: 用于发送作品信息或状态提示的机器人实例
        event: 决定文本提示接收会话的消息事件
        content: 当前作品的元数据，处理状态或独立发送结果

    Returns:
        可供撤回状态提示的消息信息，文本发送失败时返回 None
    """
    try:
        return await bot.send(event, content)
    except Exception:
        logger.exception("Pixiv Ugoira 文本提示发送失败")
        return None


async def _run_ugoira_download(
    bot: Bot, event: MessageEvent, work: Artwork, status: dict
) -> None:
    """直接上传原始 ZIP 后发送兼容视频，回执超时保留资源并独立报告结果

    Args:
        bot: 用于调用文件上传和视频发送接口的机器人实例
        event: 触发下载的群聊或私聊事件
        work: 保留原始类型及限制级标记的动图详情
        status: 当前正在下载提示的可撤回消息信息
    """
    zip_result = None
    directory = None
    retain_files = False
    phase = "原始帧 ZIP 下载"
    try:
        if work.is_r18 and not config.pixiv_r18:
            raise PixivR18Error(f"{work.id}: R18 已关闭")
        meta = await client.get_ugoira_meta(work.id)
        directory = TemporaryDirectory(prefix="nonebot-pixiv-ugoira-")
        folder = Path(directory.name)
        archive = folder / f"{work.id}_ugoira.zip"
        output = folder / f"{work.id}_ugoira.mp4"
        await client.download_ugoira_archive(meta.original_src, archive)
        await _recall(bot, status)
        status = None
        await _send_ugoira_notice(bot, event, format_ugoira(work, meta))
        retain_files = True
        zip_result = await send_ugoira_zip(bot, event, archive, work.id)
        retain_files = zip_result is MediaSendResult.UNKNOWN
        phase = "MP4 生成"
        status = await _send_ugoira_notice(bot, event, "正在生成视频")
        try:
            await ugoira.convert_to_mp4(archive, meta, output)
        except Exception:
            logger.exception(
                "Pixiv Ugoira 转换失败，作品 ID={}，帧数={}，输出={}",
                work.id, len(meta.frames), output,
            )
            raise
        if status is not None:
            await _recall(bot, status)
            status = None
        phase = "MP4 视频发送"
        retain_files = True
        video_result = await send_ugoira_video(bot, event, output, work.id)
        retain_files = MediaSendResult.UNKNOWN in (zip_result, video_result)
        result_text = format_ugoira_delivery(zip_result, video_result)
        if result_text:
            await _send_ugoira_notice(bot, event, result_text)
    except PixivR18Error:
        await _send_ugoira_notice(bot, event, "已关闭 R18，无法下载该作品")
    except Exception as exc:
        logger.exception("Pixiv Ugoira {}失败，作品 ID={}", phase, work.id)
        prefix = {
            MediaSendResult.SUCCESS: "原始帧 ZIP 已发送，但 ",
            MediaSendResult.FAILED: "原始帧 ZIP 发送失败，且 ",
            MediaSendResult.UNKNOWN: "原始帧 ZIP 发送结果未确认，请检查聊天记录，",
        }.get(zip_result, "")
        suffix = f"，{exc}" if isinstance(exc, PixivResourceError) else "，请查看控制台日志"
        await _send_ugoira_notice(bot, event, f"{prefix}{phase}失败{suffix}")
    finally:
        try:
            if status is not None:
                await _recall(bot, status)
        finally:
            if directory is not None:
                await ugoira.release_directory(directory, retain_files)


async def _run_download(bot: Bot, event: MessageEvent, kind: str, work_id: int) -> None:
    """按作品类型编排普通下载或动图 ZIP 与视频下载，结束后清理临时文件

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
        if isinstance(target, Artwork) and target.is_ugoira:
            recalled = True
            await _run_ugoira_download(bot, event, target, status)
            return
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
    """无分类下载优先解析当前索引，显式分类始终按 Pixiv ID 下载

    Args:
        bot: 用于调用 OneBot 消息接口的机器人实例
        event: 触发命令的消息事件，决定回复会话
    """
    parsed = parse_download(event.get_plaintext())
    if parsed:
        kind, number = parsed
        if kind == "auto":
            reference = await resolve_result_reference(bot, event, number)
            if reference:
                parsed = reference.kind, reference.work_id
        await _run_download(bot, event, *parsed)


@related_matcher.handle()
async def handle_related(bot: Bot, event: MessageEvent) -> None:
    """解析相关作品查询命令，仅接受正整数

    Args:
        bot: 用于调用 OneBot 消息接口的机器人实例
        event: 包含当前结果编号或 Pixiv ID 的 OneBot 消息事件
    """
    match = RELATED_RE.fullmatch(event.get_plaintext())
    if match:
        await _run_related(bot, event, int(match.group(1)))


@next_matcher.handle()
async def handle_next(bot: Bot, event: MessageEvent) -> None:
    """解析不带关键词的翻页命令并继续当前用户的分页记录

    Args:
        bot: 用于调用 OneBot 消息接口的机器人实例
        event: 触发翻页命令的 OneBot 消息事件
    """
    match = NEXT_RE.fullmatch(event.get_plaintext())
    if match:
        await _run_next(bot, event, KINDS.get(match.group(1)))

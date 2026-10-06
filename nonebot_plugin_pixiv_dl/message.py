import asyncio
import re
from collections.abc import Awaitable
from enum import Enum
from io import BytesIO
from pathlib import Path

import httpcore
import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import (
    ActionFailed,
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from PIL import Image, ImageFilter

from .models import Artwork, Novel, NovelSeries, UgoiraMeta

KIND_NAMES = {"image": "图片", "manga": "漫画", "novel": "小说"}
MEDIA_SEND_TIMEOUT = 180


# @Author: DuoDuoJuZi
# @Date: 2026-09-29
def format_artwork(work: Artwork, sent_pages: int | None = None) -> str:
    """生成作品元数据，归入图片的 Ugoira 额外显示动图标记

    Args:
        work: 待展示的插画或漫画详情
        sent_pages: 本次发送的页数，空值表示不展示发送数量

    Returns:
        包含作品基本信息的多行文本，不包含原图地址
    """
    lines = [
        f"类型：{KIND_NAMES[work.type]}{'（动图）' if work.is_ugoira else ''}",
        f"标题：{work.title}",
        f"PID：{work.id}",
        f"作者：{work.user_name}",
        f"作者 ID：{work.user_id}",
        f"标签：{'、'.join(work.tags) or '无'}",
        f"作品总页数：{work.page_count}",
        f"R18：{'是' if work.is_r18 else '否'}",
    ]
    if sent_pages is not None:
        lines.append(f"本次发送：{sent_pages}")
    if work.description:
        lines.append(f"简介：{work.description[:160]}")
    return "\n".join(lines)


def format_novel(novel: Novel) -> str:
    """生成小说元数据摘要

    Args:
        novel: 待展示的小说详情

    Returns:
        包含作者和系列等信息的多行文本，不包含小说正文
    """
    lines = [
        "类型：小说",
        f"标题：{novel.title}",
        f"小说 ID：{novel.id}",
        f"作者：{novel.user_name}",
        f"作者 ID：{novel.user_id}",
        f"标签：{'、'.join(novel.tags) or '无'}",
        f"R18：{'是' if novel.is_r18 else '否'}",
    ]
    if novel.series_id:
        lines.extend([f"系列：{novel.series_title}", f"系列 ID：{novel.series_id}"])
    if novel.description:
        lines.append(f"简介：{novel.description[:160]}")
    return "\n".join(lines)


def format_series(series: NovelSeries, downloaded: int) -> str:
    """生成小说系列摘要并展示本次下载数量

    Args:
        series: 待展示的小说系列详情
        downloaded: 本次实际生成文件的章节数

    Returns:
        包含系列总章节数与本次下载数量的多行文本
    """
    return "\n".join(
        [
            "类型：小说系列",
            f"标题：{series.title}",
            f"系列 ID：{series.id}",
            f"作者：{series.user_name}",
            f"作者 ID：{series.user_id}",
            f"标签：{'、'.join(series.tags) or '无'}",
            f"R18：{'是' if series.is_r18 else '否'}",
            f"系列总章节：{series.total}",
            f"本次下载：{downloaded}",
        ]
    )


def process_preview(data: bytes, is_r18: bool, max_edge: int) -> bytes:
    """在内存中缩放预览并编码为 JPEG，限制级作品始终进行轻度高斯模糊

    Args:
        data: 从搜索接口提供的预览地址下载的图片内容
        is_r18: 作品是否被 Pixiv 标记为限制级内容
        max_edge: 处理后图片最长边的像素上限

    Returns:
        保持宽高比并以白底处理透明通道的 JPEG 内容，质量为 80

    Raises:
        OSError: 图片无法解码或编码
        ValueError: 图片处理参数或颜色模式无效
    """
    with Image.open(BytesIO(data)) as source:
        source.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        rgba = source.convert("RGBA")
        image = Image.new("RGB", rgba.size, "white")
        image.paste(rgba, mask=rgba.getchannel("A"))
        if is_r18:
            radius = max(1.0, min(3.0, min(image.size) * 0.01))
            image = image.filter(ImageFilter.GaussianBlur(radius))
        output = BytesIO()
        image.save(output, format="JPEG", quality=80, optimize=True)
        return output.getvalue()


def build_search_forward(
    items: list[Artwork] | list[Novel],
    kind: str,
    max_messages: int,
    self_id: int,
    previews: dict[int, bytes] | None = None,
    start_index: int = 1,
) -> list[list[MessageSegment]]:
    """按结果数量拆分搜索转发，每个作品节点包含连续编号，元数据和可用的预览

    Args:
        items: 按结果顺序排列的作品元数据
        kind: 展示分类，all 表示使用每个作品的实际分类
        max_messages: 每个合并转发允许的最大节点数，至少为 1
        self_id: 合并转发节点使用的机器人 QQ 号
        previews: 按作品 ID 保存的已处理预览内容，空值表示仅展示元数据
        start_index: 本批首个结果在当前搜索会话中的编号

    Returns:
        按结果顺序排列的合并转发分包列表
    """
    packets = []
    for start in range(0, len(items), max_messages):
        packet = []
        for index, item in enumerate(items[start : start + max_messages], start_index + start):
            item_kind = "novel" if isinstance(item, Novel) else item.type
            label = KIND_NAMES[item_kind if kind == "all" else kind]
            content = format_novel(item) if isinstance(item, Novel) else format_artwork(item)
            content = f"[{index}]\n{content}"
            if isinstance(item, Artwork) and previews and item.id in previews:
                content = Message(
                    [MessageSegment.text(content), MessageSegment.image(previews[item.id])]
                )
            packet.append(
                MessageSegment.node_custom(self_id, f"Pixiv {label}", content)
            )
        packets.append(packet)
    return packets


def build_artwork_forward(
    work: Artwork, images: list[bytes], max_messages: int, self_id: int
) -> list[list[MessageSegment]]:
    """构建元数据开头的图片合并转发，每个后续节点展示一页图片

    Args:
        work: 图片所属的插画或漫画详情
        images: 按作品页序排列的已下载图片内容
        max_messages: 每包节点上限，至少为 2，包含首节点的元数据或续页提示
        self_id: 合并转发节点使用的机器人 QQ 号

    Returns:
        按页序排列的合并转发分包列表
    """
    packets = []
    capacity = max_messages - 1
    label = f"Pixiv {KIND_NAMES[work.type]}"
    for start in range(0, len(images), capacity):
        heading = (
            format_artwork(work, len(images))
            if start == 0
            else f"《{work.title}》续页\nPID：{work.id}\n从第 {start + 1} 页继续"
        )
        packet = [MessageSegment.node_custom(self_id, label, heading)]
        for index, image in enumerate(images[start : start + capacity], start + 1):
            content = Message(
                [MessageSegment.text(f"第 {index} 页\n"), MessageSegment.image(image)]
            )
            packet.append(MessageSegment.node_custom(self_id, label, content))
        packets.append(packet)
    return packets


def build_novel_forward(
    metadata: str, paths: list[Path], max_messages: int, self_id: int
) -> list[list[MessageSegment]]:
    """构建小说文件合并转发，每个后续节点附带一个 TXT 文件

    Args:
        metadata: 首个分包展示的小说或系列元数据文本
        paths: 按章节顺序排列的本地 TXT 文件路径
        max_messages: 每包节点上限，至少为 2，包含首节点的元数据或续传提示
        self_id: 合并转发节点使用的机器人 QQ 号

    Returns:
        按章节顺序排列的合并转发分包列表
    """
    packets = []
    capacity = max_messages - 1
    for start in range(0, len(paths), capacity):
        heading = metadata if start == 0 else f"小说续传\n从第 {start + 1} 个文件继续"
        packet = [MessageSegment.node_custom(self_id, "Pixiv 小说", heading)]
        for path in paths[start : start + capacity]:
            content = Message(
                [file_segment(path)]
            )
            packet.append(MessageSegment.node_custom(self_id, "Pixiv 小说", content))
        packets.append(packet)
    return packets


def file_segment(path: Path) -> MessageSegment:
    """复用 NapCat 文件扩展表示协议端可访问的本地临时文件

    Args:
        path: 发送调用结束前必须保持存在的文件路径

    Returns:
        带有安全文件名和绝对路径的文件消息段
    """
    return MessageSegment("file", {"file": str(path.resolve()), "name": path.name})


def format_ugoira(work: Artwork, meta: UgoiraMeta) -> str:
    """生成用于普通消息发送的动图元数据

    Args:
        work: 已通过限制级检查的动图作品详情
        meta: 提供帧数和完整动画时长的动图元数据

    Returns:
        包含作品信息，帧数和完整播放时长的文本
    """
    return (
        f"{format_artwork(work)}\n帧数：{len(meta.frames)}\n"
        f"动画时长：{sum(frame.delay for frame in meta.frames) / 1000:.3f} 秒"
    )


class MediaSendResult(Enum):
    """区分大资源发送成功，明确失败和回执超时未确认"""

    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"


def is_timeout_error(error: BaseException) -> bool:
    """检查异常链中是否存在发送回执超时，避免循环引用

    Args:
        error: Adapter 或底层网络库抛出的发送异常

    Returns:
        异常自身或其原因与上下文中是否包含超时
    """
    pending = [error]
    visited = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(
            current, (asyncio.TimeoutError, httpx.TimeoutException, httpcore.TimeoutException)
        ):
            return True
        pending.extend(
            linked for linked in (current.__cause__, current.__context__) if linked is not None
        )
    return False


async def _send_media(
    operation: Awaitable[object], work_id: int, label: str
) -> MediaSendResult:
    """执行一次大资源发送，将回执超时标记为未确认并记录完整异常

    Args:
        operation: 已选定群聊或私聊目标的 OneBot 调用
        work_id: 用于定位发送日志的 Pixiv 作品 ID
        label: 当前发送资源的展示名称

    Returns:
        本次调用的三态发送结果，超时不会自动重试
    """
    try:
        response = await operation
        if isinstance(response, dict) and (
            response.get("status") == "failed" or response.get("retcode", 0) != 0
        ):
            raise ActionFailed(**response)
        return MediaSendResult.SUCCESS
    except Exception as exc:
        unknown = not isinstance(exc, ActionFailed) and is_timeout_error(exc)
        logger.exception(
            "Pixiv Ugoira {}{}，作品 ID={}",
            label,
            "发送结果未确认" if unknown else "发送失败",
            work_id,
        )
        return MediaSendResult.UNKNOWN if unknown else MediaSendResult.FAILED


async def send_ugoira_zip(
    bot: Bot, event: MessageEvent, path: Path, work_id: int
) -> MediaSendResult:
    """使用群聊或私聊文件上传接口直接发送原始 ZIP

    Args:
        bot: 支持 NapCat 文件上传扩展的机器人实例
        event: 决定文件接收会话的下载消息事件
        path: 在协议端处理期间必须保持存在的本地 ZIP
        work_id: 用于定位资源发送日志的作品 ID

    Returns:
        原始帧 ZIP 的三态发送结果
    """
    params = {"file": str(path.resolve()), "name": path.name, "_timeout": MEDIA_SEND_TIMEOUT}
    if isinstance(event, GroupMessageEvent):
        operation = bot.call_api("upload_group_file", group_id=event.group_id, **params)
    else:
        operation = bot.call_api("upload_private_file", user_id=event.user_id, **params)
    return await _send_media(operation, work_id, "原始帧 ZIP")


async def send_ugoira_video(
    bot: Bot, event: MessageEvent, path: Path, work_id: int
) -> MediaSendResult:
    """以视频消息发送兼容 MP4 并延长单次 API 回执等待

    Args:
        bot: 支持 OneBot 视频消息的机器人实例
        event: 决定视频接收会话的下载消息事件
        path: 在协议端处理期间必须保持存在的本地 MP4
        work_id: 用于定位资源发送日志的作品 ID

    Returns:
        MP4 视频的三态发送结果
    """
    return await _send_media(
        bot.send(event, MessageSegment.video(path.resolve()), _timeout=MEDIA_SEND_TIMEOUT),
        work_id,
        "MP4 视频",
    )


def format_ugoira_delivery(zip_result: MediaSendResult, video_result: MediaSendResult) -> str:
    """根据两个资源的独立发送结果生成准确的部分成功提示

    Args:
        zip_result: 原始帧 ZIP 的发送结果
        video_result: 兼容 MP4 的发送结果

    Returns:
        需要补充发送的结果文本，两个资源均成功时返回空文本
    """
    success, failed, unknown = MediaSendResult
    messages = {
        (success, success): "",
        (failed, success): "MP4 已发送，但原始帧 ZIP 发送失败",
        (unknown, success): "MP4 已发送，原始帧 ZIP 发送结果未确认，请检查聊天记录",
        (success, failed): "原始帧 ZIP 已发送，但 MP4 视频发送失败，请查看控制台日志",
        (success, unknown): "原始帧 ZIP 已发送，MP4 发送结果确认超时，请检查聊天记录",
        (failed, failed): "原始帧 ZIP 和 MP4 视频发送失败，请查看控制台日志",
        (unknown, unknown): "原始帧 ZIP 和 MP4 发送结果确认超时，请检查聊天记录",
        (failed, unknown): "原始帧 ZIP 发送失败，MP4 发送结果确认超时，请检查聊天记录",
        (unknown, failed): "原始帧 ZIP 发送结果未确认，MP4 视频发送失败，请检查聊天记录",
    }
    return messages[zip_result, video_result]


async def send_forward(bot: Bot, event: MessageEvent, packet: list[MessageSegment]) -> None:
    """向触发命令的群聊或私聊发送合并转发

    Args:
        bot: 用于调用 OneBot 消息接口的机器人实例
        event: 触发命令的消息事件，决定回复会话
        packet: 按展示顺序排列的合并转发节点
    """
    if isinstance(event, GroupMessageEvent):
        await bot.call_api(
            "send_group_forward_msg", group_id=event.group_id, messages=packet
        )
    else:
        await bot.call_api(
            "send_private_forward_msg", user_id=event.user_id, messages=packet
        )


def safe_filename(title: str) -> str:
    """清理标题中的路径分隔符和文件名非法字符

    Args:
        title: 用于生成文件名的作品原始标题

    Returns:
        最多保留 48 个字符的安全名称，清理后为空时使用默认名称
    """
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title).replace("..", "_")
    return cleaned.strip(" .")[:48] or "未命名"


def write_novel_file(novel: Novel, folder: Path, number: int | None = None) -> Path:
    """将小说元数据和正文写入 UTF-8 编码的 TXT 文件

    Args:
        novel: 包含完整正文的小说详情
        folder: 用于保存本次下载文件的临时目录
        number: 系列章节序号，空值表示使用小说 ID 区分独立作品

    Returns:
        已写入的小说文件路径

    Raises:
        OSError: 目标目录不可写或文件写入失败
    """
    prefix = f"{number:02d} - " if number is not None else ""
    suffix = "" if number is not None else f" - {novel.id}"
    path = folder / f"{prefix}{safe_filename(novel.title)}{suffix}.txt"
    header = [
        novel.title,
        f"作者：{novel.user_name}",
        f"Pixiv ID：{novel.id}",
    ]
    if novel.series_id:
        header.append(f"系列：{novel.series_title}")
    path.write_text("\n".join(header) + "\n\n" + novel.content, encoding="utf-8")
    return path

import asyncio
import shutil
import stat
import struct
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import TypeVar
from zipfile import BadZipFile, ZipFile, ZipInfo

from imageio_ffmpeg import get_ffmpeg_exe
from PIL import Image, ImageFilter

from .models import UgoiraMeta
from .pixiv import PixivError, PixivResourceError

PREVIEW_ARCHIVE_LIMIT = 128 * 1024**2
FRAME_BYTES_LIMIT = 256 * 1024**2
EXTRACTED_BYTES_LIMIT = 8 * 1024**3
CONVERT_TIMEOUT = 600
convert_semaphore = asyncio.Semaphore(1)
preview_semaphore = asyncio.Semaphore(1)
Result = TypeVar("Result")


# @Author: DuoDuoJuZi
# @Date: 2026-10-03
class UgoiraError(PixivError):
    """表示动图帧资源或媒体转换失败"""


async def _run_sync(function: Callable[..., Result], *args: object) -> Result:
    """在线程内执行媒体操作，取消时等待文件使用结束后再释放资源

    Args:
        function: 需要移出事件循环的文件或图像处理操作
        args: 传递给媒体操作的位置参数

    Returns:
        媒体操作的执行结果

    Raises:
        Exception: 媒体操作自身失败
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


def _frame_entries(archive: ZipFile, meta: UgoiraMeta) -> list[ZipInfo]:
    """按元数据顺序检查所有声明帧，忽略未声明的 ZIP 成员

    Args:
        archive: 已打开的动图 ZIP
        meta: 已校验文件名和延时的动图元数据

    Returns:
        按动画时序排列且可以安全读取的 ZIP 成员

    Raises:
        UgoiraError: 声明帧缺失，重复，加密或不是普通文件
        PixivResourceError: 单帧或总解压大小超过保护上限
    """
    by_name = {}
    for info in archive.infolist():
        if info.filename in by_name:
            raise UgoiraError("Ugoira ZIP 存在重复文件名")
        by_name[info.filename] = info
    entries = []
    total = 0
    for frame in meta.frames:
        info = by_name.get(frame.file)
        if info is None:
            raise UgoiraError(f"Ugoira ZIP 缺少帧 {frame.file}")
        mode = stat.S_IFMT(info.external_attr >> 16)
        if info.is_dir() or mode not in (0, stat.S_IFREG) or info.flag_bits & 1:
            raise UgoiraError(f"Ugoira ZIP 帧不是未加密的普通文件，{frame.file}")
        total += info.file_size
        if info.file_size > FRAME_BYTES_LIMIT or total > EXTRACTED_BYTES_LIMIT:
            raise PixivResourceError("Ugoira 解压超过单帧 256 MiB 或总量 8 GiB 上限")
        entries.append(info)
    return entries


def build_preview_gif(
    archive_path: Path, meta: UgoiraMeta, is_r18: bool, max_edge: int, max_frames: int
) -> bytes:
    """均匀选取预览帧并合并跳过帧的延时，限制级输出逐帧模糊

    Args:
        archive_path: 仅从 src 下载的临时 ZIP 路径
        meta: 包含原始帧顺序及毫秒延时的动图元数据
        is_r18: 是否对每张输出帧进行高斯模糊
        max_edge: 预览最长边的像素上限
        max_frames: 均匀采样后允许保留的最大帧数

    Returns:
        无限循环播放的小尺寸 GIF 内容

    Raises:
        UgoiraError: ZIP 损坏，帧缺失或预览编码失败
        PixivResourceError: ZIP 解压规模超过保护上限
    """
    images = []
    durations = []
    count = min(len(meta.frames), max_frames)
    boundaries = [index * len(meta.frames) // count for index in range(count + 1)]
    total_ticks = (sum(frame.delay for frame in meta.frames) + 5) // 10
    if total_ticks < count:
        raise UgoiraError("Ugoira 时序过短，无法保持 GIF 预览总时长")
    elapsed = 0
    ticks = 0
    try:
        with ZipFile(archive_path) as archive:
            entries = _frame_entries(archive, meta)
            for index, (start, end) in enumerate(zip(boundaries[:-1], boundaries[1:], strict=True)):
                elapsed += sum(frame.delay for frame in meta.frames[start:end])
                next_ticks = min(
                    total_ticks - count + index + 1, max(ticks + 1, (elapsed + 5) // 10)
                )
                durations.append((next_ticks - ticks) * 10)
                ticks = next_ticks
                with archive.open(entries[start]) as stream, Image.open(stream) as source:
                    source.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                    rgba = source.convert("RGBA")
                    image = Image.new("RGB", rgba.size, "white")
                    image.paste(rgba, mask=rgba.getchannel("A"))
                    if is_r18:
                        radius = max(1.0, min(3.0, min(image.size) * 0.01))
                        image = image.filter(ImageFilter.GaussianBlur(radius))
                    images.append(image.quantize(colors=256, method=Image.Quantize.MEDIANCUT))
        output = BytesIO()
        images[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=images[1:],
            duration=durations,
            loop=0,
            optimize=False,
            disposal=2,
        )
        return output.getvalue()
    except (BadZipFile, OSError, ValueError, OverflowError) as exc:
        raise UgoiraError("Ugoira GIF 预览生成失败") from exc
    finally:
        for image in images:
            image.close()


async def build_preview(
    archive_path: Path, meta: UgoiraMeta, is_r18: bool, max_edge: int, max_frames: int
) -> bytes:
    """在线程中生成 GIF，取消时等待线程结束以安全清理临时文件

    Args:
        archive_path: 从 src 下载的临时 ZIP 路径
        meta: 保留全部原始帧顺序与延时的动图元数据
        is_r18: 是否逐帧模糊输出预览
        max_edge: GIF 最长边的像素上限
        max_frames: GIF 最多保留的采样帧数

    Returns:
        已完成必要模糊处理的 GIF 字节

    Raises:
        UgoiraError: 预览帧读取或编码失败
        PixivResourceError: 解压规模超过保护上限
    """
    return await _run_sync(build_preview_gif, archive_path, meta, is_r18, max_edge, max_frames)


def _frame_size(path: Path) -> tuple[int, int]:
    """仅读取帧头校验尺寸及位深，避免视频转换隐式缩放或丢失透明度

    Args:
        path: 从 ZIP 安全写出的单帧路径

    Returns:
        适用于无损 RGB 编码的原始宽高

    Raises:
        UgoiraError: 图像不是至多 8 位的不透明 JPEG 或 PNG，或图像头损坏
    """
    with path.open("rb") as source:
        signature = source.read(8)
        if signature == b"\x89PNG\r\n\x1a\n":
            size = None
            while header := source.read(8):
                length, kind = struct.unpack(">I4s", header)
                if kind == b"IHDR":
                    width, height, depth, color, _, _, _ = struct.unpack(
                        ">IIBBBBB", source.read(13)
                    )
                    if depth > 8 or color not in (0, 2, 3):
                        raise UgoiraError("PNG 位深或透明通道无法通过 RGB MP4 无损保留")
                    size = (width, height)
                    source.seek(4, 1)
                elif kind == b"tRNS":
                    raise UgoiraError("PNG 透明通道无法通过 RGB MP4 无损保留")
                elif kind == b"IDAT" and size:
                    return size
                else:
                    source.seek(length + 4, 1)
        elif signature[:2] == b"\xff\xd8":
            source.seek(2)
            while source.tell() < min(path.stat().st_size, 1024**2):
                if source.read(1) != b"\xff":
                    break
                marker = source.read(1)
                while marker == b"\xff":
                    marker = source.read(1)
                length = int.from_bytes(source.read(2), "big")
                if marker in (b"\xc0", b"\xc1", b"\xc2"):
                    depth, height, width, components = struct.unpack(">BHHB", source.read(6))
                    if depth != 8 or components not in (1, 3):
                        raise UgoiraError("JPEG 位深或颜色模式无法通过 RGB MP4 无损保留")
                    return width, height
                if length < 2:
                    break
                source.seek(length - 2, 1)
    raise UgoiraError("Ugoira 帧头无效或图像格式不受支持")


def prepare_timeline(archive_path: Path, meta: UgoiraMeta, folder: Path) -> Path:
    """流式提取全部声明帧并生成毫秒精度的 FFmpeg 时间轴

    Args:
        archive_path: 原封不动保存的 originalSrc ZIP 路径
        meta: 提供全部帧顺序和原始延时的动图元数据
        folder: 当前请求独占的临时目录

    Returns:
        包含全部帧及尾帧结束标记的 ffconcat 路径

    Raises:
        UgoiraError: ZIP 损坏，帧缺失，尺寸不一致或帧格式不适用
        PixivResourceError: 解压规模超过保护上限
        OSError: 临时帧或时间轴无法写入
    """
    frames_dir = folder / "frames"
    frames_dir.mkdir()
    lines = ["ffconcat version 1.0"]
    dimensions = None
    try:
        with ZipFile(archive_path) as archive:
            entries = _frame_entries(archive, meta)
            for index, (frame, info) in enumerate(zip(meta.frames, entries, strict=True)):
                extension = "jpg" if meta.mime_type == "image/jpeg" else "png"
                relative = f"frames/{index:06d}.{extension}"
                path = folder / relative
                with archive.open(info) as source, path.open("wb") as output:
                    shutil.copyfileobj(source, output, length=256 * 1024)
                size = _frame_size(path)
                if dimensions is not None and size != dimensions:
                    raise UgoiraError("Ugoira 帧尺寸不一致，拒绝隐式缩放")
                dimensions = size
                lines.extend(
                    [
                        f"file '{relative}'",
                        "option framerate 1000",
                        f"duration {frame.delay // 1000}.{frame.delay % 1000:03d}",
                    ]
                )
            lines.extend([f"file '{relative}'", "option framerate 1000"])
    except (BadZipFile, struct.error) as exc:
        raise UgoiraError("Ugoira ZIP 或帧内容无效") from exc
    timeline = folder / "timeline.ffconcat"
    timeline.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return timeline


def ffmpeg_arguments(timeline: Path, output: Path, meta: UgoiraMeta) -> list[str]:
    """生成全尺寸无损 RGB 编码参数，保留每帧时间并显式设置尾帧时长

    Args:
        timeline: 按原始顺序列出全部帧的本地时间轴
        output: 正式 MP4 的目标路径
        meta: 用于保留全部帧及最后一帧停留时间的原始元数据

    Returns:
        不含可执行文件名的 FFmpeg 参数列表
    """
    return [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-xerror",
        "-threads",
        "1",
        "-err_detect",
        "explode",
        "-f",
        "concat",
        "-safe",
        "0",
        "-protocol_whitelist",
        "file",
        "-i",
        str(timeline),
        "-map",
        "0:v:0",
        "-an",
        "-noautoscale",
        "-filter_threads",
        "1",
        "-c:v",
        "libx264rgb",
        "-crf",
        "0",
        "-preset",
        "ultrafast",
        "-threads",
        "1",
        "-pix_fmt",
        "rgb24",
        "-fps_mode",
        "passthrough",
        "-enc_time_base",
        "1:1000",
        "-video_track_timescale",
        "1000",
        "-frames:v",
        str(len(meta.frames)),
        "-bsf:v",
        f"setts=duration=if(eq(N\\,{len(meta.frames) - 1})\\,{meta.frames[-1].delay}\\,DURATION)",
        str(output),
    ]


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    """终止未完成编码并等待进程退出，必要时强制杀死

    Args:
        process: 当前请求拥有的 FFmpeg 子进程
    """
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


async def convert_to_mp4(archive_path: Path, meta: UgoiraMeta, output: Path) -> None:
    """限制整个进程同时只转换一个动图，超时或取消后回收 FFmpeg

    Args:
        archive_path: 已下载并发送的同一个 originalSrc ZIP 路径
        meta: 提供全部原始帧及逐帧延时的动图元数据
        output: 当前独占临时目录中的 MP4 路径

    Raises:
        UgoiraError: FFmpeg 不可用，转换超时，编码失败或输出为空
        PixivResourceError: 解压资源超过保护上限
        OSError: 临时文件无法读取或写入
    """
    async with convert_semaphore:
        timeline = await _run_sync(prepare_timeline, archive_path, meta, output.parent)
        try:
            executable = await _run_sync(get_ffmpeg_exe)
        except RuntimeError as exc:
            raise UgoiraError(
                "FFmpeg 不可用，当前平台需要包含可执行文件的 imageio-ffmpeg wheel"
            ) from exc
        log_path = output.parent / "ffmpeg.stderr"
        timed_out = False
        with log_path.open("wb") as stderr:
            try:
                process = await asyncio.create_subprocess_exec(
                    executable,
                    *ffmpeg_arguments(timeline, output, meta),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=stderr,
                )
            except (OSError, NotImplementedError) as exc:
                raise UgoiraError("FFmpeg 无法启动，请检查运行平台和异步子进程支持") from exc
            try:
                await asyncio.wait_for(process.wait(), CONVERT_TIMEOUT)
            except asyncio.TimeoutError:
                timed_out = True
            finally:
                await _stop_process(process)
        if timed_out or process.returncode:
            with log_path.open("rb") as log:
                log.seek(max(0, log_path.stat().st_size - 16384))
                error = log.read().decode("utf-8", errors="replace")
            reason = f"转换超过 {CONVERT_TIMEOUT} 秒上限" if timed_out else "编码失败"
            raise UgoiraError(f"FFmpeg {reason}，exit={process.returncode}，stderr={error}")
        if not output.is_file() or not output.stat().st_size:
            raise UgoiraError("FFmpeg 输出的 MP4 文件为空")

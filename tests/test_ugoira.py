import asyncio
import re
import subprocess
import threading
import time
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from zipfile import ZipFile, ZipInfo

import pytest
from imageio_ffmpeg import get_ffmpeg_exe
from PIL import Image, ImageChops, ImageSequence

from nonebot_plugin_pixiv_dl import ugoira
from nonebot_plugin_pixiv_dl.models import UgoiraFrame, UgoiraMeta


# @Author: DuoDuoJuZi
# @Date: 2026-10-03
def make_archive(
    count: int = 3,
    size: tuple[int, int] = (65, 33),
    kind: str = "PNG",
    delays: list[int] | None = None,
) -> tuple[bytes, UgoiraMeta]:
    """生成带条纹和不同颜色的帧 ZIP，故意倒序存储成员

    Args:
        count: 合成动画包含的原始帧数
        size: 每张原始帧的像素宽高
        kind: 合成帧使用的图像编码格式
        delays: 每帧原始毫秒延时，空值表示交替使用三种延时

    Returns:
        原始 ZIP 字节以及按照正确播放顺序排列的元数据
    """
    extension = "jpg" if kind == "JPEG" else "png"
    timing = delays or [41, 83, 127]
    frames = [
        UgoiraFrame(f"{index:06d}.{extension}", timing[index % len(timing)])
        for index in range(count)
    ]
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for index in reversed(range(count)):
            image = Image.new("RGB", size, (index * 37 % 256, index * 53 % 256, 255))
            for left in range(0, size[0], 8):
                image.paste((255, index * 19 % 256, 0), (left, 0, left + 4, size[1]))
            encoded = BytesIO()
            image.save(encoded, format=kind)
            archive.writestr(frames[index].file, encoded.getvalue())
        archive.writestr("../ignored.txt", b"ignored")
    return output.getvalue(), UgoiraMeta(
        "https://i.pximg.net/preview.zip",
        "https://i.pximg.net/original.zip",
        "image/jpeg" if kind == "JPEG" else "image/png",
        frames,
    )


@pytest.mark.parametrize("restricted", [False, True])
def test_gif_sampling_timing_and_every_frame_blur(tmp_path: Path, restricted: bool) -> None:
    """验证完整范围采样，尺寸限制，总时长及每帧模糊结果

    Args:
        tmp_path: 当前测试独占的临时目录
        restricted: 是否验证逐帧限制级模糊
    """
    data, meta = make_archive(100, (321, 161))
    archive = tmp_path / "preview.zip"
    archive.write_bytes(data)
    result = ugoira.build_preview_gif(archive, meta, restricted, 128, 20)
    plain = ugoira.build_preview_gif(archive, meta, False, 128, 20)
    with Image.open(BytesIO(result)) as gif, Image.open(BytesIO(plain)) as baseline:
        assert gif.is_animated
        assert gif.n_frames == 20
        assert max(gif.size) <= 128
        duration = 0
        for index, frame in enumerate(ImageSequence.Iterator(gif)):
            duration += frame.info["duration"]
            baseline.seek(index)
            if restricted:
                difference = ImageChops.difference(frame.convert("RGB"), baseline.convert("RGB"))
                assert difference.getbbox()
        assert abs(duration - sum(frame.delay for frame in meta.frames)) <= 10
        assert gif.info["loop"] == 0


def test_short_gif_keeps_all_frames(tmp_path: Path) -> None:
    """验证小于预览帧数上限的动图保留所有不同帧

    Args:
        tmp_path: 当前测试独占的临时目录
    """
    data, meta = make_archive()
    archive = tmp_path / "preview.zip"
    archive.write_bytes(data)
    result = ugoira.build_preview_gif(archive, meta, False, 256, 60)
    with Image.open(BytesIO(result)) as image:
        assert image.n_frames == 3
        assert image.size == (65, 33)


def test_full_timeline_uses_metadata_order_all_frames_and_delays(tmp_path: Path) -> None:
    """验证正式转换忽略预览上限并完整保留乱序 ZIP 的动画时序

    Args:
        tmp_path: 当前测试独占的临时目录
    """
    data, meta = make_archive(100, delays=[40, 80, 120])
    archive = tmp_path / "original.zip"
    archive.write_bytes(data)
    timeline = ugoira.prepare_timeline(archive, meta, tmp_path).read_text(encoding="utf-8")
    assert timeline.count("\nfile ") == 101
    assert timeline.count("\nduration ") == 100
    assert "duration 0.040" in timeline
    assert "duration 0.080" in timeline
    assert "duration 0.120" in timeline
    assert timeline.endswith("file 'frames/000099.png'\noption framerate 25\n")
    assert "option framerate 1000\n" not in timeline
    assert not (tmp_path.parent / "ignored.txt").exists()
    with ZipFile(archive) as source:
        for index, frame in enumerate(meta.frames):
            extracted = tmp_path / "frames" / f"{index:06d}.png"
            assert extracted.read_bytes() == source.read(frame.file)


@pytest.mark.parametrize("failure", ["invalid", "missing", "symlink", "duplicate", "large"])
def test_zip_rejects_invalid_resources(tmp_path: Path, failure: str, monkeypatch) -> None:
    """验证 ZIP 损坏，缺帧，链接，重复帧及解压超限均明确失败

    Args:
        tmp_path: 当前测试独占的临时目录
        failure: 本次注入的 ZIP 资源故障
        monkeypatch: 临时降低资源上限的 pytest 工具
    """
    data, meta = make_archive(1)
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        if failure != "missing":
            info = ZipInfo(meta.frames[0].file)
            info.external_attr = (0o120777 if failure == "symlink" else 0o100644) << 16
            archive.writestr(info, b"frame")
            if failure == "duplicate":
                with pytest.warns(UserWarning):
                    archive.writestr(info, b"frame")
    path = tmp_path / "original.zip"
    path.write_bytes(b"broken" if failure == "invalid" else output.getvalue())
    if failure == "large":
        monkeypatch.setattr(ugoira, "FRAME_BYTES_LIMIT", 1)
    with pytest.raises((ugoira.UgoiraError, ugoira.PixivResourceError)):
        ugoira.prepare_timeline(path, meta, tmp_path)


def inspect_mp4(path: Path) -> dict:
    """使用实际 FFmpeg 读取视频流，时间轴和 H.264 能力声明

    Args:
        path: 已生成并可供探测的 MP4 文件

    Returns:
        包含包时间基，包时间戳及流说明的探测结果
    """
    executable = get_ffmpeg_exe()
    result = subprocess.run(
        [executable, "-v", "error", "-i", str(path), "-c", "copy", "-f", "framehash", "-"],
        capture_output=True,
        check=True,
    ).stdout.decode()
    packets = [line.split(",") for line in result.splitlines() if not line.startswith("#")]
    time_base = Fraction(re.search(r"#tb 0: (\S+)", result)[1])
    information = subprocess.run(
        [
            executable, "-hide_banner", "-i", str(path), "-c", "copy",
            "-bsf:v", "trace_headers", "-f", "null", "-",
        ],
        capture_output=True,
        check=True,
    ).stderr.decode()
    return {
        "header": result,
        "packets": packets,
        "time_base": time_base,
        "duration": float((int(packets[-1][2]) + int(packets[-1][3])) * time_base),
        "information": information,
        "level": int(re.search(r"level_idc\s+\S+\s+= (\d+)", information)[1]),
    }


@pytest.mark.parametrize("kind", ["PNG", "JPEG"])
@pytest.mark.parametrize("size", [(64, 32), (65, 33), (1920, 1080), (1921, 1081)])
def test_real_ffmpeg_compatible_codec_dimensions_all_frames_and_timing(
    tmp_path: Path,
    kind: str,
    size: tuple[int, int],
) -> None:
    """实际编解码兼容视频，验证补齐尺寸，完整帧数及接近原作的时间轴

    Args:
        tmp_path: 当前测试独占的临时目录
        kind: 原始帧采用的 PNG 或 JPEG 格式
        size: 包含偶数和奇数高清尺寸的原始帧像素宽高
    """
    delays = [100, 80, 150]
    data, meta = make_archive(6, size, kind, delays)
    archive = tmp_path / "original.zip"
    archive.write_bytes(data)
    output = tmp_path / "output.mp4"
    started = time.perf_counter()
    asyncio.run(ugoira.convert_to_mp4(archive, meta, output))
    elapsed = time.perf_counter() - started
    executable = get_ffmpeg_exe()
    probe = inspect_mp4(output)
    width, height = ((value + 1) // 2 * 2 for value in size)
    assert f"#dimensions 0: {width}x{height}" in probe["header"]
    assert "#codec_id 0: h264" in probe["header"]
    assert "yuv420p" in probe["information"]
    assert "1000 fps" not in probe["information"]
    assert probe["level"] < 61
    assert len(probe["packets"]) == 6
    assert abs(probe["duration"] - sum(frame.delay for frame in meta.frames) / 1000) <= 0.041
    starts = [float(int(packet[2]) * probe["time_base"]) for packet in probe["packets"]]
    for index, timestamp in enumerate(starts):
        assert abs(timestamp - sum(frame.delay for frame in meta.frames[:index]) / 1000) <= 0.021
    assert len({packet[3].strip() for packet in probe["packets"]}) > 1
    decoded = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-i",
            str(output),
            "-pix_fmt",
            "rgb24",
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    assert len(decoded) == width * height * 3 * 6
    args = ugoira.ffmpeg_arguments(tmp_path / "timeline.ffconcat", output, meta)
    assert args[args.index("-crf") + 1] == "20"
    assert args[args.index("-c:v") + 1] == "libx264"
    assert args[args.index("-pix_fmt") + 1] == "yuv420p"
    assert args[args.index("-preset") + 1] == "veryfast"
    assert args[args.index("-frames:v") + 1] == "6"
    assert args[args.index("-fps_mode") + 1] == "vfr"
    assert args[args.index("-filter_threads") + 1] == "1"
    assert args[args.index("-movflags") + 1] == "+faststart"
    assert args[args.index("-vf") + 1] == "pad=ceil(iw/2)*2:ceil(ih/2)*2"
    assert all(args[i + 1] == "1" for i, arg in enumerate(args) if arg == "-threads")
    assert not any(word in " ".join(args) for word in ("scale=", "thumbnail", "crop=", "-r "))
    assert not any(
        arg in args
        for arg in ("-noautoscale", "-enc_time_base", "-video_track_timescale", "-level")
    )
    print(
        f"样本 {kind}，ZIP {len(data)} 字节，帧数 6，原始尺寸 {size}，"
        f"MP4 {output.stat().st_size} 字节，转换 {elapsed:.3f} 秒，"
        f"时长 {probe['duration']:.3f} 秒，"
        f"H.264 Level {probe['level'] / 10:.1f}"
    )


@pytest.mark.parametrize("failure", ["timeout", "exit", "empty", "unavailable", "cancel"])
def test_conversion_failure_and_process_cleanup(tmp_path: Path, monkeypatch, failure: str) -> None:
    """验证编码故障，超时和取消后的进程回收

    Args:
        tmp_path: 当前测试独占的临时目录
        monkeypatch: 替换编码进程和转换上限的 pytest 工具
        failure: 需要注入的转换失败阶段
    """
    data, meta = make_archive()
    archive = tmp_path / "original.zip"
    archive.write_bytes(data)
    process = Mock(returncode=None if failure in ("timeout", "cancel") else 1)
    entered = asyncio.Event()

    async def wait():
        """阻塞首次等待以模拟运行中的编码进程

        Returns:
            当前模拟编码进程的退出码
        """
        entered.set()
        if failure in ("timeout", "cancel") and not process.terminate.called:
            await asyncio.Event().wait()
        process.returncode = 0 if failure == "empty" else 1
        return process.returncode

    process.wait = wait
    factory = AsyncMock(return_value=process)
    monkeypatch.setattr(ugoira.asyncio, "create_subprocess_exec", factory)
    monkeypatch.setattr(ugoira, "CONVERT_TIMEOUT", 0.01)
    if failure == "unavailable":
        monkeypatch.setattr(ugoira, "get_ffmpeg_exe", Mock(side_effect=RuntimeError("缺失")))

    async def scenario():
        """执行故障转换并检查异常类型及子进程回收"""
        task = asyncio.create_task(ugoira.convert_to_mp4(archive, meta, tmp_path / "output.mp4"))
        if failure == "cancel":
            await entered.wait()
            task.cancel()
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else ugoira.UgoiraError):
            await task

    asyncio.run(scenario())
    if failure in ("timeout", "cancel"):
        process.terminate.assert_called_once()
    if failure == "unavailable":
        factory.assert_not_awaited()


def test_conversion_concurrency_is_one(tmp_path: Path, monkeypatch) -> None:
    """验证同时提交两个正式转换时仅允许一个编码子进程运行

    Args:
        tmp_path: 当前测试独占的临时目录
        monkeypatch: 替换编码子进程的 pytest 工具
    """
    active = 0
    peak = 0
    data, meta = make_archive()

    async def spawn(*args, **kwargs):
        """模拟编码过程并记录同时运行的数量

        Args:
            args: FFmpeg 可执行文件及媒体参数
            kwargs: 子进程的标准输入输出配置

        Returns:
            可等待并生成非空输出的模拟进程
        """
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        process = Mock(returncode=None)

        async def wait():
            """延迟生成视频后释放模拟编码占用

            Returns:
                表示编码成功的退出码
            """
            nonlocal active
            await asyncio.sleep(0.02)
            Path(args[-1]).write_bytes(b"mp4")
            active -= 1
            process.returncode = 0
            return 0

        process.wait = wait
        return process

    async def scenario():
        """同时提交两个具有独立临时目录的转换请求"""
        jobs = []
        for index in range(2):
            folder = tmp_path / str(index)
            folder.mkdir()
            archive = folder / "original.zip"
            archive.write_bytes(data)
            jobs.append(ugoira.convert_to_mp4(archive, meta, folder / "output.mp4"))
        await asyncio.gather(*jobs)

    monkeypatch.setattr(ugoira, "convert_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(ugoira.asyncio, "create_subprocess_exec", spawn)
    asyncio.run(scenario())
    assert peak == 1


def test_cancelled_worker_finishes_before_releasing_files() -> None:
    """验证取消线程任务后等待文件操作结束再退出媒体处理"""
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def worker():
        """持有模拟文件资源直到测试允许清理"""
        started.set()
        release.wait(5)
        finished.set()

    async def scenario():
        """在线程持有资源时取消调用并检查等待语义"""
        task = asyncio.create_task(ugoira._run_sync(worker))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["size", "corrupt"])
def test_formal_conversion_rejects_resize_and_corrupt_frames(
    tmp_path: Path,
    failure: str,
) -> None:
    """验证尺寸不一致或损坏的帧不会被隐式缩放或跳过

    Args:
        tmp_path: 当前测试独占的临时目录
        failure: 不一致尺寸或损坏的像素数据
    """
    data, meta = make_archive(2)
    original = BytesIO(data)
    archive_path = tmp_path / "original.zip"
    source = Image.new("RGB", (66, 33) if failure == "size" else (65, 33))
    replacement = BytesIO()
    source.save(replacement, format="PNG")
    with ZipFile(original) as archive, ZipFile(archive_path, "w") as target:
        target.writestr(meta.frames[0].file, archive.read(meta.frames[0].file))
        encoded = replacement.getvalue()
        if failure == "corrupt":
            encoded = encoded[:45]
        target.writestr(meta.frames[1].file, encoded)
    with pytest.raises(ugoira.UgoiraError):
        asyncio.run(ugoira.convert_to_mp4(archive_path, meta, tmp_path / "output.mp4"))


def test_gif_quantization_preserves_total_or_falls_back(tmp_path: Path) -> None:
    """验证短尾帧不会累计拉长 GIF，无可表示的短时序会明确失败

    Args:
        tmp_path: 当前测试独占的临时目录
    """
    data, meta = make_archive(3, delays=[100, 1, 1])
    path = tmp_path / "preview.zip"
    path.write_bytes(data)
    with Image.open(BytesIO(ugoira.build_preview_gif(path, meta, False, 256, 60))) as gif:
        assert sum(frame.info["duration"] for frame in ImageSequence.Iterator(gif)) == 100
    _, short_meta = make_archive(3, delays=[1])
    with pytest.raises(ugoira.UgoiraError, match="时序过短"):
        ugoira.build_preview_gif(path, short_meta, False, 256, 60)


def test_stuck_process_is_killed_and_reaped() -> None:
    """验证进程不响应正常终止时会强制结束并等待退出"""
    process = Mock(returncode=None)
    process.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), 0])
    asyncio.run(ugoira._stop_process(process))
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.wait.await_count == 2


def test_real_single_frame_keeps_its_full_delay(tmp_path: Path) -> None:
    """验证单帧保留接近原作的停留时长，且不增加重复视频帧

    Args:
        tmp_path: 当前测试独占的临时目录
    """
    data, meta = make_archive(1, delays=[137])
    archive = tmp_path / "original.zip"
    archive.write_bytes(data)
    output = tmp_path / "output.mp4"
    asyncio.run(ugoira.convert_to_mp4(archive, meta, output))
    probe = inspect_mp4(output)
    assert len(probe["packets"]) == 1
    assert abs(probe["duration"] - 0.137) <= 0.021


@pytest.mark.parametrize("mode", ["RGBA", "I;16"])
def test_compatible_conversion_accepts_alpha_and_high_depth(tmp_path: Path, mode: str) -> None:
    """兼容视频允许转换透明或高位深源帧，原始内容仍保存在 ZIP

    Args:
        tmp_path: 当前测试独占的临时目录
        mode: 需要验证的原始 PNG 像素模式
    """
    _, meta = make_archive(2, delays=[100, 80])
    path = tmp_path / "original.zip"
    with ZipFile(path, "w") as archive:
        for index, frame in enumerate(meta.frames):
            image = Image.new(
                mode, (65, 33), (index * 100, 0, 255, 128) if mode == "RGBA" else 1000
            )
            encoded = BytesIO()
            image.save(encoded, format="PNG")
            archive.writestr(frame.file, encoded.getvalue())
    original = path.read_bytes()
    output = tmp_path / "output.mp4"
    asyncio.run(ugoira.convert_to_mp4(path, meta, output))
    assert path.read_bytes() == original
    assert len(inspect_mp4(output)["packets"]) == 2


@pytest.mark.parametrize("delays", [[40, 80, 120], [1, 7, 41, 83, 127]])
def test_real_conversion_keeps_one_hundred_frames(tmp_path: Path, delays: list[int]) -> None:
    """实际编码保留全部 100 帧，短延时帧也不会因时间量化被丢弃

    Args:
        tmp_path: 当前测试独占的临时目录
        delays: 用于验证普通间隔或真实短间隔的毫秒延时序列
    """
    data, meta = make_archive(100, (64, 32), delays=delays)
    path = tmp_path / "original.zip"
    path.write_bytes(data)
    output = tmp_path / "output.mp4"
    asyncio.run(ugoira.convert_to_mp4(path, meta, output))
    probe = inspect_mp4(output)
    assert len(probe["packets"]) == 100
    assert abs(probe["duration"] - sum(frame.delay for frame in meta.frames) / 1000) <= 0.041

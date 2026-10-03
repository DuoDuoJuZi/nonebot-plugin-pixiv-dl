import asyncio
import subprocess
import threading
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
    assert timeline.endswith("file 'frames/000099.png'\noption framerate 1000\n")
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


@pytest.mark.parametrize("kind", ["PNG", "JPEG"])
def test_real_ffmpeg_preserves_rgb_dimensions_all_frames_and_exact_timing(
    tmp_path: Path,
    kind: str,
) -> None:
    """实际编解码并逐字节比较 RGB，同时验证奇数尺寸及每帧毫秒时长

    Args:
        tmp_path: 当前测试独占的临时目录
        kind: 原始帧采用的 PNG 或 JPEG 格式
    """
    delays = [1, 7, 41, 83, 127]
    data, meta = make_archive(5, (65, 33), kind, delays)
    archive = tmp_path / "original.zip"
    archive.write_bytes(data)
    output = tmp_path / "output.mp4"
    asyncio.run(ugoira.convert_to_mp4(archive, meta, output))
    executable = get_ffmpeg_exe()
    probe = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-i",
            str(output),
            "-map",
            "0:v",
            "-c",
            "copy",
            "-f",
            "framehash",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout.decode()
    packets = [line.split(",") for line in probe.splitlines() if not line.startswith("#")]
    assert "#dimensions 0: 65x33" in probe
    assert "#tb 0: 1/1000" in probe
    assert [int(packet[3]) for packet in packets] == delays
    assert [int(packet[2]) for packet in packets] == [sum(delays[:i]) for i in range(5)]
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
    original = b"".join(
        subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-i",
                str(path),
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "-",
            ],
            capture_output=True,
            check=True,
        ).stdout
        for path in sorted((tmp_path / "frames").iterdir())
    )
    assert len(decoded) == 65 * 33 * 3 * 5
    assert decoded == original
    args = ugoira.ffmpeg_arguments(tmp_path / "timeline.ffconcat", output, meta)
    assert args[args.index("-crf") + 1] == "0"
    assert args[args.index("-c:v") + 1] == "libx264rgb"
    assert args[args.index("-pix_fmt") + 1] == "rgb24"
    assert args[args.index("-preset") + 1] == "ultrafast"
    assert args[args.index("-frames:v") + 1] == "5"
    assert all(args[i + 1] == "1" for i, arg in enumerate(args) if arg == "-threads")
    assert not any(word in " ".join(args) for word in ("scale=", "thumbnail", "yuv420p", "-r "))


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


@pytest.mark.parametrize("failure", ["size", "alpha", "depth", "corrupt"])
def test_formal_conversion_rejects_silent_quality_changes(
    tmp_path: Path,
    failure: str,
) -> None:
    """验证不能无损表示的帧或解码损坏不会生成伪完整视频

    Args:
        tmp_path: 当前测试独占的临时目录
        failure: 不一致尺寸，透明通道，高位深或损坏像素数据
    """
    data, meta = make_archive(2)
    original = BytesIO(data)
    archive_path = tmp_path / "original.zip"
    mode = {"alpha": "RGBA", "depth": "I;16"}.get(failure, "RGB")
    source = Image.new(mode, (66, 33) if failure == "size" else (65, 33))
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
    """验证只有一帧时仍准确保留尾帧时长且不增加重复视频帧

    Args:
        tmp_path: 当前测试独占的临时目录
    """
    data, meta = make_archive(1, delays=[137])
    archive = tmp_path / "original.zip"
    archive.write_bytes(data)
    output = tmp_path / "output.mp4"
    asyncio.run(ugoira.convert_to_mp4(archive, meta, output))
    probe = subprocess.run(
        [get_ffmpeg_exe(), "-v", "error", "-i", str(output), "-c", "copy", "-f", "framehash", "-"],
        capture_output=True,
        check=True,
    ).stdout.decode()
    packets = [line.split(",") for line in probe.splitlines() if not line.startswith("#")]
    assert len(packets) == 1
    assert int(packets[0][3]) == 137

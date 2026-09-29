from pathlib import Path

import pytest

from nonebot_plugin_pixiv_dl import message
from nonebot_plugin_pixiv_dl.models import Artwork, Novel, NovelSeries


# @Author: DuoDuoJuZi
# @Date: 2026-09-29
def artwork(work_id: int = 1) -> Artwork:
    """构造用于消息断言的漫画样例

    Args:
        work_id: 赋给漫画样例的作品 ID

    Returns:
        包含基础元数据的漫画模型
    """
    return Artwork(work_id, "漫画", 2, "作者", ["标签"], "manga", 5, 0)


def novel(novel_id: int = 1) -> Novel:
    """构造用于文件断言的小说样例

    Args:
        novel_id: 赋给小说样例的作品 ID

    Returns:
        包含两行正文的小说模型
    """
    return Novel(novel_id, "第一章", 2, "作者", ["标签"], 0, content="首行\n次行")


def test_search_forward_has_one_metadata_node_per_result_and_splits() -> None:
    """验证搜索结果仅展示元数据并使用独立的分类标签"""
    images = [
        Artwork(index, str(index), 2, "作者", [], "image", 1, 0)
        for index in range(1, 6)
    ]
    packets = message.build_search_forward(images, "image", 2, 10)
    assert [len(packet) for packet in packets] == [2, 2, 1]
    assert all(node.type == "node" for packet in packets for node in packet)
    assert "PID：1" in packets[0][0].data["content"]
    assert "https://" not in packets[0][0].data["content"]
    assert all(node.data["nickname"] == "Pixiv 图片" for packet in packets for node in packet)

    novel_packets = message.build_search_forward([novel()], "novel", 2, 10)
    manga_packets = message.build_search_forward([artwork()], "manga", 2, 10)
    assert novel_packets[0][0].data["nickname"] == "Pixiv 小说"
    assert manga_packets[0][0].data["nickname"] == "Pixiv 漫画"


@pytest.mark.parametrize("work_type", ["image", "manga"])
def test_artwork_forward_metadata_first_one_image_per_node_and_splits(work_type: str) -> None:
    """验证图片转发分包的节点上限和跨包页序

    Args:
        work_type: 本次测试使用的插画或漫画分类
    """
    work = artwork()
    work.type = work_type
    packets = message.build_artwork_forward(work, [b"a", b"b", b"c", b"d", b"e"], 3, 10)
    assert [len(packet) for packet in packets] == [3, 3, 2]
    assert "作品总页数：5" in packets[0][0].data["content"]
    assert "本次发送：5" in packets[0][0].data["content"]
    assert "续页" in packets[1][0].data["content"]
    assert all(
        node.data["nickname"] == f"Pixiv {message.KIND_NAMES[work_type]}"
        for packet in packets
        for node in packet
    )
    pages = [node.data["content"] for packet in packets for node in packet[1:]]
    assert [content[0].data["text"] for content in pages] == [
        f"第 {index} 页\n" for index in range(1, 6)
    ]
    assert all([segment.type for segment in content] == ["text", "image"] for content in pages)


def test_novel_files_are_unique_safe_utf8_and_ordered(tmp_path: Path) -> None:
    """验证章节文件独立命名且保留 Unicode 文本和换行

    Args:
        tmp_path: 由 pytest 提供的独立临时目录
    """
    first = novel(1)
    first.title = r"..\第一章:?"
    second = novel(2)
    paths = [
        message.write_novel_file(first, tmp_path, 1),
        message.write_novel_file(second, tmp_path, 2),
    ]
    assert paths[0] != paths[1]
    assert paths[0].parent == tmp_path
    assert paths[0].name.startswith("01 - ")
    assert paths[1].name.startswith("02 - ")
    assert ".." not in paths[0].name
    assert all(char not in paths[0].name for char in r'\/:*?"<>|')
    assert "首行\n次行" in paths[0].read_text(encoding="utf-8")
    assert len(message.safe_filename("😀" * 200).encode("utf-8")) <= 192
    series = NovelSeries(9, "系列", 2, "作者", [], 0, 100)
    assert "系列总章节：100" in message.format_series(series, 50)
    assert "本次下载：50" in message.format_series(series, 50)


def test_novel_forward_keeps_metadata_first_and_splits(tmp_path: Path) -> None:
    """验证小说转发的文件顺序和分包首节点说明

    Args:
        tmp_path: 由 pytest 提供的独立临时目录
    """
    paths = [tmp_path / f"{index:02d}.txt" for index in range(1, 4)]
    packets = message.build_novel_forward("元数据", paths, 3, 10)
    assert [len(packet) for packet in packets] == [3, 2]
    assert packets[0][0].data["content"] == "元数据"
    assert "续传" in packets[1][0].data["content"]
    files = [node.data["content"][0] for packet in packets for node in packet[1:]]
    assert [segment.type for segment in files] == ["file"] * 3
    assert [segment.data["name"] for segment in files] == [path.name for path in paths]
    assert [Path(segment.data["file"]) for segment in files] == paths

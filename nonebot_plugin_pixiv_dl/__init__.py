from nonebot.plugin import PluginMetadata

from .config import Config

# @Author: DuoDuoJuZi
# @Date: 2026-09-29
__plugin_meta__ = PluginMetadata(
    name="nonebot-plugin-pixiv-dl",
    description="通过 Pixiv Web AJAX 搜索和下载作品",
    usage="/px搜索 关键词 或 /px下载 作品 ID",
    type="application",
    homepage="https://github.com/DuoDuoJuZi/nonebot-plugin-pixiv-dl",
    config=Config,
    supported_adapters={"~onebot.v11"},
)

from . import commands as commands

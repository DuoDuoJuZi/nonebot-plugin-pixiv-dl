import asyncio
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from nonebot import logger

from .models import ContentPolicy


# @Author: DuoDuoJuZi
# @Date: 2026-10-08
class PreferenceError(Exception):
    """表示偏好文件不可用，需要停止依赖该配置的操作"""


class PreferenceStore:
    """按机器人与目标账号读写群和个人偏好，串行执行原子文件更新"""

    def __init__(self, directory: Path) -> None:
        """绑定 LocalStore 持久数据目录并建立进程内异步锁

        Args:
            directory: 保存群和个人设置文件的插件数据目录
        """
        self.directory = directory
        self.lock = asyncio.Lock()

    def _load(self, name: str) -> dict:
        """读取并严格校验偏好文件，仅文件不存在时使用空记录

        Args:
            name: groups 或 users，决定允许保存的偏好字段

        Returns:
            按机器人账号和目标账号分层保存的偏好记录

        Raises:
            PreferenceError: 文件无法读取或内容结构损坏
        """
        path = self.directory / f"{name}.json"
        fields = {"r18"} if name == "groups" else {"r18", "ai"}
        try:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            if not isinstance(data, dict):
                raise ValueError("设置根节点必须是对象")
            for targets in data.values():
                if not isinstance(targets, dict):
                    raise ValueError("机器人设置必须是对象")
                for settings in targets.values():
                    if (
                        not isinstance(settings, dict)
                        or not settings
                        or not settings.keys() <= fields
                        or any(type(value) is not bool for value in settings.values())
                    ):
                        raise ValueError("偏好字段必须是有效的布尔设置")
            return data
        except (OSError, ValueError) as exc:
            logger.error("Pixiv 设置文件读取失败，文件={}，异常={}", path, exc)
            raise PreferenceError("设置文件不可用，请联系管理员检查日志") from exc

    async def get(self, name: str, bot_id: str, target_id: str) -> dict[str, bool]:
        """读取指定机器人下的群或个人偏好，缺少记录时保持旧版默认行为

        Args:
            name: groups 或 users，选择群设置或个人设置
            bot_id: 当前机器人的 QQ 号
            target_id: 群号或发起操作的用户 QQ 号

        Returns:
            包含默认值的偏好副本，R18 与 AI 默认开启

        Raises:
            PreferenceError: 对应文件无法读取或内容损坏
        """
        async with self.lock:
            defaults = {"r18": True} if name == "groups" else {"r18": True, "ai": True}
            return defaults | self._load(name).get(bot_id, {}).get(target_id, {})

    async def set(
        self, name: str, bot_id: str, target_id: str, field: str, value: bool
    ) -> None:
        """持锁读取最新设置并通过同目录临时文件原子替换旧文件

        Args:
            name: groups 或 users，选择群设置或个人设置
            bot_id: 当前机器人的 QQ 号
            target_id: 已通过命令权限校验的目标群号或发送者 QQ 号
            field: 本次修改的 r18 或 ai 偏好字段
            value: 是否开启该项偏好

        Raises:
            PreferenceError: 旧文件损坏或本次设置无法完整写入
            ValueError: 设置分类，字段或开关值不合法
        """
        fields = {"groups": {"r18"}, "users": {"r18", "ai"}}
        if field not in fields.get(name, set()) or type(value) is not bool:
            raise ValueError("偏好字段或开关值无效")
        async with self.lock:
            data = self._load(name)
            data.setdefault(bot_id, {}).setdefault(target_id, {})[field] = value
            temporary = None
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                with NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.directory,
                    prefix=f".{name}-", suffix=".tmp", delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    json.dump(data, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(self.directory / f"{name}.json")
            except OSError as exc:
                logger.error("Pixiv 设置文件写入失败，分类={}，异常={}", name, exc)
                raise PreferenceError("设置保存失败，请联系管理员检查日志") from exc
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    async def policy(
        self, bot_id: str, user_id: str, group_id: str | None, global_r18: bool
    ) -> ContentPolicy:
        """计算当前用户在指定聊天环境中的有效内容权限

        Args:
            bot_id: 当前机器人的 QQ 号
            user_id: 由消息事件确定的发送者 QQ 号
            group_id: 当前群号，私聊使用 None
            global_r18: 插件全局 R18 许可，关闭时任何偏好均不能覆盖

        Returns:
            同时受全局，群与个人约束的 R18 权限及独立的个人 AI 偏好

        Raises:
            PreferenceError: 相关设置文件损坏或无法读取
        """
        user = await self.get("users", bot_id, user_id)
        group = (
            await self.get("groups", bot_id, group_id) if group_id is not None else {"r18": True}
        )
        return ContentPolicy(global_r18 and group["r18"] and user["r18"], user["ai"])

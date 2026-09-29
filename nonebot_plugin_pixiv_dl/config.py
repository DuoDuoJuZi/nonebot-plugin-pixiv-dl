from ipaddress import ip_address

from pydantic import BaseModel, Field, field_validator


# @Author: DuoDuoJuZi
# @Date: 2026-09-29
class Config(BaseModel):
    """保存 Pixiv 请求和 OneBot 消息发送所需的配置"""

    pixiv_cookie: str = ""
    pixiv_r18: bool = True
    pixiv_search_limit: int = Field(default=20, ge=1, le=200)
    pixiv_forward_max_messages: int = Field(default=20, ge=2, le=100)
    pixiv_download_max_pages: int = Field(default=30, ge=1, le=200)
    pixiv_novel_max_chapters: int = Field(default=50, ge=1, le=500)
    pixiv_timeout: float = Field(default=20.0, gt=0)
    pixiv_proxy: str | None = None
    pixiv_fixed_ip: str | None = None

    @field_validator("pixiv_fixed_ip")
    @classmethod
    def validate_fixed_ip(cls, value: str | None) -> str | None:
        """校验 pixiv_fixed_ip 字段，仅接受有效的 IPv4 或 IPv6 地址

        Args:
            value: 配置的固定 IP 地址，空值或空字符串表示关闭固定地址路由

        Returns:
            规范化后的 IP 地址，关闭固定地址路由时返回 None

        Raises:
            ValueError: 非空配置无法解析为有效的 IP 地址
        """
        return str(ip_address(value)) if value else None

"""版本信息接口。

界面顶部的版本徽标读它，用来确认「这次升级到底生效没有」。
"""
from fastapi import APIRouter

from src.infrastructure.config.version import version_info


router = APIRouter(tags=["version"])


@router.get("/api/version")
async def get_version():
    """返回当前运行的版本号与提交哈希。

    不做鉴权：版本号不是敏感信息，而且这个接口要在登录前也能探活。
    """
    return version_info()

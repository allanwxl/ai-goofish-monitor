"""MCP 服务组装。

创建带 API Key 鉴权的 FastMCP 实例并注册只读查询工具。
鉴权使用 FastMCP 官方内置的 `StaticTokenVerifier`：
未携带或携带无效 API Key 的请求会被拒绝（401 + WWW-Authenticate）。
"""
from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.infrastructure.config.settings import mcp_settings
from src.mcp.auth import build_token_map
from src.mcp.tools import TOOL_NAMES, register_tools


SERVER_NAME = "xianyu-monitor"

SERVER_INSTRUCTIONS = """\
闲鱼二手商品监控数据服务（只读）。

可用能力：
- list_all_results：列出当前已覆盖监控的全部关键词结果集及数据规模
- search_items：按关键词分页查询商品明细，可按 AI 推荐 / 关键词命中筛选与排序
- get_price_history：查询关键词或单品的价格快照历史
- get_market_insights：查询关键词级市场行情洞察（均价、中位数、价格区间、日趋势）

注意：
- 返回的商品信息已做脱敏处理，不包含卖家昵称、头像等个人身份信息
- 数据来自定时抓取，存在滞后；请以 latest_crawl_time / latest_snapshot_at 判断数据新鲜度
- 首次调用建议先执行 list_all_results 确认可用关键词
"""


def create_server() -> FastMCP:
    """创建配置好的 MCP 服务实例。

    读取 `.env` 中的 `MCP_API_KEYS` 构建鉴权；未配置 Key 时抛
    `MCPConfigurationError`，拒绝启动。
    """
    token_map = build_token_map()
    verifier = StaticTokenVerifier(tokens=token_map)

    mcp = FastMCP(
        name=SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        auth=verifier,
    )

    register_tools(mcp, mcp_settings.parsed_allowed_tools())

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        """健康检查（不暴露数据，仅供探活使用）。"""
        return JSONResponse({"status": "healthy", "service": SERVER_NAME})

    return mcp


def describe_server(server: FastMCP) -> str:
    """生成启动日志文本（不包含任何 Key 明文）。"""
    allowed = mcp_settings.parsed_allowed_tools()
    tool_names = [
        name for name in TOOL_NAMES if not allowed or name in allowed
    ]
    host = mcp_settings.host
    port = mcp_settings.port
    lines = [
        "MCP 服务启动中...",
        f"  监听地址: http://{host}:{port}/mcp",
        f"  健康检查: http://{host}:{port}/health",
        f"  已注册工具: {', '.join(tool_names) if tool_names else '（无）'}",
        f"  API Key 数量: {len(mcp_settings.parsed_api_keys())}",
    ]
    if host in {"0.0.0.0", "::"}:
        lines.append(
            "  ⚠️  监听地址为 0.0.0.0，API Key 将以明文经过网络，"
            "请务必在反向代理层启用 HTTPS。"
        )
    return "\n".join(lines)

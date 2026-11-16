"""MCP 服务独立启动入口。

用法：
    python -m src.mcp_server

环境变量（见 .env.example 的 "MCP 服务" 段）：
    MCP_ENABLED      是否启用，默认 false
    MCP_HOST         监听地址，默认 127.0.0.1
    MCP_PORT         监听端口，默认 8001
    MCP_API_KEYS     API Key 列表，逗号分隔；未配置时拒绝启动
    MCP_ALLOWED_TOOLS 可选工具白名单，逗号分隔；为空表示全部开放
"""
from __future__ import annotations

import sys

from src.infrastructure.config.settings import mcp_settings
from src.mcp.auth import MCPConfigurationError
from src.mcp.server import create_server, describe_server


def main() -> int:
    """启动 MCP 服务，返回进程退出码。"""
    if not mcp_settings.enabled:
        print(
            "MCP 服务未启用（MCP_ENABLED=false）。"
            "如需启用，请在 .env 中设置 MCP_ENABLED=true。"
        )
        return 1

    try:
        server = create_server()
    except MCPConfigurationError as exc:
        print(f"配置错误：{exc}")
        return 1

    print(describe_server(server))
    print()

    try:
        server.run(
            transport="http",
            host=mcp_settings.host,
            port=mcp_settings.port,
            path="/mcp",
        )
    except KeyboardInterrupt:
        print("\nMCP 服务已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""MCP API Key 鉴权。

API Key 只从环境变量 `MCP_API_KEYS` 读取，不硬编码、不写入数据库。
支持两种配置形式：
    MCP_API_KEYS=key1,key2
    MCP_API_KEYS=key1:tenant-a,key2:tenant-b
冒号后的部分作为租户标识保留，当前最小实现尚未使用租户隔离，
但提前保留该位，避免后续引入多租户时破坏 Key 格式。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from src.infrastructure.config.settings import mcp_settings


class MCPConfigurationError(RuntimeError):
    """MCP 配置错误（缺少 API Key 等）。"""


@dataclass(frozen=True)
class ApiKeyEntry:
    """一条 API Key 配置。"""

    key: str
    tenant: str


def parse_api_keys(raw: str | None) -> list[ApiKeyEntry]:
    """把 `MCP_API_KEYS` 原始字符串解析为 Key 条目列表。

    忽略空片段；`key:tenant` 形式的冒号后内容作为租户标识。
    """
    entries: list[ApiKeyEntry] = []
    for chunk in (raw or "").split(","):
        item = chunk.strip()
        if not item:
            continue
        key, separator, tenant = item.partition(":")
        key = key.strip()
        if not key:
            continue
        entries.append(
            ApiKeyEntry(
                key=key,
                tenant=tenant.strip() if separator else "default",
            )
        )
    return entries


def load_api_keys() -> list[ApiKeyEntry]:
    """读取已配置的 API Key 列表。

    未配置或为空时抛出 MCPConfigurationError —— 宁可拒绝启动，
    也不允许"未配置即放行"。
    """
    entries = parse_api_keys(mcp_settings.api_keys)
    if not entries:
        raise MCPConfigurationError(
            "MCP_API_KEYS 未配置或为空，已拒绝启动 MCP 服务。"
            "请在 .env 中设置，例如 MCP_API_KEYS=sk-xxx,sk-yyy"
        )
    return entries


def verify_api_key(authorization: str | None) -> ApiKeyEntry | None:
    """校验 Authorization 头，返回命中的 Key 条目；无效时返回 None。

    接受两种写法：
      - 纯 Key：`sk-xxx`
      - 完整配置串：`sk-xxx:tenant-a`（与 .env 中的写法一致）

    使用 `secrets.compare_digest` 做常量时间比较，避免时序侧信道。
    比较前统一编码为 bytes，以支持非 ASCII 的 Key。
    """
    if not authorization:
        return None

    token = authorization.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        return None

    token_bytes = token.encode("utf-8")
    entries = load_api_keys()

    # 先按纯 Key 匹配
    for entry in entries:
        if secrets.compare_digest(entry.key.encode("utf-8"), token_bytes):
            return entry

    # 再按 `key:tenant` 完整配置串匹配
    for entry in entries:
        if not entry.tenant:
            continue
        full = f"{entry.key}:{entry.tenant}".encode("utf-8")
        if secrets.compare_digest(full, token_bytes):
            return entry

    return None


def build_token_map() -> dict[str, dict[str, object]]:
    """构造 FastMCP `StaticTokenVerifier` 所需的 token 字典。

    key -> {client_id, scopes, tenant}。租户标识写入 claims，供后续按租户过滤使用。

    纯 Key 与 `key:tenant` 完整串都登记为有效 token，
    这样客户端无论传哪种形式都能通过鉴权。
    """
    token_map: dict[str, dict[str, object]] = {}
    for entry in load_api_keys():
        claims: dict[str, object] = {
            "client_id": "xianyu-monitor-mcp",
            "scopes": ["read"],
            "tenant": entry.tenant,
        }
        token_map[entry.key] = claims
        if entry.tenant:
            token_map[f"{entry.key}:{entry.tenant}"] = claims
    return token_map

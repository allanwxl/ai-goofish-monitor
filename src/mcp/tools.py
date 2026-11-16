"""MCP 工具实现（v1，只读）。

所有对外返回的商品记录都经过 `redact_record()` 白名单裁剪：
只保留对买家有决策价值的字段，剥离卖家昵称、头像、个性签名、
在售商品列表等个人信息，也不整体外发 `raw_json`。
"""
from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

from fastmcp.exceptions import ToolError

from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.storage_names import (
    build_result_filename,
    normalize_keyword_from_filename,
)
from src.services.price_history_service import (
    build_price_history_insights,
    load_price_snapshots,
)
from src.services.result_storage_service import (
    list_result_filenames,
    query_result_records,
)


# —— 字段白名单 ——

#: 商品信息中允许外发的字段
ALLOWED_ITEM_FIELDS: tuple[str, ...] = (
    "商品标题",
    "当前售价",
    "商品原价",
    "“想要”人数",
    "商品标签",
    "发货地区",
    "发布时间",
    "商品ID",
    "商品图片列表",
    "商品主图链接",
    "浏览量",
    "商品链接",
)

#: 卖家信息中允许外发的字段（信用与经营指标，不含身份标识）
ALLOWED_SELLER_FIELDS: tuple[str, ...] = (
    "卖家在售/已售商品数",
    "卖家收到的评价总数",
    "卖家信用等级",
    "买家信用等级",
)

#: AI 分析中允许外发的字段（error 属内部实现细节，不外发）
ALLOWED_ANALYSIS_FIELDS: tuple[str, ...] = (
    "analysis_source",
    "is_recommended",
    "reason",
    "keyword_hit_count",
    "prompt_version",
    "risk_tags",
    "value_score",
    "value_summary",
    "criteria_analysis",
)

#: 记录顶层允许外发的字段
ALLOWED_TOP_LEVEL_FIELDS: tuple[str, ...] = (
    "爬取时间",
    "搜索关键字",
    "任务名称",
    "商品信息",
    "卖家信息",
    "价格参考",
    "price_insight",
    "ai_analysis",
)

SORT_COLUMNS = ("crawl_time", "publish_time", "price", "keyword_hit_count")
SORT_ORDERS = ("asc", "desc")

#: 本服务提供的全部工具名（注册顺序即此顺序）
TOOL_NAMES: tuple[str, ...] = (
    "list_all_results",
    "search_items",
    "get_price_history",
    "get_market_insights",
)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


def _pick(source: Any, allowed: tuple[str, ...]) -> dict:
    """按白名单挑选字典字段；source 非字典时返回空字典。"""
    if not isinstance(source, dict):
        return {}
    return {key: source.get(key) for key in allowed if key in source}


def redact_record(record: dict) -> dict:
    """对单条结果记录做白名单裁剪。

    这是所有 tool 的统一出口 —— 任何返回商品明细的路径都必须经过它，
    避免新增字段时遗漏脱敏。
    """
    if not isinstance(record, dict):
        return {}

    redacted = _pick(record, ALLOWED_TOP_LEVEL_FIELDS)

    item = _pick(record.get("商品信息"), ALLOWED_ITEM_FIELDS)
    if item:
        redacted["商品信息"] = item

    seller = _pick(record.get("卖家信息"), ALLOWED_SELLER_FIELDS)
    if seller:
        redacted["卖家信息"] = seller

    analysis = _pick(record.get("ai_analysis"), ALLOWED_ANALYSIS_FIELDS)
    if analysis:
        redacted["ai_analysis"] = analysis

    return redacted


def _validate_page(page: int, limit: int) -> None:
    """校验分页参数，行为与 REST 层 `/api/results/{filename}` 保持一致。"""
    if page < 1:
        raise ToolError("page 必须大于等于 1")
    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise ToolError(f"limit 必须在 1 到 {MAX_PAGE_SIZE} 之间")


def _validate_sort(sort_by: str, sort_order: str) -> None:
    """校验排序参数。"""
    if sort_by not in SORT_COLUMNS:
        raise ToolError(f"sort_by 必须是以下之一：{', '.join(SORT_COLUMNS)}")
    if sort_order not in SORT_ORDERS:
        raise ToolError(f"sort_order 必须是以下之一：{', '.join(SORT_ORDERS)}")


def _fetch_result_set_stats() -> list[dict]:
    """一次聚合查询取回各结果集的关键词、商品数与最新爬取时间。

    避免对每个结果集单独查询造成 N+1。
    """
    with sqlite_connection() as conn:
        rows = conn.execute(
            """
            SELECT result_filename,
                   keyword,
                   MAX(crawl_time) AS latest_crawl_time,
                   COUNT(*) AS item_count
            FROM result_items
            GROUP BY result_filename, keyword
            ORDER BY latest_crawl_time DESC, result_filename DESC
            """
        ).fetchall()
    return [
        {
            "result_filename": str(row["result_filename"]),
            "keyword": str(row["keyword"] or ""),
            "item_count": int(row["item_count"]),
            "latest_crawl_time": str(row["latest_crawl_time"] or ""),
        }
        for row in rows
    ]


# —— MCP tools ——


async def list_all_results() -> dict:
    """列出平台已爬取的全部结果集。

    返回每个结果集的关键词、已收录商品数与最近一次爬取时间，
    用于让 Agent 了解当前可查询的数据范围。
    """
    try:
        stats = await asyncio.to_thread(_fetch_result_set_stats)
    except sqlite3.OperationalError as exc:
        raise ToolError(f"结果库尚未初始化或不可用：{exc}") from exc

    return {
        "total_result_sets": len(stats),
        "total_items": sum(item["item_count"] for item in stats),
        "result_sets": stats,
    }


async def search_items(
    keyword: str,
    page: int = 1,
    limit: int = DEFAULT_PAGE_SIZE,
    ai_recommended_only: bool = False,
    keyword_recommended_only: bool = False,
    include_hidden: bool = False,
    sort_by: str = "crawl_time",
    sort_order: str = "desc",
) -> dict:
    """分页查询指定关键词的监控结果。

    Args:
        keyword: 监控关键词，例如 "小米17promax 16+512"。
        page: 页码，从 1 开始。
        limit: 每页条数，1–100，默认 20。
        ai_recommended_only: 只返回 AI 分析判定为推荐的商品。
        keyword_recommended_only: 只返回关键词命中的商品。
        include_hidden: 是否包含被隐藏或已过期的条目。
        sort_by: 排序字段：crawl_time / publish_time / price / keyword_hit_count。
        sort_order: 排序方向：asc / desc。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        raise ToolError("keyword 不能为空")

    if ai_recommended_only and keyword_recommended_only:
        raise ToolError("AI推荐筛选与关键词推荐筛选不能同时开启。")

    _validate_page(page, limit)
    _validate_sort(sort_by, sort_order)

    filename = build_result_filename(keyword)
    try:
        total_items, items = await query_result_records(
            filename,
            ai_recommended_only=ai_recommended_only,
            keyword_recommended_only=keyword_recommended_only,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            limit=limit,
            include_hidden=include_hidden,
        )
    except sqlite3.OperationalError as exc:
        raise ToolError(f"结果库尚未初始化或不可用：{exc}") from exc

    return {
        "keyword": keyword,
        "result_filename": filename,
        "total_items": total_items,
        "page": page,
        "limit": limit,
        "items": [redact_record(item) for item in items],
    }


async def get_price_history(keyword: str, item_id: str = "", limit: int = 50) -> dict:
    """查询关键词下的价格快照历史。

    Args:
        keyword: 监控关键词。
        item_id: 指定商品 ID；留空表示返回该关键词下的全部快照。
        limit: 最多返回条数，默认 50，最大 500。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        raise ToolError("keyword 不能为空")
    if limit < 1 or limit > 500:
        raise ToolError("limit 必须在 1 到 500 之间")

    try:
        snapshots = await asyncio.to_thread(load_price_snapshots, keyword)
    except sqlite3.OperationalError as exc:
        raise ToolError(f"结果库尚未初始化或不可用：{exc}") from exc

    if item_id:
        snapshots = [
            snapshot
            for snapshot in snapshots
            if str(snapshot.get("item_id") or "") == str(item_id)
        ]

    total = len(snapshots)
    window = snapshots[-limit:] if total > limit else snapshots

    return {
        "keyword": keyword,
        "item_id": item_id,
        "total_snapshots": total,
        "returned": len(window),
        "snapshots": window,
    }


async def get_market_insights(keyword: str) -> dict:
    """查询关键词级市场行情洞察。

    包含当前市场均价/中位数/价格区间、历史价格概览与按天价格趋势。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        raise ToolError("keyword 不能为空")

    try:
        insights = await asyncio.to_thread(build_price_history_insights, keyword)
    except sqlite3.OperationalError as exc:
        raise ToolError(f"结果库尚未初始化或不可用：{exc}") from exc

    market_summary = insights.get("market_summary") or {}
    return {
        "keyword": keyword,
        "latest_snapshot_at": insights.get("latest_snapshot_at"),
        "market_summary": market_summary,
        "history_summary": insights.get("history_summary"),
        "daily_trend": insights.get("daily_trend") or [],
        "has_data": bool(market_summary.get("sample_count")),
    }


def register_tools(mcp, allowed_tools: list[str] | None = None) -> None:
    """把上述只读工具注册到 FastMCP 实例。

    `allowed_tools` 非空时只注册白名单内的工具（白名单为空表示全部开放）。
    """
    tool_map = {
        "list_all_results": list_all_results,
        "search_items": search_items,
        "get_price_history": get_price_history,
        "get_market_insights": get_market_insights,
    }
    allowed = set(allowed_tools) if allowed_tools else None
    for name in TOOL_NAMES:
        if allowed is None or name in allowed:
            mcp.tool(tool_map[name])


__all__ = [
    "ALLOWED_ANALYSIS_FIELDS",
    "ALLOWED_ITEM_FIELDS",
    "ALLOWED_SELLER_FIELDS",
    "get_market_insights",
    "get_price_history",
    "list_all_results",
    "normalize_keyword_from_filename",
    "redact_record",
    "register_tools",
    "search_items",
]

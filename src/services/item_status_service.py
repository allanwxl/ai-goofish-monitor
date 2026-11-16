"""
商品在售状态（已售 / 已下架）判定与成交价记录服务。

数据来源是闲鱼商品详情接口 ``mtop.taobao.idle.pc.detail``，返回体里的
``data.itemDO`` 自带三个关键字段：

* ``itemStatus``  商品状态码，正数表示在售，负数表示各种下架状态（如 -98 已下架）
* ``soldPrice``   已售价格（即成交价）
* ``soldCnt``     卖出数量

这些字段的语义来自公开的 goofish 客户端文档，不是闲鱼官方文档，所以判定只依赖
状态码的符号与 soldCnt/soldPrice，**不依赖具体码值**；原始码会写入
``status_raw_code``，便于用真实数据回归校准映射表（见 ``ITEM_STATUS_CODE_MAP``）。

注意：``result_items.status``（active/hidden/expired）表达的是记录的**可见性**，
与这里的**在售状态**语义不同，两套状态互不影响。
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any, Optional

from src.services.price_history_service import parse_price_value
from src.services.result_storage_service import (
    increment_delisted_streak,
    load_item_status_events,
    load_listing_status_counts,
    mark_item_status_checked,
    persist_listing_status,
)


LISTING_STATUS_ACTIVE = "active"
LISTING_STATUS_SOLD = "sold"
LISTING_STATUS_DELISTED = "delisted"
LISTING_STATUS_UNKNOWN = "unknown"

VALID_LISTING_STATUSES = {
    LISTING_STATUS_ACTIVE,
    LISTING_STATUS_SOLD,
    LISTING_STATUS_DELISTED,
    LISTING_STATUS_UNKNOWN,
}

# 判定来源
SOURCE_DETAIL_API = "detail_api"
SOURCE_MISSING_DETAIL = "missing_detail"
SOURCE_SELLER_LIST = "seller_list"
SOURCE_ERROR = "error"

# 成交价来源
PRICE_SOURCE_API = "sold_price_api"
PRICE_SOURCE_FALLBACK = "listing_fallback"


@dataclass(frozen=True)
class ListingStatusInfo:
    """一次详情探测得出的商品状态。"""

    status: str
    raw_code: Optional[int]
    sold_cnt: int
    sold_price: Optional[float]
    sold_price_display: str
    source: str
    note: str = ""


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def load_status_code_map() -> dict[int, str]:
    """读取 ``ITEM_STATUS_CODE_MAP`` 覆盖映射。

    形如 ``{"-98": "delisted", "3": "active"}``，只接受已知的状态词。
    """
    raw = os.getenv("ITEM_STATUS_CODE_MAP")
    if not raw or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}

    code_map: dict[int, str] = {}
    for code, status in payload.items():
        try:
            code_int = int(code)
        except (TypeError, ValueError):
            continue
        status_text = str(status or "").strip().lower()
        if status_text in VALID_LISTING_STATUSES:
            code_map[code_int] = status_text
    return code_map


def _map_status_code(raw_code: Optional[int]) -> Optional[str]:
    """把平台状态码映射成业务状态；无码时返回 None。"""
    if raw_code is None:
        return None
    code_map = load_status_code_map()
    if raw_code in code_map:
        return code_map[raw_code]
    if raw_code > 0:
        return LISTING_STATUS_ACTIVE
    return LISTING_STATUS_DELISTED


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _parse_positive_price(value: Any) -> Optional[float]:
    """解析价格，0 与负数视为「没有价格」。"""
    parsed = parse_price_value(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def parse_listing_status(detail_json: dict) -> ListingStatusInfo:
    """从详情接口返回体解析商品在售状态。

    判定顺序：

    1. ``data.itemDO`` 缺失 → 详情接口明确报错或商品已被删除，判定为已下架
    2. ``soldCnt > 0`` 或 ``soldPrice`` 有值 → 已售（优先于状态码）
    3. ``itemStatus`` 负数 → 已下架；正数 → 在售
    4. 关键字段缺失 → 未知
    """
    detail_json = detail_json or {}
    ret = detail_json.get("ret") or []
    ret_text = str(ret[0]) if isinstance(ret, (list, tuple)) and ret else ""
    data = detail_json.get("data") or {}
    item_do = data.get("itemDO") if isinstance(data, dict) else None
    if not isinstance(item_do, dict) or not item_do:
        return ListingStatusInfo(
            status=LISTING_STATUS_DELISTED,
            raw_code=None,
            sold_cnt=0,
            sold_price=None,
            sold_price_display="",
            source=SOURCE_MISSING_DETAIL,
            note=ret_text or "itemDO missing",
        )

    raw_code = _coerce_int(item_do.get("itemStatus"))
    sold_cnt = _coerce_int(item_do.get("soldCnt")) or 0
    sold_price_display = str(item_do.get("soldPrice") or "").strip()
    sold_price = _parse_positive_price(sold_price_display)

    sold_precedence = _env_flag("ITEM_STATUS_SOLD_PRECEDENCE", True)
    status = LISTING_STATUS_UNKNOWN
    if sold_precedence and (sold_cnt > 0 or sold_price is not None):
        status = LISTING_STATUS_SOLD
    else:
        mapped = _map_status_code(raw_code)
        if mapped:
            status = mapped

    return ListingStatusInfo(
        status=status,
        raw_code=raw_code,
        sold_cnt=sold_cnt,
        sold_price=sold_price,
        sold_price_display=sold_price_display,
        source=SOURCE_DETAIL_API,
        note=f"itemStatus={raw_code}, soldCnt={sold_cnt}",
    )


def resolve_sold_price(
    info: ListingStatusInfo,
    last_listing_price: Optional[float],
) -> tuple[Optional[float], str, str]:
    """确定成交价口径。

    返回 ``(价格, 展示文本, 来源)``：

    * 平台给的已售价格 → ``sold_price_api``（真实成交价）
    * 取不到时回退最后一次挂牌价 → ``listing_fallback``（估算，界面必须标注）
    * 都没有 → ``(None, '', '')``
    """
    if info.sold_price is not None:
        display = info.sold_price_display or ""
        if display and "¥" not in display:
            display = f"¥{display}"
        if not display:
            display = f"¥{info.sold_price}"
        return info.sold_price, display, PRICE_SOURCE_API
    if last_listing_price is not None and last_listing_price > 0:
        return (
            round(float(last_listing_price), 2),
            f"¥{round(float(last_listing_price), 2)}",
            PRICE_SOURCE_FALLBACK,
        )
    return None, "", ""


def apply_listing_status(
    result_filename: str,
    item_id: str,
    info: ListingStatusInfo,
    *,
    run_id: str = "",
    last_listing_price: Optional[float] = None,
    detail_json: str = "",
    detected_at: str = "",
) -> dict:
    """把一次探测结果写回结果记录。

    已下架支持「连续确认」机制：``ITEM_STATUS_DELISTED_CONFIRM`` 大于 1 时，
    需要连续多次都判为下架才会落终态，中途只更新复核时间。
    """
    detected_at = detected_at or datetime.now().isoformat()

    if info.status == LISTING_STATUS_DELISTED:
        confirm_times = max(1, _env_int("ITEM_STATUS_DELISTED_CONFIRM", 1))
        if confirm_times > 1:
            streak = increment_delisted_streak(result_filename, item_id)
            if streak < confirm_times:
                mark_item_status_checked(
                    result_filename,
                    item_id,
                    status_checked_at=detected_at,
                    status_source=info.source,
                    status_raw_code=info.raw_code,
                )
                return {
                    "updated": False,
                    "pending_confirmation": True,
                    "streak": streak,
                    "status": info.status,
                    "sold_price": None,
                    "sold_price_display": "",
                    "sold_price_source": "",
                }

    sold_price: Optional[float] = None
    sold_price_display = ""
    sold_price_source = ""
    sold_at: Optional[str] = None
    if info.status == LISTING_STATUS_SOLD:
        sold_price, sold_price_display, sold_price_source = resolve_sold_price(
            info, last_listing_price
        )
        sold_at = detected_at

    updated = persist_listing_status(
        result_filename,
        item_id,
        listing_status=info.status,
        sold_price=sold_price,
        sold_price_display=sold_price_display,
        sold_price_source=sold_price_source,
        sold_at=sold_at,
        status_checked_at=detected_at,
        status_source=info.source,
        status_raw_code=info.raw_code,
        run_id=run_id,
        detail_json=detail_json,
    )
    return {
        "updated": updated,
        "pending_confirmation": False,
        "status": info.status,
        "sold_price": sold_price,
        "sold_price_display": sold_price_display,
        "sold_price_source": sold_price_source,
    }


def _summarize_prices(prices: list[float]) -> dict:
    if not prices:
        return {
            "sample_count": 0,
            "avg_price": None,
            "median_price": None,
            "min_price": None,
            "max_price": None,
        }
    return {
        "sample_count": len(prices),
        "avg_price": round(sum(prices) / len(prices), 2),
        "median_price": round(float(median(prices)), 2),
        "min_price": round(min(prices), 2),
        "max_price": round(max(prices), 2),
    }


def build_sold_status_insights(result_filename: str) -> dict:
    """汇总已售 / 已下架情况与按天的成交价曲线。"""
    events = load_item_status_events(result_filename)
    counts = load_listing_status_counts(result_filename)

    sold_events = [
        event for event in events if event.get("to_status") == LISTING_STATUS_SOLD
    ]

    grouped: dict[str, list[dict]] = defaultdict(list)
    for event in sold_events:
        grouped[str(event.get("detected_at") or "")[:10]].append(event)

    sold_trend: list[dict] = []
    for day in sorted(day for day in grouped if day):
        day_events = grouped[day]
        prices = [
            float(event["price"])
            for event in day_events
            if event.get("price") is not None
        ]
        summary = _summarize_prices(prices)
        sold_trend.append(
            {
                "day": day,
                "sold_count": len(day_events),
                "avg_price": summary["avg_price"],
                "median_price": summary["median_price"],
                "min_price": summary["min_price"],
                "max_price": summary["max_price"],
                "estimated_count": sum(
                    1
                    for event in day_events
                    if event.get("price_source") == PRICE_SOURCE_FALLBACK
                ),
            }
        )

    all_prices = [
        float(event["price"]) for event in sold_events if event.get("price") is not None
    ]
    summary = _summarize_prices(all_prices)
    last_sold_at = max(
        (str(event.get("detected_at") or "") for event in sold_events),
        default=None,
    )

    return {
        "sold_summary": {
            **counts,
            "sold_avg_price": summary["avg_price"],
            "sold_median_price": summary["median_price"],
            "sold_min_price": summary["min_price"],
            "sold_max_price": summary["max_price"],
            "estimated_count": sum(
                1
                for event in sold_events
                if event.get("price_source") == PRICE_SOURCE_FALLBACK
            ),
            "last_sold_at": last_sold_at or None,
        },
        "sold_trend": sold_trend,
    }

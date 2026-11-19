"""
商品在售状态（已售 / 已下架）判定与成交价记录服务。

数据来源是闲鱼商品详情接口 ``mtop.taobao.idle.pc.detail``，返回体里的
``data.itemDO`` 自带三个关键字段：

* ``itemStatus``  商品状态码（**唯一的状态判定依据**）
* ``soldPrice``   商品当前价格（字段名叫「已售价格」，但在售商品上同样有值）
* ``soldCnt``     卖出数量

⚠️ 判定口径（2026-11 修正）
--------------------------------
历史上这里把「``soldCnt > 0`` 或 ``soldPrice`` 有值」当成已售的充分条件，
而 ``soldPrice`` 在**在售商品上恒有值**（它就是当前售价），导致所有商品都被误判成
「已售」，成交价曲线被挂牌价污染。

现在判定**只认 ``itemStatus`` 码表**：

* 码表命中 → active / sold / delisted
* 码表未收录 → ``unknown``（绝不猜测：误判「已售」会污染成交价曲线且是终态）
* 探测失败（接口级失败、响应体商品不符）→ ``probe_ok=False``，调用方**不得改业务状态**

``soldPrice`` / ``soldCnt`` 完全退出判定链路，只作为取证数据带出来（``sold_cnt``
保留在 :class:`ListingStatusInfo` 里，便于日后校准码表）。

``ITEM_STATUS_CODE_MAP`` 环境变量可以免改代码覆盖码表，优先级高于内置表。

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
from typing import Any, Dict, Optional

from src.services.price_history_service import parse_price_value
from src.services.result_storage_service import (
    increment_delisted_streak,
    load_item_status_events,
    load_listing_status_counts,
    mark_item_status_checked,
    persist_listing_status,
)
from src.utils import log_time


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

# ``itemDO.itemStatus`` 的真实码表。
#
# 公开资料查不到 ``mtop.taobao.idle.pc.detail`` 的码表（官方文档只提示「检查
# itemStatus 字段」），必须用真实响应样本回归校准。**未知码一律判 unknown，
# 绝不猜测**——误判「已售」的代价（污染成交价曲线 + 终态永不自愈）远大于判「未知」。
#
# 校准依据（2026-11-19，线上真实样本）：
#   99 条被误判成「已售」的记录，原始码分布为 0×92 / 1×4 / -2×3。
#   交叉验证「最近一次搜索结果里还看得到 = 仍在售」：0 码有 40 条仍在榜，
#   而 1 码 4 条、-2 码 3 条全部不在榜；叠加用户逐条确认「商品实际仍在售」，
#   可判定 **0 = 在售**。
DEFAULT_STATUS_CODE_MAP: Dict[int, str] = {
    # 0 = 商品在售（唯一已校准的码值）
    0: LISTING_STATUS_ACTIVE,
    # 码 1（样本 4 条）与 -2（样本 3 条）量太小，语义仍未确证，**故意不收录**
    # → 一律判 unknown（显示「未知」），宁可少标也不误标成「已售 / 已下架」。
    # 人工核实后在此追加，例如：
    #     1: LISTING_STATUS_SOLD,
    #     -2: LISTING_STATUS_DELISTED,
}

# 「已售优先于状态码」开关已废弃：判定不再依赖 soldCnt / soldPrice。
# 保留读取只是为了提示使用者该配置已经无效。
_SOLD_PRECEDENCE_DEPRECATION_WARNED = False


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
    # False = 探测失败（接口级失败 / 响应体商品不符），调用方**不得改业务状态**
    probe_ok: bool = True


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
    这是免改代码校准码表的手段，优先级高于内置的 :data:`DEFAULT_STATUS_CODE_MAP`。
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
    """把平台状态码映射成业务状态；没有收录的码返回 None（判 unknown）。

    查找顺序：``ITEM_STATUS_CODE_MAP`` 环境变量覆盖 → 内置码表。
    **不再使用「正数 = 在售 / 负数 = 下架」的符号猜测**——那只是猜测，
    一旦猜错就会把在售商品误判成已下架。
    """
    if raw_code is None:
        return None
    code_map = load_status_code_map()
    if raw_code in code_map:
        return code_map[raw_code]
    if raw_code in DEFAULT_STATUS_CODE_MAP:
        return DEFAULT_STATUS_CODE_MAP[raw_code]
    return None


def _warn_sold_precedence_deprecated() -> None:
    """提示 ``ITEM_STATUS_SOLD_PRECEDENCE`` 已失效（只提示一次）。"""
    global _SOLD_PRECEDENCE_DEPRECATION_WARNED
    if _SOLD_PRECEDENCE_DEPRECATION_WARNED:
        return
    raw = os.getenv("ITEM_STATUS_SOLD_PRECEDENCE")
    if raw is None or not str(raw).strip():
        return
    _SOLD_PRECEDENCE_DEPRECATION_WARNED = True
    log_time(
        "[状态判定] ITEM_STATUS_SOLD_PRECEDENCE 已废弃："
        "判定只认 itemStatus 码表，soldCnt / soldPrice 不再参与状态判定。"
    )


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


def parse_listing_status(
    detail_json: dict,
    expected_item_id: Optional[str] = None,
) -> ListingStatusInfo:
    """从详情接口返回体解析商品在售状态。

    判定顺序（2026-11 修正版）：

    1. **接口级失败**：``ret[0]`` 不以 ``SUCCESS`` 开头（会话过期 / 风控 / 限流等）
       → ``probe_ok=False``。**不改业务状态**，调用方只更新复核时间戳。
    2. **响应体一致性**：给了 ``expected_item_id`` 且响应里的 ``itemDO.itemId``
       与之不符 → ``probe_ok=False``（接口返回了别的商品）。
    3. ``itemDO`` 缺失但接口明确成功 → ``delisted``（商品体为空 = 已被删除/下架）。
    4. **查码表**：``itemStatus`` → ``ITEM_STATUS_CODE_MAP`` 覆盖 → 内置码表；
       都没有 → ``unknown``（不猜）。
    5. ``soldPrice`` / ``soldCnt`` **不参与判定**，只作为取证数据带出来。
    """
    detail_json = detail_json or {}
    _warn_sold_precedence_deprecated()
    ret = detail_json.get("ret") or []
    ret_text = str(ret[0]) if isinstance(ret, (list, tuple)) and ret else ""

    # 1) 接口级失败：探测失败，绝不改业务状态
    if not ret_text.startswith("SUCCESS"):
        return ListingStatusInfo(
            status=LISTING_STATUS_UNKNOWN,
            raw_code=None,
            sold_cnt=0,
            sold_price=None,
            sold_price_display="",
            source=SOURCE_ERROR,
            note=ret_text or "接口未返回 ret 标识",
            probe_ok=False,
        )

    data = detail_json.get("data") or {}
    item_do = data.get("itemDO") if isinstance(data, dict) else None
    if not isinstance(item_do, dict) or not item_do:
        # 接口明确成功但商品体为空 = 商品已被删除或下架
        return ListingStatusInfo(
            status=LISTING_STATUS_DELISTED,
            raw_code=None,
            sold_cnt=0,
            sold_price=None,
            sold_price_display="",
            source=SOURCE_MISSING_DETAIL,
            note=ret_text or "itemDO missing",
        )

    # 2) 响应体一致性：防止接口返回了别的商品
    expected = str(expected_item_id or "").strip()
    actual = str(item_do.get("itemId") or "").strip()
    if expected and actual and actual != expected:
        return ListingStatusInfo(
            status=LISTING_STATUS_UNKNOWN,
            raw_code=None,
            sold_cnt=0,
            sold_price=None,
            sold_price_display="",
            source=SOURCE_ERROR,
            note=f"itemId 不一致：期望 {expected}，实际 {actual}",
            probe_ok=False,
        )

    raw_code = _coerce_int(item_do.get("itemStatus"))
    sold_cnt = _coerce_int(item_do.get("soldCnt")) or 0
    sold_price_display = str(item_do.get("soldPrice") or "").strip()
    sold_price = _parse_positive_price(sold_price_display)

    # 3) 查码表；未收录的码一律判 unknown
    mapped = _map_status_code(raw_code)
    status = mapped or LISTING_STATUS_UNKNOWN

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

    ⚠️ 字段名有误导性：详情接口里的 ``soldPrice`` 实际是**商品当前价格**，
    在售商品上同样有值（这正是历史上误判「已售」的根因）。它只有在商品
    **确实成交**之后才等于成交价——因为成交后价格不再变动。

    返回 ``(价格, 展示文本, 来源)``：

    * 平台给的当前价（商品已判定为已售时）→ ``sold_price_api``（真实成交价）
    * 取不到时回退最后一次挂牌价 → ``listing_fallback``（估算，界面必须标注）
    * 都没有 → ``(None, '', '')``

    只有在状态已被判定为 ``sold`` 时才会调用本函数（见
    :func:`apply_listing_status`），否则挂牌价会被冒充成成交价。
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

    **探测失败（``info.probe_ok is False``）直接短路**：只更新复核时间戳与
    ``status_source='error'``，绝不碰 ``listing_status`` / ``sold_price`` /
    ``sold_at`` / 状态事件表——探测失败不是业务结论。
    """
    detected_at = detected_at or datetime.now().isoformat()

    if not info.probe_ok:
        mark_item_status_checked(
            result_filename,
            item_id,
            status_checked_at=detected_at,
            status_source=SOURCE_ERROR,
            keep_raw_code=True,
        )
        return {
            "updated": False,
            "pending_confirmation": False,
            "probe_failed": True,
            "status": info.status,
            "sold_price": None,
            "sold_price_display": "",
            "sold_price_source": "",
            "note": info.note,
        }

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
                    "probe_failed": False,
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
        "probe_failed": False,
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

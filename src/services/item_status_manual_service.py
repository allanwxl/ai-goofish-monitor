"""
商品在售状态（已售 / 已下架）的人工标记服务。

用户在「结果查看」页把某条记录**手动标记为已售或已下架**（例如自己已经拍下、
或者卖家把商品下架了），不必等平台探测给出结论。

四条硬规则：

1. **成交价口径**：人工标记已售不询问价格，用该记录卡片上显示的挂牌价作为
   *估算*成交价（``sold_price_source='listing_fallback'``），界面必须标注
   「挂牌价估算」——绝不把挂牌价伪装成真实成交价。
2. **可撤销**：撤销 = 状态回落到 ``unknown``（那一刻并不知道真实状态，
   不能直接写「在售」）、清空成交价与成交时间、删除该商品的状态事件
   （否则成交价曲线会重复计数）。
3. **已下架 ≠ 已售**：标记已下架会清掉可能残留的成交价与成交事件——商品
   没卖出去就不能进成交价曲线。
4. **人工判断优先**：人工标记的记录不进入自动巡检队列，平台探测结果
   不覆盖人工判断；撤销后保护自然解除。

⚠️ 与 ``result_items.status``（active/hidden/expired，可见性）是两套语义，
互不影响：人工标记已售 / 已下架**不会**隐藏卡片。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from src.services.item_status_service import (
    LISTING_STATUS_DELISTED,
    LISTING_STATUS_SOLD,
    LISTING_STATUS_UNKNOWN,
    PRICE_SOURCE_FALLBACK,
)
from src.services.result_storage_service import (
    MANUAL_STATUS_SOURCE,
    load_item_listing_snapshot,
    persist_listing_status,
    reset_listing_status,
)

# 人工标记的事件行标记，便于事后追溯是哪次人工操作写的。
MANUAL_RUN_ID = "manual"

# 人工标记的记录不参与自动巡检（队列层的守卫条件用的就是这个来源标记）。
STATUS_SOURCE_MANUAL = MANUAL_STATUS_SOURCE

# 人工可标记的终态（撤销时这两个状态都要一起清掉）。
MANUAL_TERMINAL_STATUSES = (LISTING_STATUS_SOLD, LISTING_STATUS_DELISTED)


def _format_price_display(display: str, price: Optional[float]) -> str:
    """把挂牌价展示成「¥1234」的形式。"""
    text = str(display or "").strip()
    if text and "¥" not in text:
        text = f"¥{text}"
    if not text and price is not None:
        text = f"¥{price}"
    return text


def _build_result(
    snapshot: Optional[dict],
    *,
    updated: bool,
    changed: bool,
    reason: str = "",
) -> dict:
    """统一返回结构，字段与 API 响应一一对应。"""
    snapshot = snapshot or {}
    return {
        "updated": updated,
        "changed": changed,
        "listing_status": snapshot.get("listing_status") or LISTING_STATUS_UNKNOWN,
        "sold_price": snapshot.get("sold_price"),
        "sold_price_display": snapshot.get("sold_price_display") or "",
        "sold_price_source": snapshot.get("sold_price_source") or "",
        "sold_at": snapshot.get("sold_at"),
        "reason": reason,
    }


def mark_item_sold_manually(
    filename: str,
    item_id: str,
    *,
    detected_at: str = "",
) -> dict:
    """人工把一条记录标记为已售。

    成交价取该记录的挂牌价（卡片上显示的价格），口径是 *估算*
    （``listing_fallback``）；已是已售的记录不会被覆盖平台成交价。

    返回 ``{"updated", "changed", "listing_status", "sold_price", ...}``；
    商品不存在时 ``updated=False`` 且 ``reason='not_found'``。
    """
    snapshot = load_item_listing_snapshot(filename, item_id)
    if snapshot is None:
        return _build_result(None, updated=False, changed=False, reason="not_found")

    already_sold = snapshot.get("listing_status") == LISTING_STATUS_SOLD
    listing_price = snapshot.get("listing_price")
    listing_price_display = snapshot.get("listing_price_display") or ""

    # 已售记录再标记一次：不覆盖已有成交价（persist 内是 COALESCE 语义，
    # 传 None 即保留原值），只把来源改成人工。
    sold_price = None if already_sold else listing_price
    sold_price_display = (
        "" if already_sold else _format_price_display(listing_price_display, listing_price)
    )
    sold_price_source = "" if already_sold else PRICE_SOURCE_FALLBACK

    stamp = detected_at or datetime.now().isoformat()
    persist_listing_status(
        filename,
        item_id,
        listing_status=LISTING_STATUS_SOLD,
        sold_price=sold_price,
        sold_price_display=sold_price_display,
        sold_price_source=sold_price_source,
        sold_at=stamp,
        status_checked_at=stamp,
        status_source=STATUS_SOURCE_MANUAL,
        # 人工判断没有平台状态码，清掉上一轮留档的原始码，避免污染码表校准样本
        status_raw_code=None,
        run_id=MANUAL_RUN_ID,
        detail_json="",
    )

    latest = load_item_listing_snapshot(filename, item_id)
    return _build_result(
        latest,
        updated=True,
        changed=not already_sold,
    )


def mark_item_delisted_manually(
    filename: str,
    item_id: str,
    *,
    detected_at: str = "",
) -> dict:
    """人工把一条记录标记为已下架。

    已下架 = 商品没卖出去（卖家自己下架 / 商品被删除），因此：

    * 清掉可能残留的成交价与成交事件——下架不能进成交价曲线；
    * 状态来源记为人工，平台探测结果不覆盖人工判断。

    返回 ``{"updated", "changed", "listing_status", "sold_price", ...}``；
    商品不存在时 ``updated=False`` 且 ``reason='not_found'``。
    """
    snapshot = load_item_listing_snapshot(filename, item_id)
    if snapshot is None:
        return _build_result(None, updated=False, changed=False, reason="not_found")

    already_delisted = snapshot.get("listing_status") == LISTING_STATUS_DELISTED

    # 先把「已售 / 已下架」的残留状态、成交价与状态事件清掉：
    # 商品没成交，残留的 sold_price / sold 事件会污染成交价曲线。
    reset_listing_status(
        filename,
        statuses=MANUAL_TERMINAL_STATUSES,
        item_ids=[item_id],
    )

    stamp = detected_at or datetime.now().isoformat()
    persist_listing_status(
        filename,
        item_id,
        listing_status=LISTING_STATUS_DELISTED,
        sold_price=None,
        sold_price_display="",
        sold_price_source="",
        sold_at=None,
        status_checked_at=stamp,
        status_source=STATUS_SOURCE_MANUAL,
        # 人工判断没有平台状态码，清掉上一轮留档的原始码，避免污染码表校准样本
        status_raw_code=None,
        run_id=MANUAL_RUN_ID,
        detail_json="",
    )

    latest = load_item_listing_snapshot(filename, item_id)
    return _build_result(
        latest,
        updated=True,
        changed=not already_delisted,
    )


def clear_manual_listing_status(filename: str, item_id: str) -> dict:
    """撤销人工标记（已售 / 已下架）：状态回未知、清成交价、删状态事件。

    复用既有的 :func:`reset_listing_status`，保证与文件级重置口径一致
    （同样的清空字段与删事件范围）。

    商品不存在时 ``reason='not_found'``；当前不是人工标记的终态时
    ``reason='not_marked'``。
    """
    snapshot = load_item_listing_snapshot(filename, item_id)
    if snapshot is None:
        return _build_result(None, updated=False, changed=False, reason="not_found")
    if snapshot.get("listing_status") not in MANUAL_TERMINAL_STATUSES:
        return _build_result(
            snapshot,
            updated=False,
            changed=False,
            reason="not_marked",
        )

    reset_listing_status(filename, statuses=MANUAL_TERMINAL_STATUSES, item_ids=[item_id])

    latest = load_item_listing_snapshot(filename, item_id)
    return _build_result(latest, updated=True, changed=True)

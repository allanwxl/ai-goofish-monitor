"""
商品在售状态巡检编排。

每次任务执行结束后，对结果集里「未终结」的记录轮询复核一次在售状态
（是否已售 / 已下架），并记录成交价。

风控约束（这些接口和主抓取共用同一个账号，任何一次异常都可能触发验证）：

* 单次运行有复核条数上限与时间预算
* 串行请求 + 随机间隔 + 周期性长休
* 探测失败只更新复核时间，绝不改写业务状态
* 遇到反爬验证立即结束巡检，不影响主任务
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from src.services.item_status_service import (
    LISTING_STATUS_DELISTED,
    LISTING_STATUS_SOLD,
    apply_listing_status,
    parse_listing_status,
)
from src.services.price_history_service import load_price_snapshots
from src.services.result_storage_service import (
    load_status_recheck_queue,
    mark_item_status_check_failed,
)
from src.utils import log_time, random_sleep

DetailFetcher = Callable[..., Awaitable[Optional[dict]]]
Sleeper = Callable[[float, float], Awaitable[None]]


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


def get_status_check_settings() -> dict:
    """读取巡检相关配置。"""
    return {
        "enabled": _env_flag("ITEM_STATUS_CHECK_ENABLED", True),
        "limit": max(0, _env_int("ITEM_STATUS_RECHECK_LIMIT", 20)),
        "interval_hours": max(0, _env_int("ITEM_STATUS_RECHECK_INTERVAL_HOURS", 24)),
        "max_seconds": max(0, _env_int("ITEM_STATUS_RECHECK_MAX_SECONDS", 300)),
        "probe_mode": (os.getenv("ITEM_STATUS_PROBE_MODE") or "auto").strip().lower(),
    }


MTOP_DETAIL_REQUEST_SCRIPT = """
async (itemId) => {
  if (!window.lib || !window.lib.mtop || typeof window.lib.mtop.request !== 'function') {
    return { __unavailable: true };
  }
  const payload = await new Promise((resolve) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) { settled = true; resolve({ __timeout: true }); }
    }, 20000);
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    try {
      window.lib.mtop.request(
        {
          api: 'mtop.taobao.idle.pc.detail',
          data: { itemId: String(itemId) },
          type: 'POST',
          v: '1.0',
          dataType: 'json',
          needLogin: false,
          needLoginPC: false,
          sessionOption: 'AutoLoginOnly',
          ecode: 0
        },
        (res) => finish(res)
      );
    } catch (err) {
      finish({ __error: String(err) });
    }
  });
  return payload;
}
"""


async def probe_item_detail_via_mtop(page, item_id: str) -> Optional[dict]:
    """在已登录页面里直调详情接口，不新开页面。

    返回详情接口的 JSON；mtop 不可用时返回 ``None``（调用方决定是否降级）。
    """
    try:
        payload = await page.evaluate(MTOP_DETAIL_REQUEST_SCRIPT, item_id)
    except Exception as exc:
        log_time(f"   [状态巡检] mtop 探测异常: {exc}")
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("__unavailable"):
        return None
    if payload.get("__timeout"):
        log_time("   [状态巡检] mtop 探测超时。")
        return None
    if payload.get("__error"):
        log_time(f"   [状态巡检] mtop 探测报错: {payload.get('__error')}")
        return None
    return payload


def build_last_listing_prices(keyword: str) -> dict[str, float]:
    """按 item_id 汇总最后一次看到的挂牌价，用作成交价回退口径。"""
    prices: dict[str, float] = {}
    for snapshot in load_price_snapshots(keyword):
        item_id = str(snapshot.get("item_id") or "").strip()
        price = snapshot.get("price")
        if not item_id or price is None:
            continue
        try:
            prices[item_id] = float(price)
        except (TypeError, ValueError):
            continue
    return prices


async def run_status_recheck(
    *,
    context,
    page,
    keyword: str,
    result_filename: str,
    run_id: str,
    fetch_detail: Optional[DetailFetcher] = None,
    sleep: Optional[Sleeper] = None,
    limit: Optional[int] = None,
    interval_hours: Optional[int] = None,
    max_seconds: Optional[int] = None,
    probe_mode: Optional[str] = None,
    seen_item_ids: Optional[set[str]] = None,
) -> dict:
    """复核一批记录的商品在售状态。

    ``fetch_detail`` 与 ``sleep`` 可注入，便于在没有浏览器的环境下测试。
    """
    settings = get_status_check_settings()
    effective_limit = settings["limit"] if limit is None else limit
    effective_interval = (
        settings["interval_hours"] if interval_hours is None else interval_hours
    )
    effective_budget = settings["max_seconds"] if max_seconds is None else max_seconds
    effective_mode = (probe_mode or settings["probe_mode"] or "auto").lower()
    sleeper = sleep or random_sleep

    summary = {
        "checked": 0,
        "sold": 0,
        "delisted": 0,
        "unchanged": 0,
        "failed": 0,
        "skipped": 0,
        "stopped_reason": "",
    }

    if effective_limit <= 0:
        summary["stopped_reason"] = "limit_disabled"
        return summary

    queue = load_status_recheck_queue(
        result_filename,
        limit=effective_limit,
        interval_hours=effective_interval,
        seen_item_ids=seen_item_ids,
    )
    if not queue:
        summary["stopped_reason"] = "empty_queue"
        return summary

    last_prices = build_last_listing_prices(keyword)
    deadline = time.monotonic() + effective_budget if effective_budget > 0 else None

    log_time(
        f"[状态巡检] 本轮待复核 {len(queue)} 条记录"
        f"（模式 {effective_mode}，上限 {effective_limit} 条）。"
    )

    for index, item in enumerate(queue):
        if deadline is not None and time.monotonic() > deadline:
            summary["stopped_reason"] = "time_budget_exceeded"
            log_time("[状态巡检] 已达时间预算，剩余记录留到下轮复核。")
            break

        item_id = item["item_id"]
        detected_at = datetime.now().isoformat()
        detail_json: Optional[dict] = None
        try:
            detail_json = await _probe_one(
                context=context,
                page=page,
                item=item,
                probe_mode=effective_mode,
                fetch_detail=fetch_detail,
            )
        except Exception as exc:
            if type(exc).__name__ == "RiskControlError":
                summary["stopped_reason"] = "risk_control"
                log_time(f"[状态巡检] 触发反爬验证，结束本轮巡检：{exc}")
                break
            log_time(f"[状态巡检] 探测 {item_id} 失败: {exc}")

        if detail_json is None:
            summary["failed"] += 1
            mark_item_status_check_failed(
                result_filename,
                item_id,
                status_checked_at=detected_at,
            )
            continue

        try:
            info = parse_listing_status(detail_json, expected_item_id=item_id)
        except Exception as exc:
            summary["failed"] += 1
            log_time(f"[状态巡检] 解析 {item_id} 响应失败: {exc}")
            mark_item_status_check_failed(
                result_filename,
                item_id,
                status_checked_at=detected_at,
            )
            continue

        # 探测失败（接口级失败 / 返回了别的商品）不是业务结论，绝不改写状态
        if not info.probe_ok:
            summary["failed"] += 1
            log_time(
                f"[状态巡检] {item_id} 探测失败（{info.note or info.source}），保持原状态。"
            )
            mark_item_status_check_failed(
                result_filename,
                item_id,
                status_checked_at=detected_at,
            )
            continue

        try:
            outcome = apply_listing_status(
                result_filename,
                item_id,
                info,
                run_id=run_id,
                last_listing_price=last_prices.get(item_id),
                detail_json=json.dumps(detail_json, ensure_ascii=False)[:2000],
                detected_at=detected_at,
            )
        except Exception as exc:
            summary["failed"] += 1
            log_time(f"[状态巡检] 写入 {item_id} 状态失败: {exc}")
            mark_item_status_check_failed(
                result_filename,
                item_id,
                status_checked_at=detected_at,
            )
            continue

        summary["checked"] += 1
        if outcome.get("pending_confirmation"):
            summary["skipped"] += 1
            log_time(
                f"[状态巡检] {item_id} 疑似已下架，等待二次确认"
                f"（{outcome.get('streak')} 次）。"
            )
        elif outcome.get("status") == LISTING_STATUS_SOLD:
            summary["sold"] += 1
            log_time(
                f"[状态巡检] {item_id} 已售，成交价 {outcome.get('sold_price_display')}"
                f"（{outcome.get('sold_price_source')}）。"
            )
        elif outcome.get("status") == LISTING_STATUS_DELISTED:
            summary["delisted"] += 1
            log_time(f"[状态巡检] {item_id} 已下架。")
        else:
            summary["unchanged"] += 1

        if index + 1 < len(queue):
            await sleeper(1, 2.5)
            if (index + 1) % 10 == 0:
                await sleeper(8, 15)

    return summary


async def _probe_one(
    *,
    context,
    page,
    item: dict,
    probe_mode: str,
    fetch_detail: Optional[DetailFetcher],
) -> Optional[dict]:
    item_id = item["item_id"]
    if probe_mode in {"mtop", "auto"}:
        payload = await probe_item_detail_via_mtop(page, item_id)
        if payload is not None:
            return payload
        if probe_mode == "mtop":
            return None

    if fetch_detail is None:
        return None
    url = item.get("link") or f"https://www.goofish.com/item?id={item_id}"
    return await fetch_detail(context, url)

"""
结果数据的 SQLite 读写服务。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta
from typing import Optional, Sequence

from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.storage_names import build_result_filename
from src.services.price_history_service import parse_price_value
from src.services.result_blacklist_service import (
    match_blacklist_keywords,
    normalize_blacklist_keywords,
)


SORT_COLUMN_MAP = {
    "crawl_time": "crawl_time",
    "publish_time": "COALESCE(publish_time, '')",
    "price": "COALESCE(price, 0)",
    "keyword_hit_count": "keyword_hit_count",
}


def _get_link_unique_key(link: str) -> str:
    return link.split("&", 1)[0]


def _fallback_unique_key(record: dict, item: dict) -> str:
    item_id = str(item.get("商品ID") or "").strip()
    if item_id:
        return f"item:{item_id}"
    digest = hashlib.sha1(
        json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"hash:{digest}"


def _parse_raw_record(raw_json: str, *, status: str | None = None) -> dict:
    record = json.loads(raw_json)
    if status is not None:
        record["_status"] = status
    return record


LISTING_STATUS_FILTER_VALUES = {"active", "sold", "delisted", "unknown"}


def _build_query_conditions(
    *,
    filename: str,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    listing_status: list[str] | None = None,
) -> tuple[str, list]:
    conditions = ["result_filename = ?"]
    params: list = [filename]
    if ai_recommended_only:
        conditions.append("is_recommended = 1")
        conditions.append("analysis_source = ?")
        params.append("ai")
    if keyword_recommended_only:
        conditions.append("is_recommended = 1")
        conditions.append("analysis_source = ?")
        params.append("keyword")
    if listing_status:
        normalized = [
            value for value in listing_status if value in LISTING_STATUS_FILTER_VALUES
        ]
        if normalized:
            placeholders = ", ".join("?" for _ in normalized)
            conditions.append(f"listing_status IN ({placeholders})")
            params.extend(normalized)
    return " AND ".join(conditions), params


def _sort_expression(sort_by: str, sort_order: str) -> str:
    column = SORT_COLUMN_MAP.get(sort_by, SORT_COLUMN_MAP["crawl_time"])
    direction = "ASC" if sort_order == "asc" else "DESC"
    return f"(CASE WHEN status = 'active' THEN 0 ELSE 1 END), {column} {direction}, id {direction}"


def _load_blacklist_keywords_from_conn(conn, filename: str) -> list[str]:
    row = conn.execute(
        """
        SELECT blacklist_keywords_json
        FROM result_blacklist_rules
        WHERE result_filename = ?
        """,
        (filename,),
    ).fetchone()
    if row is None:
        return []
    try:
        payload = json.loads(row["blacklist_keywords_json"] or "[]")
    except json.JSONDecodeError:
        return []
    return normalize_blacklist_keywords(payload)


def _decorate_record_visibility(
    record: dict,
    status: str | None,
    blacklist_keywords: list[str],
    listing: dict | None = None,
) -> dict:
    matched_keywords = match_blacklist_keywords(record, blacklist_keywords)
    hidden_reason = None
    if status == "expired":
        hidden_reason = "expired"
    elif status and status != "active":
        hidden_reason = "manual"
    elif matched_keywords:
        hidden_reason = "rule"

    record["_status"] = status or "active"
    record["_matched_blacklist_keywords"] = matched_keywords
    record["_hidden_reason"] = hidden_reason
    record["_effective_hidden"] = hidden_reason is not None

    listing = listing or {}
    record["_listing_status"] = listing.get("listing_status") or "unknown"
    record["_sold_price"] = listing.get("sold_price")
    record["_sold_price_display"] = listing.get("sold_price_display") or ""
    record["_sold_price_source"] = listing.get("sold_price_source") or ""
    record["_sold_at"] = listing.get("sold_at")
    record["_status_checked_at"] = listing.get("status_checked_at")
    record["_status_source"] = listing.get("status_source") or ""
    record["_status_raw_code"] = listing.get("status_raw_code")
    return record


def _is_record_visible(record: dict) -> bool:
    return record.get("_effective_hidden") is not True


def _load_filtered_records_from_conn(
    conn,
    *,
    filename: str,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    sort_by: str,
    sort_order: str,
    include_hidden: bool,
    listing_status: list[str] | None = None,
) -> list[dict]:
    where_clause, params = _build_query_conditions(
        filename=filename,
        ai_recommended_only=ai_recommended_only,
        keyword_recommended_only=keyword_recommended_only,
        listing_status=listing_status,
    )
    order_clause = _sort_expression(sort_by, sort_order)
    rows = conn.execute(
        f"""
        SELECT raw_json, status, listing_status, sold_price, sold_price_display,
               sold_price_source, sold_at, status_checked_at, status_source,
               status_raw_code
        FROM result_items
        WHERE {where_clause}
        ORDER BY {order_clause}
        """,
        tuple(params),
    ).fetchall()
    blacklist_keywords = _load_blacklist_keywords_from_conn(conn, filename)

    records: list[dict] = []
    for row in rows:
        record = _parse_raw_record(str(row["raw_json"]), status=row["status"])
        decorated = _decorate_record_visibility(
            record,
            row["status"],
            blacklist_keywords,
            listing={
                "listing_status": row["listing_status"],
                "sold_price": row["sold_price"],
                "sold_price_display": row["sold_price_display"],
                "sold_price_source": row["sold_price_source"],
                "sold_at": row["sold_at"],
                "status_checked_at": row["status_checked_at"],
                "status_source": row["status_source"],
                "status_raw_code": row["status_raw_code"],
            },
        )
        if include_hidden or _is_record_visible(decorated):
            records.append(decorated)
    return records


async def save_result_record(record: dict, keyword: str) -> bool:
    return await asyncio.to_thread(_save_result_record_sync, record, keyword)


def _save_result_record_sync(record: dict, keyword: str) -> bool:
    bootstrap_sqlite_storage()
    item = record.get("商品信息", {}) or {}
    analysis = record.get("ai_analysis", {}) or {}
    link = str(item.get("商品链接") or "")
    link_unique_key = _get_link_unique_key(link) if link else _fallback_unique_key(record, item)
    keyword_hit_count = analysis.get("keyword_hit_count", 0)
    try:
        keyword_hit_count = int(keyword_hit_count)
    except (TypeError, ValueError):
        keyword_hit_count = 0

    with sqlite_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO result_items (
                result_filename, keyword, task_name, crawl_time, publish_time, price,
                price_display, item_id, title, link, link_unique_key, seller_nickname,
                is_recommended, analysis_source, keyword_hit_count, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                build_result_filename(keyword),
                record.get("搜索关键字", keyword),
                record.get("任务名称", ""),
                record.get("爬取时间", ""),
                item.get("发布时间"),
                parse_price_value(item.get("当前售价")),
                item.get("当前售价"),
                item.get("商品ID"),
                item.get("商品标题"),
                link,
                link_unique_key,
                (record.get("卖家信息", {}) or {}).get("卖家昵称") or item.get("卖家昵称"),
                1 if analysis.get("is_recommended") else 0,
                analysis.get("analysis_source"),
                keyword_hit_count,
                json.dumps(record, ensure_ascii=False),
            ),
        )
        conn.commit()
    return True


def load_processed_link_keys(keyword: str) -> set[str]:
    bootstrap_sqlite_storage()
    filename = build_result_filename(keyword)
    with sqlite_connection() as conn:
        rows = conn.execute(
            "SELECT link_unique_key FROM result_items WHERE result_filename = ?",
            (filename,),
        ).fetchall()
    return {str(row["link_unique_key"]) for row in rows if row["link_unique_key"]}


async def list_result_filenames() -> list[str]:
    return await asyncio.to_thread(_list_result_filenames_sync)


def _list_result_filenames_sync() -> list[str]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        rows = conn.execute(
            """
            SELECT result_filename, MAX(crawl_time) AS latest_crawl_time
            FROM result_items
            GROUP BY result_filename
            ORDER BY latest_crawl_time DESC, result_filename DESC
            """
        ).fetchall()
    return [str(row["result_filename"]) for row in rows]


async def result_file_exists(filename: str) -> bool:
    return await asyncio.to_thread(_result_file_exists_sync, filename)


def _result_file_exists_sync(filename: str) -> bool:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM result_items WHERE result_filename = ? LIMIT 1",
            (filename,),
        ).fetchone()
    return row is not None


async def delete_result_file_records(filename: str) -> int:
    return await asyncio.to_thread(_delete_result_file_records_sync, filename)


def _delete_result_file_records_sync(filename: str) -> int:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM result_items WHERE result_filename = ?",
            (filename,),
        )
        conn.commit()
    return int(cursor.rowcount or 0)


async def query_result_records(
    filename: str,
    *,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    sort_by: str,
    sort_order: str,
    page: int,
    limit: int,
    include_hidden: bool = False,
    listing_status: list[str] | None = None,
) -> tuple[int, list[dict]]:
    return await asyncio.to_thread(
        _query_result_records_sync,
        filename,
        ai_recommended_only,
        keyword_recommended_only,
        sort_by,
        sort_order,
        page,
        limit,
        include_hidden,
        listing_status,
    )


def _query_result_records_sync(
    filename: str,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    sort_by: str,
    sort_order: str,
    page: int,
    limit: int,
    include_hidden: bool,
    listing_status: list[str] | None,
) -> tuple[int, list[dict]]:
    bootstrap_sqlite_storage()
    offset = max(page - 1, 0) * limit
    with sqlite_connection() as conn:
        records = _load_filtered_records_from_conn(
            conn,
            filename=filename,
            ai_recommended_only=ai_recommended_only,
            keyword_recommended_only=keyword_recommended_only,
            sort_by=sort_by,
            sort_order=sort_order,
            include_hidden=include_hidden,
            listing_status=listing_status,
        )
    total = len(records)
    return total, records[offset: offset + limit]


async def load_all_result_records(
    filename: str,
    *,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    sort_by: str,
    sort_order: str,
    include_hidden: bool = False,
    listing_status: list[str] | None = None,
) -> list[dict]:
    return await asyncio.to_thread(
        _load_all_result_records_sync,
        filename,
        ai_recommended_only,
        keyword_recommended_only,
        sort_by,
        sort_order,
        include_hidden,
        listing_status,
    )


def _load_all_result_records_sync(
    filename: str,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    sort_by: str,
    sort_order: str,
    include_hidden: bool,
    listing_status: list[str] | None,
) -> list[dict]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        return _load_filtered_records_from_conn(
            conn,
            filename=filename,
            ai_recommended_only=ai_recommended_only,
            keyword_recommended_only=keyword_recommended_only,
            sort_by=sort_by,
            sort_order=sort_order,
            include_hidden=include_hidden,
            listing_status=listing_status,
        )


async def build_result_ndjson(filename: str) -> str:
    return await asyncio.to_thread(_build_result_ndjson_sync, filename)


def _build_result_ndjson_sync(filename: str) -> str:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        rows = conn.execute(
            "SELECT raw_json FROM result_items WHERE result_filename = ? ORDER BY id ASC",
            (filename,),
        ).fetchall()
    return "\n".join(str(row["raw_json"]) for row in rows)


async def load_result_summary(filename: str) -> dict | None:
    return await asyncio.to_thread(_load_result_summary_sync, filename)


def _load_result_summary_sync(filename: str) -> dict | None:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        visible_records = _load_filtered_records_from_conn(
            conn,
            filename=filename,
            ai_recommended_only=False,
            keyword_recommended_only=False,
            sort_by="crawl_time",
            sort_order="desc",
            include_hidden=False,
        )
    if not visible_records:
        return None

    recommended_records = [
        record
        for record in visible_records
        if (record.get("ai_analysis", {}) or {}).get("is_recommended") is True
    ]
    ai_recommended_items = 0
    keyword_recommended_items = 0
    for record in recommended_records:
        source = (record.get("ai_analysis", {}) or {}).get("analysis_source")
        if source == "ai":
            ai_recommended_items += 1
        elif source == "keyword":
            keyword_recommended_items += 1

    return {
        "total_items": len(visible_records),
        "recommended_items": len(recommended_records),
        "ai_recommended_items": ai_recommended_items,
        "keyword_recommended_items": keyword_recommended_items,
        "latest_crawl_time": visible_records[0].get("爬取时间"),
        "latest_record": visible_records[0],
        "latest_recommendation": recommended_records[0] if recommended_records else None,
    }


async def update_item_status(filename: str, item_id: str, status: str) -> bool:
    valid = {"active", "hidden", "expired"}
    if status not in valid:
        raise ValueError(f"status must be one of {valid}")
    return await asyncio.to_thread(_update_item_status_sync, filename, item_id, status)


def _update_item_status_sync(filename: str, item_id: str, status: str) -> bool:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        cursor = conn.execute(
            "UPDATE result_items SET status = ? WHERE result_filename = ? AND item_id = ?",
            (status, filename, item_id),
        )
        conn.commit()
        return cursor.rowcount > 0


async def load_result_blacklist_keywords(filename: str) -> list[str]:
    return await asyncio.to_thread(_load_result_blacklist_keywords_sync, filename)


def _load_result_blacklist_keywords_sync(filename: str) -> list[str]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        return _load_blacklist_keywords_from_conn(conn, filename)


async def save_result_blacklist_keywords(filename: str, keywords: list[str]) -> list[str]:
    return await asyncio.to_thread(_save_result_blacklist_keywords_sync, filename, keywords)


def _save_result_blacklist_keywords_sync(filename: str, keywords: list[str]) -> list[str]:
    bootstrap_sqlite_storage()
    normalized_keywords = normalize_blacklist_keywords(keywords)
    now = datetime.now().isoformat()
    with sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO result_blacklist_rules (
                result_filename, blacklist_keywords_json, updated_at
            ) VALUES (?, ?, ?)
            ON CONFLICT(result_filename) DO UPDATE SET
                blacklist_keywords_json = excluded.blacklist_keywords_json,
                updated_at = excluded.updated_at
            """,
            (filename, json.dumps(normalized_keywords, ensure_ascii=False), now),
        )
        conn.commit()
    return normalized_keywords


def load_visible_result_item_ids(filename: str) -> set[str]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        visible_records = _load_filtered_records_from_conn(
            conn,
            filename=filename,
            ai_recommended_only=False,
            keyword_recommended_only=False,
            sort_by="crawl_time",
            sort_order="desc",
            include_hidden=False,
        )
    item_ids: set[str] = set()
    for record in visible_records:
        product = record.get("商品信息", {}) or {}
        item_id = str(product.get("商品ID") or "").strip()
        if item_id:
            item_ids.add(item_id)
    return item_ids


# ── 商品在售状态（已售 / 已下架）相关存储 ──────────────────────────────

# 默认只复核「未终结」的记录；已判定为已售 / 已下架的记录是终态。
RECHECKABLE_LISTING_STATUSES = ("active", "unknown")

# 打开 ITEM_STATUS_RECHECK_TERMINAL 后，终态记录也参与复核（误判可自动纠正）。
TERMINAL_RECHECKABLE_LISTING_STATUSES = ("active", "unknown", "sold", "delisted")

# 允许被重置的业务状态（重置即回到 unknown）。
RESETTABLE_LISTING_STATUSES = ("sold", "delisted")

# 人工标记（用户在结果页手动标记已售）的记录来源标记。
MANUAL_STATUS_SOURCE = "manual"

# 人工标记的「已售」不参与自动巡检：平台探测结果不覆盖人工判断。
MANUAL_SOLD_GUARD_SQL = "NOT (listing_status = 'sold' AND status_source = 'manual')"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _recheckable_listing_statuses() -> tuple[str, ...]:
    """本轮允许参与复核的商品状态集合。"""
    if _env_flag("ITEM_STATUS_RECHECK_TERMINAL", False):
        return TERMINAL_RECHECKABLE_LISTING_STATUSES
    return RECHECKABLE_LISTING_STATUSES


def load_status_recheck_queue(
    filename: str,
    *,
    limit: int = 20,
    interval_hours: int = 24,
    seen_item_ids: Optional[set[str]] = None,
) -> list[dict]:
    """挑出本轮需要复核在售状态的记录。

    默认只复核未终结的记录（active / unknown），已判定为已售或已下架的不再复核；
    设置 ``ITEM_STATUS_RECHECK_TERMINAL=true`` 后终态记录也会参与复核。
    排序优先「AI 推荐命中」，其次「最久没查过」，最后「最近抓取」。

    ``seen_item_ids`` 是本次运行在搜索结果里见过的商品。「本次没出现」的商品
    更可能已经成交或被下架，所以会优先复核——但它只是排序权重，
    绝不据此直接判定下架（搜索排序与分页同样会让商品缺席）。
    """
    bootstrap_sqlite_storage()
    cutoff = (
        datetime.now() - timedelta(hours=max(0, interval_hours))
    ).isoformat()
    recheckable = _recheckable_listing_statuses()
    placeholders = ", ".join("?" for _ in recheckable)
    candidate_limit = max(int(limit) * 10, 200)
    with sqlite_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT item_id, link, title, price, price_display, listing_status,
                   status_checked_at, is_recommended, crawl_time
            FROM result_items
            WHERE result_filename = ?
              AND (listing_status IS NULL OR listing_status IN ({placeholders}))
              AND (status_checked_at IS NULL OR status_checked_at <= ?)
              AND {MANUAL_SOLD_GUARD_SQL}
            ORDER BY is_recommended DESC,
                     COALESCE(status_checked_at, '') ASC,
                     crawl_time DESC
            LIMIT ?
            """,
            (filename, *recheckable, cutoff, candidate_limit),
        ).fetchall()

    entries = [
        {
            "item_id": str(row["item_id"] or ""),
            "link": str(row["link"] or ""),
            "title": str(row["title"] or ""),
            "price": row["price"],
            "price_display": str(row["price_display"] or ""),
            "listing_status": str(row["listing_status"] or "unknown"),
            "status_checked_at": row["status_checked_at"],
            "is_recommended": bool(row["is_recommended"]),
            "crawl_time": str(row["crawl_time"] or ""),
        }
        for row in rows
        if str(row["item_id"] or "").strip()
    ]

    if seen_item_ids:
        normalized_seen = {str(item_id) for item_id in seen_item_ids}
        entries.sort(
            key=lambda entry: (
                not entry["is_recommended"],
                entry["item_id"] in normalized_seen,
                entry["status_checked_at"] or "",
            )
        )

    return entries[: max(0, int(limit))]


def load_item_listing_snapshot(filename: str, item_id: str) -> Optional[dict]:
    """读取单条记录的挂牌价与当前在售状态（人工标记用）。

    挂牌价优先取 ``raw_json`` 里的「当前售价」——那正是卡片上显示的那个价；
    取不到时回落到 ``result_items.price`` 列。所有查询都带显式 WHERE。
    """
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute(
            """
            SELECT listing_status, sold_price, sold_price_display, sold_price_source,
                   sold_at, price, price_display, raw_json
            FROM result_items
            WHERE result_filename = ? AND item_id = ?
            """,
            (filename, item_id),
        ).fetchone()
    if row is None:
        return None

    listing_price = None
    listing_price_display = ""
    raw_json = str(row["raw_json"] or "")
    if raw_json:
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            payload = {}
        product = payload.get("商品信息") if isinstance(payload, dict) else None
        if isinstance(product, dict):
            raw_price = str(product.get("当前售价") or "").strip()
            if raw_price:
                listing_price_display = raw_price
                listing_price = parse_price_value(raw_price)

    if listing_price is None:
        listing_price = row["price"]
    if not listing_price_display:
        listing_price_display = str(row["price_display"] or "")

    return {
        "listing_status": str(row["listing_status"] or "unknown"),
        "sold_price": row["sold_price"],
        "sold_price_display": str(row["sold_price_display"] or ""),
        "sold_price_source": str(row["sold_price_source"] or ""),
        "sold_at": row["sold_at"],
        "listing_price": listing_price,
        "listing_price_display": listing_price_display,
    }


def persist_listing_status(
    filename: str,
    item_id: str,
    *,
    listing_status: str,
    sold_price: float | None = None,
    sold_price_display: str = "",
    sold_price_source: str = "",
    sold_at: str | None = None,
    status_checked_at: str = "",
    status_source: str = "",
    status_raw_code: int | None = None,
    run_id: str = "",
    detail_json: str = "",
) -> bool:
    """写入商品的最新在售状态，并在状态变化时追加一条状态事件。

    ``sold_at`` 只在首次判定为已售时落库；重复复核同一状态不会产生重复事件。
    """
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute(
            """
            SELECT listing_status, sold_price
            FROM result_items
            WHERE result_filename = ? AND item_id = ?
            """,
            (filename, item_id),
        ).fetchone()
        if row is None:
            return False

        previous_status = str(row["listing_status"] or "unknown")
        previous_price = row["sold_price"]
        status_changed = previous_status != listing_status
        price_first_seen = (
            sold_price is not None
            and previous_price is None
        )

        conn.execute(
            """
            UPDATE result_items
            SET listing_status = ?,
                sold_price = COALESCE(?, sold_price),
                sold_price_display = CASE WHEN ? != '' THEN ? ELSE sold_price_display END,
                sold_price_source = CASE WHEN ? != '' THEN ? ELSE sold_price_source END,
                sold_at = CASE WHEN ? = 'sold' THEN COALESCE(sold_at, ?) ELSE sold_at END,
                status_checked_at = ?,
                status_source = ?,
                status_raw_code = ?,
                delisted_streak = CASE
                    WHEN ? = 'delisted' THEN delisted_streak
                    ELSE 0
                END
            WHERE result_filename = ? AND item_id = ?
            """,
            (
                listing_status,
                sold_price,
                sold_price_display,
                sold_price_display,
                sold_price_source,
                sold_price_source,
                listing_status,
                sold_at or "",
                status_checked_at,
                status_source,
                status_raw_code,
                listing_status,
                filename,
                item_id,
            ),
        )

        if status_changed or price_first_seen:
            conn.execute(
                """
                INSERT INTO item_status_events (
                    result_filename, item_id, from_status, to_status, price,
                    price_display, price_source, status_raw_code, detected_at,
                    run_id, detail_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    filename,
                    item_id,
                    previous_status,
                    listing_status,
                    sold_price,
                    sold_price_display,
                    sold_price_source,
                    status_raw_code,
                    status_checked_at,
                    run_id,
                    detail_json,
                ),
            )
        conn.commit()
    return True


def mark_item_status_checked(
    filename: str,
    item_id: str,
    *,
    status_checked_at: str = "",
    status_source: str = "",
    status_raw_code: int | None = None,
    keep_raw_code: bool = False,
) -> bool:
    """只更新复核时间戳与来源，不改变商品的listing_status。

    ``keep_raw_code=True`` 时保留原有的 ``status_raw_code``（探测失败时用），
    避免把上一轮留档的原始状态码覆盖成空。
    """
    bootstrap_sqlite_storage()
    raw_code_expr = "COALESCE(?, status_raw_code)" if keep_raw_code else "?"
    with sqlite_connection() as conn:
        cursor = conn.execute(
            f"""
            UPDATE result_items
            SET status_checked_at = ?,
                status_source = ?,
                status_raw_code = {raw_code_expr}
            WHERE result_filename = ? AND item_id = ?
            """,
            (status_checked_at, status_source, status_raw_code, filename, item_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def load_delisted_streak(filename: str, item_id: str) -> int:
    """读取连续判定为「已下架」的次数。"""
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute(
            """
            SELECT delisted_streak FROM result_items
            WHERE result_filename = ? AND item_id = ?
            """,
            (filename, item_id),
        ).fetchone()
    if row is None:
        return 0
    return int(row["delisted_streak"] or 0)


def increment_delisted_streak(filename: str, item_id: str) -> int:
    """累加「连续判定为已下架」的次数，返回累加后的值。

    用于「连续多次都判下架才落终态」的保守模式：即使本轮还没达到确认次数，
    计数也要累加，否则永远攒不够次数。
    """
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        conn.execute(
            """
            UPDATE result_items
            SET delisted_streak = COALESCE(delisted_streak, 0) + 1
            WHERE result_filename = ? AND item_id = ?
            """,
            (filename, item_id),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT delisted_streak FROM result_items
            WHERE result_filename = ? AND item_id = ?
            """,
            (filename, item_id),
        ).fetchone()
    if row is None:
        return 0
    return int(row["delisted_streak"] or 0)


def mark_item_status_check_failed(
    filename: str,
    item_id: str,
    *,
    status_checked_at: str = "",
    keep_raw_code: bool = True,
) -> bool:
    """复核失败时只更新时间戳，不改变商品状态。

    查不到商品不等于商品已下架，绝不能因为一次探测失败就改写业务状态。
    ``keep_raw_code`` 默认保留上一轮的原始状态码，便于事后校准码表。
    """
    return mark_item_status_checked(
        filename,
        item_id,
        status_checked_at=status_checked_at,
        status_source="error",
        keep_raw_code=keep_raw_code,
    )


def delete_item_status_events(
    filename: str,
    *,
    to_statuses: Sequence[str] = ("sold", "delisted"),
    item_ids: Optional[Sequence[str]] = None,
) -> int:
    """删除指定结果文件里的状态事件（成交价曲线的数据源）。

    返回删除的条数。所有 DELETE 都带显式 WHERE。
    """
    bootstrap_sqlite_storage()
    normalized = [str(s).strip().lower() for s in to_statuses if str(s).strip()]
    if not normalized:
        return 0

    conditions = ["result_filename = ?"]
    params: list = [filename]
    placeholders = ", ".join("?" for _ in normalized)
    conditions.append(f"to_status IN ({placeholders})")
    params.extend(normalized)

    normalized_ids = [str(i).strip() for i in (item_ids or []) if str(i).strip()]
    if normalized_ids:
        # 分批 IN，避免一次命中太多记录时超出 SQLite 变量上限
        deleted = 0
        with sqlite_connection() as conn:
            for start in range(0, len(normalized_ids), 400):
                chunk = normalized_ids[start : start + 400]
                chunk_placeholders = ", ".join("?" for _ in chunk)
                cursor = conn.execute(
                    f"""
                    DELETE FROM item_status_events
                    WHERE result_filename = ?
                      AND to_status IN ({placeholders})
                      AND item_id IN ({chunk_placeholders})
                    """,
                    (filename, *normalized, *chunk),
                )
                deleted += int(cursor.rowcount or 0)
            conn.commit()
        return deleted

    with sqlite_connection() as conn:
        cursor = conn.execute(
            f"DELETE FROM item_status_events WHERE {' AND '.join(conditions)}",
            params,
        )
        conn.commit()
        return int(cursor.rowcount or 0)


def reset_listing_status(
    filename: str,
    *,
    statuses: Sequence[str] = ("sold", "delisted"),
    item_ids: Optional[Sequence[str]] = None,
    status_raw_codes: Optional[Sequence[int]] = None,
) -> dict:
    """把误判记录重置回 unknown，清空成交价与相关事件。

    用于清理「商品实际仍在售却被标记成已售」的存量数据：

    * ``result_items``：状态回到 ``unknown``，清空成交价 / 成交时间，
      ``status_checked_at`` 置空（保证下一轮复核优先挑中它）
    * ``item_status_events``：删除**同一批记录**对应的状态事件（成交价曲线的数据源）

    事件删除用子查询锁定「即将被重置的那批记录」，因此按 ``item_ids`` /
    ``status_raw_codes`` 收敛时不会误伤其它记录的历史事件。

    所有 UPDATE / DELETE 都带显式 WHERE。

    返回 ``{"reset_items": n, "deleted_events": m}``。
    """
    bootstrap_sqlite_storage()
    normalized_statuses = [
        str(s).strip().lower() for s in statuses if str(s).strip()
    ]
    if not normalized_statuses:
        return {"reset_items": 0, "deleted_events": 0}

    conditions = ["result_filename = ?"]
    where_params: list = [filename]
    status_placeholders = ", ".join("?" for _ in normalized_statuses)
    conditions.append(f"listing_status IN ({status_placeholders})")
    where_params.extend(normalized_statuses)

    normalized_ids = [str(i).strip() for i in (item_ids or []) if str(i).strip()]
    if normalized_ids:
        id_placeholders = ", ".join("?" for _ in normalized_ids)
        conditions.append(f"item_id IN ({id_placeholders})")
        where_params.extend(normalized_ids)

    normalized_codes: list[int] = []
    for code in status_raw_codes or []:
        try:
            normalized_codes.append(int(code))
        except (TypeError, ValueError):
            continue
    if normalized_codes:
        code_placeholders = ", ".join("?" for _ in normalized_codes)
        conditions.append(f"status_raw_code IN ({code_placeholders})")
        where_params.extend(normalized_codes)

    where = " AND ".join(conditions)

    with sqlite_connection() as conn:
        # 先删事件：子查询基于「重置前」的状态，正好锁定这批记录
        deleted_events = int(
            conn.execute(
                f"""
                DELETE FROM item_status_events
                WHERE result_filename = ?
                  AND to_status IN ({status_placeholders})
                  AND item_id IN (
                      SELECT item_id FROM result_items WHERE {where}
                  )
                """,
                (filename, *normalized_statuses, *where_params),
            ).rowcount
            or 0
        )

        # 再重置记录：状态回 unknown，清空成交价，清空复核时间戳
        cursor = conn.execute(
            f"""
            UPDATE result_items
            SET listing_status = 'unknown',
                sold_price = NULL,
                sold_price_display = '',
                sold_price_source = '',
                sold_at = NULL,
                delisted_streak = 0,
                status_checked_at = '',
                status_source = NULL
            WHERE {where}
            """,
            where_params,
        )
        reset_items = int(cursor.rowcount or 0)
        conn.commit()

    return {"reset_items": reset_items, "deleted_events": deleted_events}


def load_item_status_events(filename: str) -> list[dict]:
    """读取某个结果集的商品状态变更事件流。"""
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        rows = conn.execute(
            """
            SELECT item_id, from_status, to_status, price, price_display,
                   price_source, status_raw_code, detected_at, run_id
            FROM item_status_events
            WHERE result_filename = ?
            ORDER BY detected_at ASC, id ASC
            """,
            (filename,),
        ).fetchall()
    return [
        {
            "item_id": str(row["item_id"] or ""),
            "from_status": row["from_status"],
            "to_status": row["to_status"],
            "price": row["price"],
            "price_display": str(row["price_display"] or ""),
            "price_source": str(row["price_source"] or ""),
            "status_raw_code": row["status_raw_code"],
            "detected_at": str(row["detected_at"] or ""),
            "run_id": str(row["run_id"] or ""),
        }
        for row in rows
    ]


def load_listing_status_counts(filename: str) -> dict[str, int]:
    """统计某个结果集里各在售状态下的记录数。"""
    bootstrap_sqlite_storage()
    counts = {
        "active_count": 0,
        "sold_count": 0,
        "delisted_count": 0,
        "unknown_count": 0,
    }
    with sqlite_connection() as conn:
        rows = conn.execute(
            """
            SELECT listing_status, COUNT(1) AS total
            FROM result_items
            WHERE result_filename = ?
            GROUP BY listing_status
            """,
            (filename,),
        ).fetchall()
    counter = {
        "active": "active_count",
        "sold": "sold_count",
        "delisted": "delisted_count",
        "unknown": "unknown_count",
    }
    for row in rows:
        key = counter.get(str(row["listing_status"] or "unknown"))
        if key:
            counts[key] = int(row["total"] or 0)
    return counts

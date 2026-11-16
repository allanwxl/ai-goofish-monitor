"""商品在售状态（已售 / 已下架）判定与成交价记录测试。"""

import asyncio
import json
import sqlite3

from src.infrastructure.persistence.sqlite_connection import (
    init_schema,
    sqlite_connection,
)
from src.services.item_status_rechecker import run_status_recheck
from src.services.item_status_service import (
    LISTING_STATUS_ACTIVE,
    LISTING_STATUS_DELISTED,
    LISTING_STATUS_SOLD,
    LISTING_STATUS_UNKNOWN,
    apply_listing_status,
    build_sold_status_insights,
    parse_listing_status,
    resolve_sold_price,
)
from src.services.result_storage_service import (
    load_item_status_events,
    load_status_recheck_queue,
    save_result_record,
)


def _detail(item_do=None, ret="SUCCESS::调用成功"):
    payload = {"ret": [ret], "data": {}}
    if item_do is not None:
        payload["data"]["itemDO"] = item_do
    return payload


def _save_item(item_id: str, *, price: str = "¥1000", keyword: str = "demo"):
    return asyncio.run(
        save_result_record(
            {
                "爬取时间": "2026-01-01T10:00:00",
                "搜索关键字": keyword,
                "任务名称": "Demo 任务",
                "商品信息": {
                    "商品ID": item_id,
                    "商品标题": f"Demo {item_id}",
                    "商品链接": f"https://www.goofish.com/item?id={item_id}",
                    "当前售价": price,
                },
                "卖家信息": {"卖家昵称": "卖家A"},
                "ai_analysis": {
                    "analysis_source": "ai",
                    "is_recommended": True,
                    "reason": "ok",
                },
            },
            keyword,
        )
    )


# ── 状态判定 ────────────────────────────────────────────────────────────


def test_parse_listing_status_active():
    info = parse_listing_status(_detail({"itemStatus": 1, "soldCnt": 0}))
    assert info.status == LISTING_STATUS_ACTIVE
    assert info.raw_code == 1
    assert info.sold_cnt == 0


def test_parse_listing_status_delisted_by_negative_code():
    info = parse_listing_status(_detail({"itemStatus": -98, "soldCnt": 0}))
    assert info.status == LISTING_STATUS_DELISTED
    assert info.raw_code == -98


def test_parse_listing_status_sold_by_sold_count():
    info = parse_listing_status(_detail({"itemStatus": -98, "soldCnt": 1}))
    # 已售优先于状态码：卖家下架后又成交的情况也要判成已售
    assert info.status == LISTING_STATUS_SOLD
    assert info.sold_cnt == 1


def test_parse_listing_status_sold_by_sold_price():
    info = parse_listing_status(_detail({"itemStatus": 1, "soldCnt": 0, "soldPrice": "3500"}))
    assert info.status == LISTING_STATUS_SOLD
    assert info.sold_price == 3500.0
    assert info.sold_price_display == "3500"


def test_parse_listing_status_missing_item_do_is_delisted():
    info = parse_listing_status(_detail(None, ret="FAIL_SYS_ITEM_NOT_EXIST::商品不存在"))
    assert info.status == LISTING_STATUS_DELISTED
    assert info.source == "missing_detail"
    assert "FAIL_SYS_ITEM_NOT_EXIST" in info.note


def test_parse_listing_status_empty_item_do_is_delisted():
    info = parse_listing_status(_detail({}))
    assert info.status == LISTING_STATUS_DELISTED
    assert info.source == "missing_detail"


def test_parse_listing_status_unknown_when_fields_missing():
    info = parse_listing_status(_detail({"title": "没有状态字段的商品"}))
    assert info.status == LISTING_STATUS_UNKNOWN
    assert info.raw_code is None


def test_parse_listing_status_sold_precedence_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ITEM_STATUS_SOLD_PRECEDENCE", "false")
    info = parse_listing_status(_detail({"itemStatus": 1, "soldCnt": 0, "soldPrice": "3500"}))
    assert info.status == LISTING_STATUS_ACTIVE


def test_status_code_map_can_override(monkeypatch):
    monkeypatch.setenv("ITEM_STATUS_CODE_MAP", json.dumps({"-98": "delisted", "7": "sold"}))
    info = parse_listing_status(_detail({"itemStatus": 7, "soldCnt": 0}))
    assert info.status == LISTING_STATUS_SOLD


# ── 成交价口径 ──────────────────────────────────────────────────────────


def test_resolve_sold_price_prefers_platform_price():
    info = parse_listing_status(_detail({"itemStatus": 1, "soldCnt": 1, "soldPrice": "3500"}))
    price, display, source = resolve_sold_price(info, 9999.0)
    assert price == 3500.0
    assert display == "¥3500"
    assert source == "sold_price_api"


def test_resolve_sold_price_falls_back_to_last_listing_price():
    info = parse_listing_status(_detail({"itemStatus": 1, "soldCnt": 1}))
    price, display, source = resolve_sold_price(info, 4200.0)
    assert price == 4200.0
    assert display == "¥4200.0"
    assert source == "listing_fallback"


def test_resolve_sold_price_without_any_price():
    info = parse_listing_status(_detail({"itemStatus": 1, "soldCnt": 1}))
    price, display, source = resolve_sold_price(info, None)
    assert price is None
    assert display == ""
    assert source == ""


# ── 数据层：迁移与落库 ──────────────────────────────────────────────────


def test_migration_adds_listing_columns_to_legacy_table(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        """
        CREATE TABLE result_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            result_filename TEXT NOT NULL,
            keyword TEXT NOT NULL,
            task_name TEXT NOT NULL,
            crawl_time TEXT NOT NULL,
            publish_time TEXT,
            price REAL,
            price_display TEXT,
            item_id TEXT,
            title TEXT,
            link TEXT,
            link_unique_key TEXT NOT NULL,
            seller_nickname TEXT,
            is_recommended INTEGER NOT NULL,
            analysis_source TEXT,
            keyword_hit_count INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            raw_json TEXT NOT NULL,
            UNIQUE(result_filename, link_unique_key)
        )
        """
    )
    conn.commit()
    conn.close()

    with sqlite_connection(str(db_path)) as conn:
        init_schema(conn)
        init_schema(conn)  # 幂等：重复执行不应报错
        columns = [row[1] for row in conn.execute("PRAGMA table_info(result_items)").fetchall()]
        events_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='item_status_events'"
        ).fetchone()

    for column in (
        "listing_status",
        "sold_price",
        "sold_price_display",
        "sold_price_source",
        "sold_at",
        "status_checked_at",
        "status_source",
        "status_raw_code",
        "delisted_streak",
    ):
        assert column in columns
    assert events_table is not None


def test_migration_backfills_columns_missing_from_already_migrated_db(tmp_path, monkeypatch):
    """已经打过迁移标记的库，后来新增的列也必须自动补上。"""
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "partial.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        """
        CREATE TABLE result_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            result_filename TEXT NOT NULL,
            keyword TEXT NOT NULL,
            task_name TEXT NOT NULL,
            crawl_time TEXT NOT NULL,
            publish_time TEXT,
            price REAL,
            price_display TEXT,
            item_id TEXT,
            title TEXT,
            link TEXT,
            link_unique_key TEXT NOT NULL,
            seller_nickname TEXT,
            is_recommended INTEGER NOT NULL,
            analysis_source TEXT,
            keyword_hit_count INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            listing_status TEXT NOT NULL DEFAULT 'unknown',
            raw_json TEXT NOT NULL,
            UNIQUE(result_filename, link_unique_key)
        )
        """
    )
    conn.execute(
        "INSERT INTO app_metadata(key, value) VALUES ('migration:result_items_listing_status', 'done')"
    )
    conn.commit()
    conn.close()

    with sqlite_connection(str(db_path)) as conn:
        init_schema(conn)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(result_items)").fetchall()]

    for column in (
        "sold_price",
        "sold_price_display",
        "sold_price_source",
        "sold_at",
        "status_checked_at",
        "status_source",
        "status_raw_code",
        "delisted_streak",
    ):
        assert column in columns


def test_apply_listing_status_writes_event_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("1001")

    info = parse_listing_status(_detail({"itemStatus": 1, "soldCnt": 1, "soldPrice": "3500"}))
    outcome = apply_listing_status(
        "demo_full_data.jsonl",
        "1001",
        info,
        run_id="run-1",
        last_listing_price=4200.0,
        detected_at="2026-01-02T10:00:00",
    )
    assert outcome["status"] == LISTING_STATUS_SOLD
    assert outcome["sold_price"] == 3500.0
    assert outcome["sold_price_source"] == "sold_price_api"

    events = load_item_status_events("demo_full_data.jsonl")
    assert len(events) == 1
    assert events[0]["to_status"] == LISTING_STATUS_SOLD
    assert events[0]["price"] == 3500.0
    assert events[0]["detected_at"] == "2026-01-02T10:00:00"

    # 同一状态重复复核不应产生重复事件
    apply_listing_status(
        "demo_full_data.jsonl",
        "1001",
        info,
        run_id="run-2",
        last_listing_price=4200.0,
        detected_at="2026-01-03T10:00:00",
    )
    assert len(load_item_status_events("demo_full_data.jsonl")) == 1


def test_apply_listing_status_keeps_first_sold_at(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("1002")

    info = parse_listing_status(_detail({"itemStatus": 1, "soldCnt": 1, "soldPrice": "3500"}))
    apply_listing_status(
        "demo_full_data.jsonl",
        "1002",
        info,
        detected_at="2026-01-02T10:00:00",
    )
    apply_listing_status(
        "demo_full_data.jsonl",
        "1002",
        info,
        detected_at="2026-01-05T10:00:00",
    )

    with sqlite_connection() as conn:
        row = conn.execute(
            """
            SELECT sold_at, sold_price FROM result_items
            WHERE result_filename = ? AND item_id = ?
            """,
            ("demo_full_data.jsonl", "1002"),
        ).fetchone()
    assert row["sold_at"] == "2026-01-02T10:00:00"
    assert row["sold_price"] == 3500.0


def test_delisted_confirm_requires_repeated_detection(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITEM_STATUS_DELISTED_CONFIRM", "2")
    _save_item("1003")

    info = parse_listing_status(_detail({"itemStatus": -98}))
    first = apply_listing_status(
        "demo_full_data.jsonl", "1003", info, detected_at="2026-01-02T10:00:00"
    )
    assert first["pending_confirmation"] is True
    assert first["updated"] is False

    second = apply_listing_status(
        "demo_full_data.jsonl", "1003", info, detected_at="2026-01-03T10:00:00"
    )
    assert second["pending_confirmation"] is False
    assert second["status"] == LISTING_STATUS_DELISTED

    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT listing_status FROM result_items WHERE item_id = ?",
            ("1003",),
        ).fetchone()
    assert row["listing_status"] == LISTING_STATUS_DELISTED


# ── 复核队列 ────────────────────────────────────────────────────────────


def test_recheck_queue_only_picks_unfinished_records(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("2001")
    _save_item("2002")
    _save_item("2003")

    sold_info = parse_listing_status(_detail({"itemStatus": 1, "soldCnt": 1, "soldPrice": "100"}))
    apply_listing_status("demo_full_data.jsonl", "2002", sold_info)

    queue = load_status_recheck_queue("demo_full_data.jsonl", limit=20, interval_hours=24)
    item_ids = [entry["item_id"] for entry in queue]
    assert "2001" in item_ids
    assert "2003" in item_ids
    assert "2002" not in item_ids


def test_recheck_queue_respects_limit_and_interval(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for index in range(3):
        _save_item(f"300{index}")

    limited = load_status_recheck_queue("demo_full_data.jsonl", limit=2, interval_hours=24)
    assert len(limited) == 2

    with sqlite_connection() as conn:
        conn.execute(
            """
            UPDATE result_items SET status_checked_at = ?
            WHERE result_filename = ? AND item_id = ?
            """,
            ("2026-11-16T00:00:00", "demo_full_data.jsonl", "3000"),
        )
        conn.commit()

    recent = load_status_recheck_queue("demo_full_data.jsonl", limit=20, interval_hours=24)
    assert all(entry["item_id"] != "3000" for entry in recent)


# ── 巡检编排 ────────────────────────────────────────────────────────────


def test_recheck_queue_prioritises_items_missing_from_this_run(tmp_path, monkeypatch):
    """本次搜索没出现的商品更可能已经成交，应该优先复核。"""
    monkeypatch.chdir(tmp_path)
    _save_item("6001")
    _save_item("6002")

    queue = load_status_recheck_queue(
        "demo_full_data.jsonl",
        limit=20,
        interval_hours=24,
        seen_item_ids={"6002"},
    )
    assert [entry["item_id"] for entry in queue] == ["6001", "6002"]


def test_run_status_recheck_updates_records_and_survives_failures(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("4001")
    _save_item("4002")

    calls: list[str] = []

    async def fake_fetch_detail(context, url):
        calls.append(url)
        if "4002" in url:
            raise RuntimeError("网络异常")
        return _detail({"itemStatus": 1, "soldCnt": 1, "soldPrice": "3500"})

    async def no_sleep(min_seconds, max_seconds):
        return None

    summary = asyncio.run(
        run_status_recheck(
            context=None,
            page=None,
            keyword="demo",
            result_filename="demo_full_data.jsonl",
            run_id="run-1",
            fetch_detail=fake_fetch_detail,
            sleep=no_sleep,
            limit=20,
            interval_hours=24,
            max_seconds=60,
            probe_mode="navigate",
        )
    )

    assert summary["checked"] == 1
    assert summary["sold"] == 1
    assert summary["failed"] == 1
    assert len(calls) == 2

    with sqlite_connection() as conn:
        sold_row = conn.execute(
            "SELECT listing_status, sold_price FROM result_items WHERE item_id = ?",
            ("4001",),
        ).fetchone()
        failed_row = conn.execute(
            "SELECT listing_status, status_checked_at, status_source FROM result_items WHERE item_id = ?",
            ("4002",),
        ).fetchone()

    assert sold_row["listing_status"] == LISTING_STATUS_SOLD
    assert sold_row["sold_price"] == 3500.0
    # 探测失败只能更新时间戳，绝不能把商品判成已下架
    assert failed_row["listing_status"] == "unknown"
    assert failed_row["status_source"] == "error"
    assert failed_row["status_checked_at"]


# ── 洞察聚合 ────────────────────────────────────────────────────────────


def test_build_sold_status_insights(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("5001")
    _save_item("5002")

    sold_info = parse_listing_status(_detail({"itemStatus": 1, "soldCnt": 1, "soldPrice": "3500"}))
    apply_listing_status(
        "demo_full_data.jsonl", "5001", sold_info, detected_at="2026-01-02T10:00:00"
    )
    fallback_info = parse_listing_status(_detail({"itemStatus": 1, "soldCnt": 1}))
    apply_listing_status(
        "demo_full_data.jsonl",
        "5002",
        fallback_info,
        last_listing_price=4200.0,
        detected_at="2026-01-03T10:00:00",
    )

    insights = build_sold_status_insights("demo_full_data.jsonl")
    assert insights["sold_summary"]["sold_count"] == 2
    assert insights["sold_summary"]["estimated_count"] == 1
    assert insights["sold_summary"]["last_sold_at"] == "2026-01-03T10:00:00"

    days = [point["day"] for point in insights["sold_trend"]]
    assert days == ["2026-01-02", "2026-01-03"]
    assert insights["sold_trend"][0]["avg_price"] == 3500.0
    assert insights["sold_trend"][1]["avg_price"] == 4200.0
    assert insights["sold_trend"][1]["estimated_count"] == 1

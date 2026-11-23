"""商品在售状态的人工标记（标记已售 / 撤销）测试。"""

import asyncio
import json

import pytest

from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.services.item_status_manual_service import (
    clear_manual_listing_status,
    mark_item_delisted_manually,
    mark_item_sold_manually,
)
from src.services.item_status_service import (
    LISTING_STATUS_DELISTED,
    LISTING_STATUS_SOLD,
    apply_listing_status,
    build_sold_status_insights,
    parse_listing_status,
)
from src.services.result_storage_service import (
    load_item_listing_snapshot,
    load_item_status_events,
    load_status_recheck_queue,
    save_result_record,
)


TEST_CODE_MAP = {"1": "active", "5": "sold", "7": "sold", "-98": "delisted"}


@pytest.fixture(autouse=True)
def _status_code_map(monkeypatch):
    monkeypatch.setenv("ITEM_STATUS_CODE_MAP", json.dumps(TEST_CODE_MAP))


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


def _row(item_id: str) -> dict:
    with sqlite_connection() as conn:
        row = conn.execute(
            """
            SELECT listing_status, sold_price, sold_price_display, sold_price_source,
                   sold_at, status_checked_at, status_source, status_raw_code
            FROM result_items
            WHERE item_id = ?
            """,
            (item_id,),
        ).fetchone()
    return dict(row) if row is not None else {}


def _detail(item_do=None, ret="SUCCESS::调用成功"):
    payload = {"ret": [ret], "data": {}}
    if item_do is not None:
        payload["data"]["itemDO"] = item_do
    return payload


# ── 标记已售 ──────────────────────────────────────────────────────────


def test_mark_item_sold_manually_uses_listing_price_as_estimate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("1001", price="¥950")

    result = mark_item_sold_manually(
        "demo_full_data.jsonl", "1001", detected_at="2026-01-02T10:00:00"
    )

    assert result["updated"] is True
    assert result["changed"] is True
    assert result["listing_status"] == LISTING_STATUS_SOLD

    row = _row("1001")
    assert row["listing_status"] == LISTING_STATUS_SOLD
    # 人工判断没有平台状态码，不能留上一轮的原始码
    assert row["status_raw_code"] is None
    assert row["status_source"] == "manual"
    assert row["sold_price"] == 950.0
    assert row["sold_price_display"] == "¥950"
    # 成交价是**估算**（挂牌价回退口径），不能冒充平台成交价
    assert row["sold_price_source"] == "listing_fallback"
    assert row["sold_at"] == "2026-01-02T10:00:00"
    assert row["status_checked_at"] == "2026-01-02T10:00:00"


def test_mark_item_sold_manually_writes_sold_event(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("1001", price="¥950")

    mark_item_sold_manually(
        "demo_full_data.jsonl", "1001", detected_at="2026-01-02T10:00:00"
    )

    events = load_item_status_events("demo_full_data.jsonl")
    assert len(events) == 1
    assert events[0]["from_status"] == "unknown"
    assert events[0]["to_status"] == LISTING_STATUS_SOLD
    assert events[0]["price"] == 950.0
    assert events[0]["price_source"] == "listing_fallback"
    assert events[0]["run_id"] == "manual"


def test_mark_item_sold_manually_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("1001", price="¥950")

    mark_item_sold_manually("demo_full_data.jsonl", "1001")
    second = mark_item_sold_manually("demo_full_data.jsonl", "1001")

    assert second["changed"] is False
    # 重复标记不产生重复事件
    assert len(load_item_status_events("demo_full_data.jsonl")) == 1


def test_mark_item_sold_manually_keeps_platform_sold_price(tmp_path, monkeypatch):
    """已经是平台探测出来的已售时，人工标记不能覆盖平台成交价。"""
    monkeypatch.chdir(tmp_path)
    _save_item("1001", price="¥950")

    apply_listing_status(
        "demo_full_data.jsonl",
        "1001",
        parse_listing_status(_detail({"itemStatus": 7, "soldCnt": 1, "soldPrice": "900"})),
        detected_at="2026-01-02T10:00:00",
    )

    result = mark_item_sold_manually("demo_full_data.jsonl", "1001")

    assert result["changed"] is False
    row = _row("1001")
    assert row["sold_price"] == 900.0
    assert row["sold_price_source"] == "sold_price_api"
    assert row["status_source"] == "manual"


def test_mark_item_sold_manually_reports_missing_item(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = mark_item_sold_manually("demo_full_data.jsonl", "404")

    assert result["updated"] is False
    assert result["changed"] is False
    assert result["reason"] == "not_found"


# ── 撤销 ─────────────────────────────────────────────────────────────


def test_clear_manual_listing_status_resets_state_and_events(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("1001", price="¥950")
    _save_item("1002", price="¥1200")

    mark_item_sold_manually("demo_full_data.jsonl", "1001")
    apply_listing_status(
        "demo_full_data.jsonl",
        "1002",
        parse_listing_status(_detail({"itemStatus": 7, "soldCnt": 1, "soldPrice": "1100"})),
        detected_at="2026-01-02T10:00:00",
    )

    result = clear_manual_listing_status("demo_full_data.jsonl", "1001")

    assert result["updated"] is True
    assert result["changed"] is True
    assert result["listing_status"] == "unknown"

    row = _row("1001")
    assert row["listing_status"] == "unknown"
    assert row["sold_price"] is None
    assert row["sold_price_display"] == ""
    assert row["sold_price_source"] == ""
    assert row["sold_at"] is None
    assert row["status_source"] is None

    # 只删这一条记录的成交事件，另一条不受影响
    events = load_item_status_events("demo_full_data.jsonl")
    assert [event["item_id"] for event in events] == ["1002"]


def test_clear_manual_listing_status_on_non_sold_item(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("1001")

    result = clear_manual_listing_status("demo_full_data.jsonl", "1001")

    assert result["updated"] is False
    assert result["changed"] is False
    assert result["reason"] == "not_marked"


def test_clear_manual_listing_status_reports_missing_item(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = clear_manual_listing_status("demo_full_data.jsonl", "404")

    assert result["updated"] is False
    assert result["reason"] == "not_found"


# ── 与自动巡检的关系 ──────────────────────────────────────────────────


def test_manual_sold_is_excluded_from_recheck_queue(tmp_path, monkeypatch):
    """人工标记的已售不能被平台探测结果覆盖。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITEM_STATUS_RECHECK_TERMINAL", "true")
    _save_item("1001", price="¥950")

    mark_item_sold_manually("demo_full_data.jsonl", "1001")

    queue = load_status_recheck_queue("demo_full_data.jsonl", limit=20)
    assert "1001" not in {entry["item_id"] for entry in queue}

    # 撤销后保护自然解除，记录重新进入可复核队列
    clear_manual_listing_status("demo_full_data.jsonl", "1001")
    queue = load_status_recheck_queue("demo_full_data.jsonl", limit=20)
    assert "1001" in {entry["item_id"] for entry in queue}


def test_platform_sold_still_enters_recheck_queue_when_terminal_enabled(tmp_path, monkeypatch):
    """平台探测出来的已售仍可参与终态复核（只有人工标记被排除）。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITEM_STATUS_RECHECK_TERMINAL", "true")
    _save_item("1002", price="¥1200")

    apply_listing_status(
        "demo_full_data.jsonl",
        "1002",
        parse_listing_status(_detail({"itemStatus": 7, "soldCnt": 1, "soldPrice": "1100"})),
        detected_at="2026-01-02T10:00:00",
    )

    queue = load_status_recheck_queue("demo_full_data.jsonl", limit=20)
    assert "1002" in {entry["item_id"] for entry in queue}


# ── 成交价曲线 ────────────────────────────────────────────────────────


def test_manual_sold_shows_up_in_sold_insights(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("1001", price="¥950")

    mark_item_sold_manually(
        "demo_full_data.jsonl", "1001", detected_at="2026-01-02T10:00:00"
    )

    insights = build_sold_status_insights("demo_full_data.jsonl")
    assert insights["sold_summary"]["sold_count"] == 1
    assert insights["sold_summary"]["estimated_count"] == 1
    assert insights["sold_trend"][0]["day"] == "2026-01-02"
    assert insights["sold_trend"][0]["sold_count"] == 1
    assert insights["sold_trend"][0]["avg_price"] == 950.0
    assert insights["sold_trend"][0]["estimated_count"] == 1

    # 撤销后这笔记账要一起消失，否则成交价曲线会重复计数
    clear_manual_listing_status("demo_full_data.jsonl", "1001")
    insights = build_sold_status_insights("demo_full_data.jsonl")
    assert insights["sold_summary"]["sold_count"] == 0
    assert insights["sold_trend"] == []


# ── 标记已下架 ────────────────────────────────────────────────────────


def test_mark_item_delisted_manually_sets_status_without_sold_price(tmp_path, monkeypatch):
    """已下架 = 没卖出去：不应产生任何成交价。"""
    monkeypatch.chdir(tmp_path)
    _save_item("1001", price="¥950")

    result = mark_item_delisted_manually(
        "demo_full_data.jsonl", "1001", detected_at="2026-01-02T10:00:00"
    )

    assert result["updated"] is True
    assert result["changed"] is True
    assert result["listing_status"] == LISTING_STATUS_DELISTED
    assert result["sold_price"] is None

    row = _row("1001")
    assert row["listing_status"] == LISTING_STATUS_DELISTED
    assert row["sold_price"] is None
    assert row["sold_at"] is None
    # 人工判断没有平台状态码，不能留上一轮的原始码
    assert row["status_raw_code"] is None
    assert row["status_source"] == "manual"
    assert row["status_checked_at"] == "2026-01-02T10:00:00"

    # 对外口径：没有成交价，也没有成交价来源
    snapshot = load_item_listing_snapshot("demo_full_data.jsonl", "1001")
    assert snapshot["sold_price_display"] == ""
    assert snapshot["sold_price_source"] == ""


def test_mark_item_delisted_manually_clears_previous_sold_record(tmp_path, monkeypatch):
    """从「已售」改成「已下架」：成交价与成交事件都要清掉，不留在成交价曲线里。"""
    monkeypatch.chdir(tmp_path)
    _save_item("1001", price="¥950")

    mark_item_sold_manually("demo_full_data.jsonl", "1001")
    assert build_sold_status_insights("demo_full_data.jsonl")["sold_summary"]["sold_count"] == 1

    result = mark_item_delisted_manually("demo_full_data.jsonl", "1001")

    assert result["changed"] is True
    assert result["listing_status"] == LISTING_STATUS_DELISTED

    row = _row("1001")
    assert row["sold_price"] is None
    assert row["sold_at"] is None
    assert row["sold_price_source"] == ""

    insights = build_sold_status_insights("demo_full_data.jsonl")
    assert insights["sold_summary"]["sold_count"] == 0
    assert insights["sold_trend"] == []

    # 只剩下「已下架」这一条状态事件
    events = load_item_status_events("demo_full_data.jsonl")
    assert [event["to_status"] for event in events] == [LISTING_STATUS_DELISTED]


def test_mark_item_delisted_manually_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("1001", price="¥950")

    mark_item_delisted_manually("demo_full_data.jsonl", "1001")
    second = mark_item_delisted_manually("demo_full_data.jsonl", "1001")

    assert second["changed"] is False
    assert second["listing_status"] == LISTING_STATUS_DELISTED
    assert len(load_item_status_events("demo_full_data.jsonl")) == 1


def test_mark_item_delisted_manually_reports_missing_item(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = mark_item_delisted_manually("demo_full_data.jsonl", "404")

    assert result["updated"] is False
    assert result["changed"] is False
    assert result["reason"] == "not_found"


def test_clear_manual_listing_status_clears_delisted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("1001", price="¥950")

    mark_item_delisted_manually("demo_full_data.jsonl", "1001")
    result = clear_manual_listing_status("demo_full_data.jsonl", "1001")

    assert result["updated"] is True
    assert result["changed"] is True
    assert result["listing_status"] == "unknown"

    row = _row("1001")
    assert row["listing_status"] == "unknown"
    assert row["status_source"] is None
    assert load_item_status_events("demo_full_data.jsonl") == []


def test_manual_delisted_is_excluded_from_recheck_queue(tmp_path, monkeypatch):
    """人工标记的已下架同样不能被平台探测结果覆盖。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITEM_STATUS_RECHECK_TERMINAL", "true")
    _save_item("1001", price="¥950")

    mark_item_delisted_manually("demo_full_data.jsonl", "1001")

    queue = load_status_recheck_queue("demo_full_data.jsonl", limit=20)
    assert "1001" not in {entry["item_id"] for entry in queue}

    # 撤销后保护自然解除，记录重新进入可复核队列
    clear_manual_listing_status("demo_full_data.jsonl", "1001")
    queue = load_status_recheck_queue("demo_full_data.jsonl", limit=20)
    assert "1001" in {entry["item_id"] for entry in queue}

"""商品在售状态（已售 / 已下架）判定与成交价记录测试。"""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from src.infrastructure.persistence.sqlite_connection import (
    init_schema,
    sqlite_connection,
)
from src.services import item_status_service
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
    delete_item_status_events,
    load_item_status_events,
    load_status_recheck_queue,
    reset_listing_status,
    save_result_record,
)


# 测试用的码表：真实码值需要用线上响应样本校准，这里用固定映射覆盖行为。
TEST_CODE_MAP = {"1": "active", "5": "sold", "7": "sold", "-98": "delisted"}


@pytest.fixture(autouse=True)
def _status_code_map(monkeypatch):
    """让绝大多数用例都能命中码表；需要验证「未收录」的用例用别的码值。"""
    monkeypatch.setenv("ITEM_STATUS_CODE_MAP", json.dumps(TEST_CODE_MAP))


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


def _row(item_id: str) -> dict:
    with sqlite_connection() as conn:
        row = conn.execute(
            """
            SELECT listing_status, sold_price, sold_price_display, sold_price_source,
                   sold_at, status_checked_at, status_source, status_raw_code,
                   delisted_streak
            FROM result_items
            WHERE item_id = ?
            """,
            (item_id,),
        ).fetchone()
    return dict(row) if row is not None else {}


# ── 判定：探测失败必须不改业务状态 ──────────────────────────────────────


def test_session_expired_is_probe_failure_not_delisted():
    """会话过期是探测失败，绝不能判成「已下架」。"""
    info = parse_listing_status(_detail(None, ret="FAIL_SYS_SESSION_EXPIRED::Session过期"))
    assert info.probe_ok is False
    assert info.status != LISTING_STATUS_DELISTED
    assert info.source == "error"
    assert "FAIL_SYS_SESSION_EXPIRED" in info.note


def test_risk_control_ret_is_probe_failure():
    info = parse_listing_status(_detail(None, ret="FAIL_SYS_USER_VALIDATE::需要验证"))
    assert info.probe_ok is False
    assert info.status == LISTING_STATUS_UNKNOWN


def test_missing_ret_is_probe_failure():
    info = parse_listing_status({"data": {"itemDO": {"itemStatus": 1}}})
    assert info.probe_ok is False
    assert info.source == "error"


def test_item_do_missing_with_success_is_delisted():
    """接口明确成功但商品体为空 = 商品已被删除/下架。"""
    info = parse_listing_status(_detail(None))
    assert info.probe_ok is True
    assert info.status == LISTING_STATUS_DELISTED
    assert info.source == "missing_detail"


def test_empty_item_do_with_success_is_delisted():
    info = parse_listing_status(_detail({}))
    assert info.probe_ok is True
    assert info.status == LISTING_STATUS_DELISTED


def test_item_id_mismatch_is_probe_failure():
    """接口返回了别的商品 → 探测失败，不能拿它的状态覆盖目标商品。"""
    info = parse_listing_status(
        _detail({"itemId": "9999", "itemStatus": 1}), expected_item_id="1001"
    )
    assert info.probe_ok is False
    assert info.status != LISTING_STATUS_ACTIVE
    assert "itemId" in info.note


def test_item_id_match_is_accepted():
    info = parse_listing_status(
        _detail({"itemId": "1001", "itemStatus": 1}), expected_item_id="1001"
    )
    assert info.probe_ok is True
    assert info.status == LISTING_STATUS_ACTIVE


def test_item_id_absent_in_response_is_not_treated_as_mismatch():
    """响应里没有 itemId 时无法校验，不应误判成探测失败。"""
    info = parse_listing_status(_detail({"itemStatus": 1}), expected_item_id="1001")
    assert info.probe_ok is True
    assert info.status == LISTING_STATUS_ACTIVE


# ── 判定：只认 itemStatus 码表 ──────────────────────────────────────────


def test_code_map_hit_active():
    info = parse_listing_status(_detail({"itemStatus": 1, "soldCnt": 0}))
    assert info.status == LISTING_STATUS_ACTIVE
    assert info.raw_code == 1
    assert info.sold_cnt == 0


def test_code_map_hit_sold():
    info = parse_listing_status(_detail({"itemStatus": 7}))
    assert info.status == LISTING_STATUS_SOLD
    assert info.raw_code == 7


def test_code_map_hit_delisted():
    info = parse_listing_status(_detail({"itemStatus": -98}))
    assert info.status == LISTING_STATUS_DELISTED
    assert info.raw_code == -98


def test_unknown_code_is_unknown():
    """没收录的码一律判未知，绝不猜。"""
    info = parse_listing_status(_detail({"itemStatus": 4242}))
    assert info.status == LISTING_STATUS_UNKNOWN
    assert info.status not in (
        LISTING_STATUS_ACTIVE,
        LISTING_STATUS_SOLD,
        LISTING_STATUS_DELISTED,
    )
    assert info.raw_code == 4242


def test_missing_status_field_is_unknown():
    info = parse_listing_status(_detail({"title": "没有状态字段的商品"}))
    assert info.status == LISTING_STATUS_UNKNOWN
    assert info.raw_code is None


def test_env_code_map_overrides_builtin_map(monkeypatch):
    """环境变量覆盖优先级高于内置码表。"""
    monkeypatch.setitem(item_status_service.DEFAULT_STATUS_CODE_MAP, 42, "sold")
    monkeypatch.setenv("ITEM_STATUS_CODE_MAP", json.dumps({"42": "active"}))
    info = parse_listing_status(_detail({"itemStatus": 42}))
    assert info.status == LISTING_STATUS_ACTIVE


def test_builtin_map_used_without_env_override(monkeypatch):
    monkeypatch.delenv("ITEM_STATUS_CODE_MAP", raising=False)
    monkeypatch.setitem(item_status_service.DEFAULT_STATUS_CODE_MAP, 42, "sold")
    info = parse_listing_status(_detail({"itemStatus": 42}))
    assert info.status == LISTING_STATUS_SOLD


def test_builtin_code_map_calibrated_zero_is_active(monkeypatch):
    """内置码表已用线上真实样本校准：itemStatus=0 = 在售。

    校准依据：99 条误判记录里 92 条原始码是 0，交叉验证「仍在最近搜索结果里」
    有 40 条，且用户逐条确认这些商品实际仍在售。
    """
    monkeypatch.delenv("ITEM_STATUS_CODE_MAP", raising=False)
    info = parse_listing_status(
        _detail({"itemStatus": 0, "soldCnt": 0, "soldPrice": "4200"})
    )
    assert info.status == LISTING_STATUS_ACTIVE
    # 价格字段照样不参与判定
    assert info.sold_price == 4200.0


def test_builtin_code_map_leaves_unconfirmed_codes_unknown(monkeypatch):
    """码 1 / -2 的样本量太小（4 条 / 3 条），故意不收录 → 判未知，绝不猜。"""
    monkeypatch.delenv("ITEM_STATUS_CODE_MAP", raising=False)
    for code in (1, -2):
        info = parse_listing_status(_detail({"itemStatus": code, "soldPrice": "100"}))
        assert info.status == LISTING_STATUS_UNKNOWN
        assert info.status not in (
            LISTING_STATUS_ACTIVE,
            LISTING_STATUS_SOLD,
            LISTING_STATUS_DELISTED,
        )


# ── 判定回归：soldPrice / soldCnt 不再影响状态 ─────────────────────────


def test_sold_price_does_not_override_active():
    """RC-1 回归：soldPrice 在在售商品上恒有值，不能据此判已售。"""
    info = parse_listing_status(
        _detail({"itemStatus": 1, "soldCnt": 0, "soldPrice": "3500"})
    )
    assert info.status == LISTING_STATUS_ACTIVE
    assert info.sold_price == 3500.0
    assert info.sold_price_display == "3500"


def test_sold_count_does_not_trigger_sold():
    """RC-1 回归：soldCnt > 0 不再单独触发「已售」。"""
    info = parse_listing_status(_detail({"itemStatus": 1, "soldCnt": 5}))
    assert info.status == LISTING_STATUS_ACTIVE
    assert info.sold_cnt == 5


def test_sold_precedence_switch_is_deprecated_noop(monkeypatch):
    """ITEM_STATUS_SOLD_PRECEDENCE 已废弃，打开也不再影响判定。"""
    monkeypatch.setenv("ITEM_STATUS_SOLD_PRECEDENCE", "true")
    info = parse_listing_status(
        _detail({"itemStatus": 1, "soldCnt": 3, "soldPrice": "3500"})
    )
    assert info.status == LISTING_STATUS_ACTIVE


# ── 成交价口径 ──────────────────────────────────────────────────────────


def test_resolve_sold_price_prefers_platform_price():
    info = parse_listing_status(_detail({"itemStatus": 7, "soldCnt": 1, "soldPrice": "3500"}))
    price, display, source = resolve_sold_price(info, 9999.0)
    assert price == 3500.0
    assert display == "¥3500"
    assert source == "sold_price_api"


def test_resolve_sold_price_falls_back_to_last_listing_price():
    info = parse_listing_status(_detail({"itemStatus": 7, "soldCnt": 1}))
    price, display, source = resolve_sold_price(info, 4200.0)
    assert price == 4200.0
    assert display == "¥4200.0"
    assert source == "listing_fallback"


def test_resolve_sold_price_without_any_price():
    info = parse_listing_status(_detail({"itemStatus": 7, "soldCnt": 1}))
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

    info = parse_listing_status(_detail({"itemStatus": 7, "soldCnt": 1, "soldPrice": "3500"}))
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

    info = parse_listing_status(_detail({"itemStatus": 7, "soldCnt": 1, "soldPrice": "3500"}))
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


def test_apply_listing_status_probe_failure_keeps_business_state(tmp_path, monkeypatch):
    """探测失败时不得改写 listing_status / 成交价，也不得写事件。"""
    monkeypatch.chdir(tmp_path)
    _save_item("1003")

    sold_info = parse_listing_status(_detail({"itemStatus": 7, "soldPrice": "3500"}))
    apply_listing_status(
        "demo_full_data.jsonl", "1003", sold_info, detected_at="2026-01-02T10:00:00"
    )

    failed_info = parse_listing_status(
        _detail(None, ret="FAIL_SYS_SESSION_EXPIRED::Session过期")
    )
    outcome = apply_listing_status(
        "demo_full_data.jsonl", "1003", failed_info, detected_at="2026-01-03T10:00:00"
    )
    assert outcome["probe_failed"] is True
    assert outcome["updated"] is False

    row = _row("1003")
    assert row["listing_status"] == LISTING_STATUS_SOLD
    assert row["sold_price"] == 3500.0
    assert row["sold_at"] == "2026-01-02T10:00:00"
    assert row["status_source"] == "error"
    assert row["status_checked_at"] == "2026-01-03T10:00:00"
    # 原始码保留，便于事后校准码表
    assert row["status_raw_code"] == 7

    events = load_item_status_events("demo_full_data.jsonl")
    assert [event["to_status"] for event in events] == [LISTING_STATUS_SOLD]


def test_delisted_confirm_requires_repeated_detection(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITEM_STATUS_DELISTED_CONFIRM", "2")
    _save_item("1004")

    info = parse_listing_status(_detail({"itemStatus": -98}))
    first = apply_listing_status(
        "demo_full_data.jsonl", "1004", info, detected_at="2026-01-02T10:00:00"
    )
    assert first["pending_confirmation"] is True
    assert first["updated"] is False

    second = apply_listing_status(
        "demo_full_data.jsonl", "1004", info, detected_at="2026-01-03T10:00:00"
    )
    assert second["pending_confirmation"] is False
    assert second["status"] == LISTING_STATUS_DELISTED

    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT listing_status FROM result_items WHERE item_id = ?",
            ("1004",),
        ).fetchone()
    assert row["listing_status"] == LISTING_STATUS_DELISTED


# ── 存量数据清理 ────────────────────────────────────────────────────────


def test_reset_listing_status_clears_sold_state_and_events(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("7001")
    _save_item("7002")

    sold_info = parse_listing_status(_detail({"itemStatus": 7, "soldPrice": "3500"}))
    for item_id in ("7001", "7002"):
        apply_listing_status(
            "demo_full_data.jsonl", item_id, sold_info, detected_at="2026-01-02T10:00:00"
        )

    result = reset_listing_status("demo_full_data.jsonl")
    assert result["reset_items"] == 2
    assert result["deleted_events"] == 2

    row = _row("7001")
    assert row["listing_status"] == LISTING_STATUS_UNKNOWN
    assert row["sold_price"] is None
    assert row["sold_price_display"] == ""
    assert row["sold_price_source"] == ""
    assert row["sold_at"] is None
    assert row["delisted_streak"] == 0
    assert row["status_checked_at"] == ""
    assert row["status_source"] is None

    assert load_item_status_events("demo_full_data.jsonl") == []


def test_reset_listing_status_respects_item_ids_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("7101")
    _save_item("7102")

    sold_info = parse_listing_status(_detail({"itemStatus": 7, "soldPrice": "3500"}))
    for item_id in ("7101", "7102"):
        apply_listing_status("demo_full_data.jsonl", item_id, sold_info)

    result = reset_listing_status("demo_full_data.jsonl", item_ids=["7101"])
    assert result["reset_items"] == 1
    assert result["deleted_events"] == 1

    assert _row("7101")["listing_status"] == LISTING_STATUS_UNKNOWN
    assert _row("7102")["listing_status"] == LISTING_STATUS_SOLD


def test_reset_listing_status_respects_status_raw_codes_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("7201")
    _save_item("7202")

    sold_info = parse_listing_status(_detail({"itemStatus": 7, "soldPrice": "3500"}))
    for item_id in ("7201", "7202"):
        apply_listing_status("demo_full_data.jsonl", item_id, sold_info)

    result = reset_listing_status("demo_full_data.jsonl", status_raw_codes=[7])
    assert result["reset_items"] == 2

    result = reset_listing_status("demo_full_data.jsonl", status_raw_codes=[999])
    assert result["reset_items"] == 0


def test_reset_with_raw_code_filter_only_deletes_its_own_events(tmp_path, monkeypatch):
    """按原始码收敛时，只能删掉这批记录的事件，别人的历史事件要留着。"""
    monkeypatch.chdir(tmp_path)
    _save_item("7251")
    _save_item("7252")

    apply_listing_status(
        "demo_full_data.jsonl",
        "7251",
        parse_listing_status(_detail({"itemStatus": 7, "soldPrice": "3500"})),
    )
    apply_listing_status(
        "demo_full_data.jsonl",
        "7252",
        parse_listing_status(_detail({"itemStatus": 5, "soldPrice": "4200"})),
    )
    assert len(load_item_status_events("demo_full_data.jsonl")) == 2

    result = reset_listing_status("demo_full_data.jsonl", status_raw_codes=[7])
    assert result["reset_items"] == 1
    assert result["deleted_events"] == 1

    assert _row("7251")["listing_status"] == LISTING_STATUS_UNKNOWN
    assert _row("7252")["listing_status"] == LISTING_STATUS_SOLD

    events = load_item_status_events("demo_full_data.jsonl")
    assert [event["item_id"] for event in events] == ["7252"]


def test_delete_item_status_events_only_removes_target_statuses(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("7301")

    sold_info = parse_listing_status(_detail({"itemStatus": 7, "soldPrice": "3500"}))
    apply_listing_status("demo_full_data.jsonl", "7301", sold_info)
    assert len(load_item_status_events("demo_full_data.jsonl")) == 1

    assert delete_item_status_events("demo_full_data.jsonl", to_statuses=["delisted"]) == 0
    assert len(load_item_status_events("demo_full_data.jsonl")) == 1

    assert delete_item_status_events("demo_full_data.jsonl", to_statuses=["sold"]) == 1
    assert load_item_status_events("demo_full_data.jsonl") == []


def test_reset_records_are_picked_first_by_recheck_queue(tmp_path, monkeypatch):
    """重置后 status_checked_at 为空 → 下一轮复核优先挑中它们。"""
    monkeypatch.chdir(tmp_path)
    _save_item("7401")
    _save_item("7402")

    sold_info = parse_listing_status(_detail({"itemStatus": 7, "soldPrice": "3500"}))
    apply_listing_status("demo_full_data.jsonl", "7401", sold_info)
    # 7402 刚复核过，24 小时间隔内不会进队列
    apply_listing_status(
        "demo_full_data.jsonl",
        "7402",
        parse_listing_status(_detail({"itemStatus": 4242})),
        detected_at=datetime.now().isoformat(),
    )

    queue = load_status_recheck_queue("demo_full_data.jsonl", limit=20, interval_hours=24)
    assert "7401" not in [entry["item_id"] for entry in queue]

    reset_listing_status("demo_full_data.jsonl")

    queue = load_status_recheck_queue("demo_full_data.jsonl", limit=20, interval_hours=24)
    assert [entry["item_id"] for entry in queue] == ["7401"]


# ── 复核队列 ────────────────────────────────────────────────────────────


def test_recheck_queue_only_picks_unfinished_records(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("2001")
    _save_item("2002")
    _save_item("2003")

    sold_info = parse_listing_status(_detail({"itemStatus": 7, "soldPrice": "100"}))
    apply_listing_status("demo_full_data.jsonl", "2002", sold_info)

    queue = load_status_recheck_queue("demo_full_data.jsonl", limit=20, interval_hours=24)
    item_ids = [entry["item_id"] for entry in queue]
    assert "2001" in item_ids
    assert "2003" in item_ids
    assert "2002" not in item_ids


def test_recheck_queue_can_include_terminal_statuses_when_enabled(tmp_path, monkeypatch):
    """ITEM_STATUS_RECHECK_TERMINAL 打开后，已售记录也参与复核（徽章可自愈）。"""
    monkeypatch.chdir(tmp_path)
    _save_item("2101")

    sold_info = parse_listing_status(_detail({"itemStatus": 7, "soldPrice": "100"}))
    apply_listing_status("demo_full_data.jsonl", "2101", sold_info)

    # 默认：已售是终态，不进复核队列
    queue = load_status_recheck_queue("demo_full_data.jsonl", limit=20, interval_hours=0)
    assert "2101" not in [entry["item_id"] for entry in queue]

    monkeypatch.setenv("ITEM_STATUS_RECHECK_TERMINAL", "true")
    queue = load_status_recheck_queue("demo_full_data.jsonl", limit=20, interval_hours=0)
    assert "2101" in [entry["item_id"] for entry in queue]


def test_recheck_queue_respects_limit_and_interval(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for index in range(3):
        _save_item(f"300{index}")

    limited = load_status_recheck_queue("demo_full_data.jsonl", limit=2, interval_hours=24)
    assert len(limited) == 2

    # 用相对时间，避免测试随运行日期漂移
    checked_at = datetime.now().isoformat()
    with sqlite_connection() as conn:
        conn.execute(
            """
            UPDATE result_items SET status_checked_at = ?
            WHERE result_filename = ? AND item_id = ?
            """,
            (checked_at, "demo_full_data.jsonl", "3000"),
        )
        conn.commit()

    recent = load_status_recheck_queue("demo_full_data.jsonl", limit=20, interval_hours=24)
    assert all(entry["item_id"] != "3000" for entry in recent)

    stale_at = (datetime.now() - timedelta(hours=48)).isoformat()
    with sqlite_connection() as conn:
        conn.execute(
            """
            UPDATE result_items SET status_checked_at = ?
            WHERE result_filename = ? AND item_id = ?
            """,
            (stale_at, "demo_full_data.jsonl", "3001"),
        )
        conn.commit()

    after_interval = load_status_recheck_queue(
        "demo_full_data.jsonl", limit=20, interval_hours=24
    )
    assert "3001" in [entry["item_id"] for entry in after_interval]


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
        return _detail({"itemStatus": 7, "soldCnt": 1, "soldPrice": "3500"})

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


def test_run_status_recheck_allows_sold_to_active_rollback(tmp_path, monkeypatch):
    """终态没有锁：重判为在售时状态可以回落，并新增一条 active 事件。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITEM_STATUS_RECHECK_TERMINAL", "true")
    _save_item("4101")

    sold_info = parse_listing_status(_detail({"itemStatus": 7, "soldPrice": "3500"}))
    apply_listing_status(
        "demo_full_data.jsonl", "4101", sold_info, detected_at="2026-01-02T10:00:00"
    )

    async def fake_fetch_detail(context, url):
        return _detail({"itemStatus": 1, "soldCnt": 0, "soldPrice": "3500"})

    async def no_sleep(min_seconds, max_seconds):
        return None

    summary = asyncio.run(
        run_status_recheck(
            context=None,
            page=None,
            keyword="demo",
            result_filename="demo_full_data.jsonl",
            run_id="run-2",
            fetch_detail=fake_fetch_detail,
            sleep=no_sleep,
            limit=20,
            interval_hours=24,
            max_seconds=60,
            probe_mode="navigate",
        )
    )

    assert summary["checked"] == 1
    assert summary["unchanged"] == 1
    assert _row("4101")["listing_status"] == LISTING_STATUS_ACTIVE

    events = load_item_status_events("demo_full_data.jsonl")
    assert [event["to_status"] for event in events] == [
        LISTING_STATUS_SOLD,
        LISTING_STATUS_ACTIVE,
    ]


# ── 洞察聚合 ────────────────────────────────────────────────────────────


def test_build_sold_status_insights(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_item("5001")
    _save_item("5002")

    sold_info = parse_listing_status(_detail({"itemStatus": 7, "soldCnt": 1, "soldPrice": "3500"}))
    apply_listing_status(
        "demo_full_data.jsonl", "5001", sold_info, detected_at="2026-01-02T10:00:00"
    )
    fallback_info = parse_listing_status(_detail({"itemStatus": 7, "soldCnt": 1}))
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

import json
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import results
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.services.item_status_service import apply_listing_status, parse_listing_status
from src.services.price_history_service import record_market_snapshots
from src.services.result_storage_service import load_item_status_events, load_status_recheck_queue


# 真实码值要用线上响应样本校准；测试里用固定映射覆盖行为。
TEST_CODE_MAP = {"1": "sold", "7": "sold", "-98": "delisted"}


@pytest.fixture(autouse=True)
def _status_code_map(monkeypatch):
    monkeypatch.setenv("ITEM_STATUS_CODE_MAP", json.dumps(TEST_CODE_MAP))


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def test_results_filter_and_sort_for_keyword_recommendations(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    target_file = jsonl_dir / "demo_full_data.jsonl"

    records = [
        {
            "爬取时间": "2026-01-01T01:00:00",
            "商品信息": {"当前售价": "¥1000", "发布时间": "2026-01-01 10:00"},
            "ai_analysis": {
                "analysis_source": "keyword",
                "is_recommended": True,
                "keyword_hit_count": 3,
                "reason": "命中 3 个关键词",
            },
        },
        {
            "爬取时间": "2026-01-01T02:00:00",
            "商品信息": {"当前售价": "¥2000", "发布时间": "2026-01-01 11:00"},
            "ai_analysis": {
                "analysis_source": "keyword",
                "is_recommended": True,
                "keyword_hit_count": 1,
                "reason": "命中 1 个关键词",
            },
        },
        {
            "爬取时间": "2026-01-01T03:00:00",
            "商品信息": {"当前售价": "¥3000", "发布时间": "2026-01-01 12:00"},
            "ai_analysis": {
                "analysis_source": "ai",
                "is_recommended": True,
                "reason": "AI推荐",
            },
        },
    ]
    _write_jsonl(target_file, records)

    app = FastAPI()
    app.include_router(results.router)
    client = TestClient(app)

    resp = client.get(
        "/api/results/demo_full_data.jsonl",
        params={"keyword_recommended_only": True, "sort_by": "keyword_hit_count", "sort_order": "desc"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_items"] == 2
    assert data["items"][0]["ai_analysis"]["keyword_hit_count"] == 3
    assert data["items"][1]["ai_analysis"]["keyword_hit_count"] == 1

    resp = client.get(
        "/api/results/demo_full_data.jsonl",
        params={"ai_recommended_only": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_items"] == 1
    assert data["items"][0]["ai_analysis"]["analysis_source"] == "ai"

    resp = client.get(
        "/api/results/demo_full_data.jsonl",
        params={"ai_recommended_only": True, "keyword_recommended_only": True},
    )
    assert resp.status_code == 400


def test_results_insights_and_export_csv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    target_file = jsonl_dir / "demo_full_data.jsonl"

    records = [
        {
            "爬取时间": "2026-01-02T09:00:00",
            "搜索关键字": "demo",
            "任务名称": "Demo 任务",
            "商品信息": {
                "商品ID": "1001",
                "商品标题": "Demo One",
                "商品链接": "https://www.goofish.com/item?id=1001",
                "当前售价": "¥950",
                "发布时间": "2026-01-02 08:30",
            },
            "卖家信息": {"卖家昵称": "卖家A"},
            "ai_analysis": {
                "analysis_source": "ai",
                "is_recommended": True,
                "reason": "价格低于近期均价",
            },
        },
        {
            "爬取时间": "2026-01-02T09:05:00",
            "搜索关键字": "demo",
            "任务名称": "Demo 任务",
            "商品信息": {
                "商品ID": "1002",
                "商品标题": "Demo Two",
                "商品链接": "https://www.goofish.com/item?id=1002",
                "当前售价": "¥1200",
                "发布时间": "2026-01-02 08:45",
            },
            "卖家信息": {"卖家昵称": "卖家B"},
            "ai_analysis": {
                "analysis_source": "keyword",
                "is_recommended": False,
                "reason": "未命中",
                "keyword_hit_count": 0,
            },
        },
    ]
    _write_jsonl(target_file, records)

    record_market_snapshots(
        keyword="demo",
        task_name="Demo 任务",
        items=[
            {
                "商品ID": "1001",
                "商品标题": "Demo One",
                "当前售价": "¥1000",
                "商品链接": "https://www.goofish.com/item?id=1001",
            },
            {
                "商品ID": "1002",
                "商品标题": "Demo Two",
                "当前售价": "¥1200",
                "商品链接": "https://www.goofish.com/item?id=1002",
            },
        ],
        run_id="run-1",
        snapshot_time="2026-01-01T10:00:00",
        seen_item_ids=set(),
    )
    record_market_snapshots(
        keyword="demo",
        task_name="Demo 任务",
        items=[
            {
                "商品ID": "1001",
                "商品标题": "Demo One",
                "当前售价": "¥950",
                "商品链接": "https://www.goofish.com/item?id=1001",
            },
            {
                "商品ID": "1002",
                "商品标题": "Demo Two",
                "当前售价": "¥1180",
                "商品链接": "https://www.goofish.com/item?id=1002",
            },
        ],
        run_id="run-2",
        snapshot_time="2026-01-02T10:00:00",
        seen_item_ids=set(),
    )

    app = FastAPI()
    app.include_router(results.router)
    client = TestClient(app)

    insights_resp = client.get("/api/results/demo_full_data.jsonl/insights")
    assert insights_resp.status_code == 200
    insights = insights_resp.json()
    assert insights["market_summary"]["sample_count"] == 2
    assert len(insights["daily_trend"]) == 2

    list_resp = client.get("/api/results/demo_full_data.jsonl")
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert items[0]["price_insight"]["observation_count"] >= 1

    export_resp = client.get(
        "/api/results/demo_full_data.jsonl/export",
        params={"sort_by": "price", "sort_order": "asc"},
    )
    assert export_resp.status_code == 200
    assert "text/csv" in export_resp.headers["content-type"]
    text = export_resp.text
    assert "任务名称,搜索关键字,商品ID,商品标题" in text
    assert "Demo One" in text


def test_results_export_csv_supports_unicode_filename(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    target_file = jsonl_dir / "演示_full_data.jsonl"

    records = [
        {
            "爬取时间": "2026-01-02T09:00:00",
            "搜索关键字": "演示",
            "任务名称": "演示任务",
            "商品信息": {
                "商品ID": "1001",
                "商品标题": "演示商品",
                "商品链接": "https://www.goofish.com/item?id=1001",
                "当前售价": "¥950",
                "发布时间": "2026-01-02 08:30",
            },
            "卖家信息": {"卖家昵称": "卖家A"},
            "ai_analysis": {
                "analysis_source": "ai",
                "is_recommended": True,
                "reason": "价格合理",
            },
        }
    ]
    _write_jsonl(target_file, records)

    app = FastAPI()
    app.include_router(results.router)
    client = TestClient(app)

    export_resp = client.get("/api/results/演示_full_data.jsonl/export")
    assert export_resp.status_code == 200
    assert "text/csv" in export_resp.headers["content-type"]
    disposition = export_resp.headers["content-disposition"]
    assert 'filename="export.csv"' in disposition
    assert "filename*=UTF-8''%E6%BC%94%E7%A4%BA_full_data.csv" in disposition


def test_results_blacklist_rules_hide_items_from_view_and_insights(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    target_file = jsonl_dir / "macbook_air_m1_full_data.jsonl"

    records = [
        {
            "爬取时间": "2026-01-03T09:00:00",
            "搜索关键字": "MacBook Air M1",
            "任务名称": "MacBook Air M1 监控",
            "商品信息": {
                "商品ID": "2001",
                "商品标题": "MacBook Air M1 8+256",
                "商品链接": "https://www.goofish.com/item?id=2001",
                "当前售价": "¥4200",
                "发布时间": "2026-01-03 08:30",
            },
            "卖家信息": {"卖家昵称": "卖家A"},
            "ai_analysis": {
                "analysis_source": "ai",
                "is_recommended": True,
                "reason": "符合目标机型",
            },
        },
        {
            "爬取时间": "2026-01-03T09:05:00",
            "搜索关键字": "MacBook Air M1",
            "任务名称": "MacBook Air M1 监控",
            "商品信息": {
                "商品ID": "2002",
                "商品标题": "MacBook Air Intel i5 8+256",
                "商品链接": "https://www.goofish.com/item?id=2002",
                "当前售价": "¥3100",
                "发布时间": "2026-01-03 08:35",
            },
            "卖家信息": {"卖家昵称": "卖家B"},
            "ai_analysis": {
                "analysis_source": "ai",
                "is_recommended": False,
                "reason": "不是目标机型",
            },
        },
        {
            "爬取时间": "2026-01-03T09:10:00",
            "搜索关键字": "MacBook Air M1",
            "任务名称": "MacBook Air M1 监控",
            "商品信息": {
                "商品ID": "2003",
                "商品标题": "MacBook Pro Intel 13寸",
                "商品链接": "https://www.goofish.com/item?id=2003",
                "当前售价": "¥3600",
                "发布时间": "2026-01-03 08:40",
            },
            "卖家信息": {"卖家昵称": "卖家C"},
            "ai_analysis": {
                "analysis_source": "ai",
                "is_recommended": False,
                "reason": "不是 Air M1",
            },
        },
    ]
    _write_jsonl(target_file, records)

    record_market_snapshots(
        keyword="MacBook Air M1",
        task_name="MacBook Air M1 监控",
        items=[
            {
                "商品ID": "2001",
                "商品标题": "MacBook Air M1 8+256",
                "当前售价": "¥4300",
                "商品链接": "https://www.goofish.com/item?id=2001",
            },
            {
                "商品ID": "2002",
                "商品标题": "MacBook Air Intel i5 8+256",
                "当前售价": "¥3200",
                "商品链接": "https://www.goofish.com/item?id=2002",
            },
            {
                "商品ID": "2003",
                "商品标题": "MacBook Pro Intel 13寸",
                "当前售价": "¥3700",
                "商品链接": "https://www.goofish.com/item?id=2003",
            },
        ],
        run_id="run-1",
        snapshot_time="2026-01-02T10:00:00",
        seen_item_ids=set(),
    )
    record_market_snapshots(
        keyword="MacBook Air M1",
        task_name="MacBook Air M1 监控",
        items=[
            {
                "商品ID": "2001",
                "商品标题": "MacBook Air M1 8+256",
                "当前售价": "¥4200",
                "商品链接": "https://www.goofish.com/item?id=2001",
            },
            {
                "商品ID": "2002",
                "商品标题": "MacBook Air Intel i5 8+256",
                "当前售价": "¥3100",
                "商品链接": "https://www.goofish.com/item?id=2002",
            },
            {
                "商品ID": "2003",
                "商品标题": "MacBook Pro Intel 13寸",
                "当前售价": "¥3600",
                "商品链接": "https://www.goofish.com/item?id=2003",
            },
        ],
        run_id="run-2",
        snapshot_time="2026-01-03T10:00:00",
        seen_item_ids=set(),
    )

    app = FastAPI()
    app.include_router(results.router)
    client = TestClient(app)

    update_rules_resp = client.put(
        "/api/results/macbook_air_m1_full_data.jsonl/blacklist-rules",
        json={"keywords": ["intel"]},
    )
    assert update_rules_resp.status_code == 200
    assert update_rules_resp.json()["keywords"] == ["intel"]

    rules_resp = client.get("/api/results/macbook_air_m1_full_data.jsonl/blacklist-rules")
    assert rules_resp.status_code == 200
    assert rules_resp.json()["keywords"] == ["intel"]

    filtered_resp = client.get("/api/results/macbook_air_m1_full_data.jsonl")
    assert filtered_resp.status_code == 200
    filtered_payload = filtered_resp.json()
    assert filtered_payload["total_items"] == 1
    assert [item["商品信息"]["商品ID"] for item in filtered_payload["items"]] == ["2001"]

    include_hidden_resp = client.get(
        "/api/results/macbook_air_m1_full_data.jsonl",
        params={"include_hidden": True},
    )
    assert include_hidden_resp.status_code == 200
    include_hidden_payload = include_hidden_resp.json()
    assert include_hidden_payload["total_items"] == 3
    hidden_item = next(
        item for item in include_hidden_payload["items"]
        if item["商品信息"]["商品ID"] == "2002"
    )
    assert hidden_item["_hidden_reason"] == "rule"
    assert hidden_item["_matched_blacklist_keywords"] == ["intel"]

    insights_resp = client.get("/api/results/macbook_air_m1_full_data.jsonl/insights")
    assert insights_resp.status_code == 200
    insights = insights_resp.json()
    assert insights["market_summary"]["sample_count"] == 1
    assert insights["market_summary"]["avg_price"] == 4200.0
    assert all(point["sample_count"] == 1 for point in insights["daily_trend"])

    list_resp = client.get("/api/results/macbook_air_m1_full_data.jsonl")
    assert list_resp.status_code == 200
    visible_item = list_resp.json()["items"][0]
    assert visible_item["price_insight"]["market_avg_price"] == 4200.0

    export_resp = client.get("/api/results/macbook_air_m1_full_data.jsonl/export")
    assert export_resp.status_code == 200
    assert "MacBook Air M1 8+256" in export_resp.text
    assert "MacBook Air Intel i5 8+256" not in export_resp.text
    assert "MacBook Pro Intel 13寸" not in export_resp.text

    download_resp = client.get("/api/results/files/macbook_air_m1_full_data.jsonl")
    assert download_resp.status_code == 200
    assert "MacBook Air Intel i5 8+256" in download_resp.text
    assert "MacBook Pro Intel 13寸" in download_resp.text


def _listing_detail(item_status=1, sold_cnt=0, sold_price=""):
    item_do = {"itemStatus": item_status, "soldCnt": sold_cnt}
    if sold_price:
        item_do["soldPrice"] = sold_price
    return {"ret": ["SUCCESS::调用成功"], "data": {"itemDO": item_do}}


def test_results_expose_listing_status_and_sold_price(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    target_file = jsonl_dir / "demo_full_data.jsonl"

    records = [
        {
            "爬取时间": "2026-01-02T09:00:00",
            "搜索关键字": "demo",
            "任务名称": "Demo 任务",
            "商品信息": {
                "商品ID": "1001",
                "商品标题": "Demo One",
                "商品链接": "https://www.goofish.com/item?id=1001",
                "当前售价": "¥950",
                "发布时间": "2026-01-02 08:30",
            },
            "卖家信息": {"卖家昵称": "卖家A"},
            "ai_analysis": {
                "analysis_source": "ai",
                "is_recommended": True,
                "reason": "价格低于近期均价",
            },
        },
        {
            "爬取时间": "2026-01-02T09:05:00",
            "搜索关键字": "demo",
            "任务名称": "Demo 任务",
            "商品信息": {
                "商品ID": "1002",
                "商品标题": "Demo Two",
                "商品链接": "https://www.goofish.com/item?id=1002",
                "当前售价": "¥1200",
                "发布时间": "2026-01-02 08:45",
            },
            "卖家信息": {"卖家昵称": "卖家B"},
            "ai_analysis": {
                "analysis_source": "keyword",
                "is_recommended": False,
                "reason": "未命中",
                "keyword_hit_count": 0,
            },
        },
    ]
    _write_jsonl(target_file, records)

    apply_listing_status(
        "demo_full_data.jsonl",
        "1001",
        parse_listing_status(_listing_detail(sold_cnt=1, sold_price="900")),
        detected_at="2026-01-03T10:00:00",
    )
    apply_listing_status(
        "demo_full_data.jsonl",
        "1002",
        parse_listing_status(_listing_detail(item_status=-98)),
        detected_at="2026-01-03T10:00:00",
    )

    app = FastAPI()
    app.include_router(results.router)
    client = TestClient(app)

    list_resp = client.get("/api/results/demo_full_data.jsonl")
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    by_id = {item["商品信息"]["商品ID"]: item for item in items}
    assert by_id["1001"]["_listing_status"] == "sold"
    assert by_id["1001"]["_sold_price"] == 900.0
    assert by_id["1001"]["_sold_price_source"] == "sold_price_api"
    assert by_id["1001"]["_status_checked_at"] == "2026-01-03T10:00:00"
    assert by_id["1002"]["_listing_status"] == "delisted"
    assert by_id["1002"]["_sold_price"] is None

    # 已售 / 已下架的记录仍然保留显示，不被可见性状态过滤掉
    assert list_resp.json()["total_items"] == 2

    sold_only = client.get(
        "/api/results/demo_full_data.jsonl", params={"listing_status": "sold"}
    )
    assert sold_only.status_code == 200
    sold_payload = sold_only.json()
    assert sold_payload["total_items"] == 1
    assert sold_payload["items"][0]["商品信息"]["商品ID"] == "1001"

    delisted_only = client.get(
        "/api/results/demo_full_data.jsonl", params={"listing_status": "delisted"}
    )
    assert delisted_only.status_code == 200
    assert delisted_only.json()["total_items"] == 1

    insights_resp = client.get("/api/results/demo_full_data.jsonl/insights")
    assert insights_resp.status_code == 200
    insights = insights_resp.json()
    assert insights["sold_summary"]["sold_count"] == 1
    assert insights["sold_summary"]["delisted_count"] == 1
    assert insights["sold_trend"][0]["day"] == "2026-01-03"
    assert insights["sold_trend"][0]["sold_count"] == 1
    assert insights["sold_trend"][0]["avg_price"] == 900.0

    export_resp = client.get("/api/results/demo_full_data.jsonl/export")
    assert export_resp.status_code == 200
    text = export_resp.text
    assert "商品状态,成交价,成交价口径,状态判定时间" in text
    assert "已售" in text
    assert "已下架" in text


def test_manual_hide_does_not_change_listing_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    target_file = jsonl_dir / "demo_full_data.jsonl"

    records = [
        {
            "爬取时间": "2026-01-02T09:00:00",
            "搜索关键字": "demo",
            "任务名称": "Demo 任务",
            "商品信息": {
                "商品ID": "1001",
                "商品标题": "Demo One",
                "商品链接": "https://www.goofish.com/item?id=1001",
                "当前售价": "¥950",
                "发布时间": "2026-01-02 08:30",
            },
            "卖家信息": {"卖家昵称": "卖家A"},
            "ai_analysis": {
                "analysis_source": "ai",
                "is_recommended": True,
                "reason": "ok",
            },
        }
    ]
    _write_jsonl(target_file, records)

    apply_listing_status(
        "demo_full_data.jsonl",
        "1001",
        parse_listing_status(_listing_detail(sold_cnt=1, sold_price="900")),
        detected_at="2026-01-03T10:00:00",
    )

    app = FastAPI()
    app.include_router(results.router)
    client = TestClient(app)

    hide_resp = client.patch(
        "/api/results/demo_full_data.jsonl/items/1001/status",
        json={"status": "hidden"},
    )
    assert hide_resp.status_code == 200

    hidden_resp = client.get(
        "/api/results/demo_full_data.jsonl", params={"include_hidden": True}
    )
    assert hidden_resp.status_code == 200
    item = hidden_resp.json()["items"][0]
    # 手动屏蔽只影响可见性，在售状态与成交价保持不变
    assert item["_status"] == "hidden"
    assert item["_listing_status"] == "sold"
    assert item["_sold_price"] == 900.0

    visible_resp = client.get("/api/results/demo_full_data.jsonl")
    assert visible_resp.status_code == 200
    assert visible_resp.json()["total_items"] == 0


def test_manual_status_check_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ITEM_STATUS_MANUAL_CHECK_ENABLED", raising=False)

    app = FastAPI()
    app.include_router(results.router)
    client = TestClient(app)

    resp = client.post("/api/results/demo_full_data.jsonl/status-check")
    assert resp.status_code == 403
    assert "ITEM_STATUS_MANUAL_CHECK_ENABLED" in resp.json()["detail"]


def test_manual_status_check_starts_background_job_when_enabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITEM_STATUS_MANUAL_CHECK_ENABLED", "true")
    calls: dict = {}

    async def fake_run(*, keyword, result_filename, run_id=None):
        calls["keyword"] = keyword
        calls["result_filename"] = result_filename
        return {"checked": 1, "sold": 0, "delisted": 0, "failed": 0}

    monkeypatch.setattr(results, "run_standalone_status_check", fake_run)

    app = FastAPI()
    app.include_router(results.router)
    client = TestClient(app)

    resp = client.post("/api/results/demo_full_data.jsonl/status-check")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["filename"] == "demo_full_data.jsonl"
    assert payload["keyword"] == "demo"

    # 再发一次请求，给事件循环机会把后台任务跑完
    client.get("/api/results/demo_full_data.jsonl")
    assert calls.get("keyword") == "demo"
    assert calls.get("result_filename") == "demo_full_data.jsonl"


# ── 手动重置误判状态（status-reset） ────────────────────────────────────


def _seed_status_records(jsonl_dir, item_ids):
    """写一份结果文件，并给每条记录灌入指定的在售状态。"""
    records = []
    for index, item_id in enumerate(item_ids):
        records.append(
            {
                "爬取时间": f"2026-01-02T09:0{index}:00",
                "搜索关键字": "demo",
                "任务名称": "Demo 任务",
                "商品信息": {
                    "商品ID": item_id,
                    "商品标题": f"Demo {item_id}",
                    "商品链接": f"https://www.goofish.com/item?id={item_id}",
                    "当前售价": "¥950",
                    "发布时间": "2026-01-02 08:30",
                },
                "卖家信息": {"卖家昵称": "卖家A"},
                "ai_analysis": {
                    "analysis_source": "ai",
                    "is_recommended": True,
                    "reason": "符合预期",
                },
            }
        )
    _write_jsonl(jsonl_dir / "demo_full_data.jsonl", records)


def _status_row(item_id):
    with sqlite_connection() as conn:
        row = conn.execute(
            """
            SELECT listing_status, sold_price, sold_price_display, sold_price_source,
                   sold_at, status_checked_at, status_source, status_raw_code
            FROM result_items WHERE item_id = ?
            """,
            (item_id,),
        ).fetchone()
    return dict(row) if row is not None else {}


def test_status_reset_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ITEM_STATUS_RESET_ENABLED", raising=False)

    app = FastAPI()
    app.include_router(results.router)
    client = TestClient(app)

    resp = client.post("/api/results/demo_full_data.jsonl/status-reset", json={})
    assert resp.status_code == 403
    assert "ITEM_STATUS_RESET_ENABLED" in resp.json()["detail"]


def test_status_reset_rejects_invalid_filename(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITEM_STATUS_RESET_ENABLED", "true")

    app = FastAPI()
    app.include_router(results.router)
    client = TestClient(app)

    resp = client.post("/api/results/demo.txt/status-reset", json={})
    assert resp.status_code == 400


def test_status_reset_resets_records_and_deletes_events(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITEM_STATUS_RESET_ENABLED", "true")
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    _seed_status_records(jsonl_dir, ["1001", "1002"])

    apply_listing_status(
        "demo_full_data.jsonl",
        "1001",
        parse_listing_status(_listing_detail(item_status=1, sold_cnt=1, sold_price="900")),
        detected_at="2026-01-03T10:00:00",
    )
    apply_listing_status(
        "demo_full_data.jsonl",
        "1002",
        parse_listing_status(_listing_detail(item_status=-98)),
        detected_at="2026-01-03T10:00:00",
    )
    assert len(load_item_status_events("demo_full_data.jsonl")) == 2

    app = FastAPI()
    app.include_router(results.router)
    client = TestClient(app)

    resp = client.post("/api/results/demo_full_data.jsonl/status-reset", json={})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["reset_items"] == 2
    assert payload["deleted_events"] == 2
    assert payload["recheck_started"] is False
    assert payload["recheck_limit"] == 2
    assert payload["recheck_budget_seconds"] > 0

    row = _status_row("1001")
    assert row["listing_status"] == "unknown"
    assert row["sold_price"] is None
    assert row["sold_price_display"] == ""
    assert row["sold_price_source"] == ""
    assert row["sold_at"] is None
    assert row["status_checked_at"] == ""
    assert row["status_source"] is None

    # 成交价曲线的数据源（状态事件）被清掉
    assert load_item_status_events("demo_full_data.jsonl") == []

    # 结果页不再显示假的「已售」
    list_resp = client.get("/api/results/demo_full_data.jsonl")
    assert list_resp.status_code == 200
    for item in list_resp.json()["items"]:
        assert item["_listing_status"] == "unknown"
        assert item["_sold_price"] is None


def test_status_reset_respects_item_ids_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITEM_STATUS_RESET_ENABLED", "true")
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    _seed_status_records(jsonl_dir, ["1001", "1002"])

    for item_id in ("1001", "1002"):
        apply_listing_status(
            "demo_full_data.jsonl",
            item_id,
            parse_listing_status(_listing_detail(item_status=1, sold_cnt=1, sold_price="900")),
            detected_at="2026-01-03T10:00:00",
        )

    app = FastAPI()
    app.include_router(results.router)
    client = TestClient(app)

    resp = client.post(
        "/api/results/demo_full_data.jsonl/status-reset",
        json={"item_ids": ["1001"]},
    )
    assert resp.status_code == 200
    assert resp.json()["reset_items"] == 1

    assert _status_row("1001")["listing_status"] == "unknown"
    assert _status_row("1002")["listing_status"] == "sold"


def test_status_reset_respects_status_raw_codes_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITEM_STATUS_RESET_ENABLED", "true")
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    _seed_status_records(jsonl_dir, ["1001"])

    apply_listing_status(
        "demo_full_data.jsonl",
        "1001",
        parse_listing_status(_listing_detail(item_status=1, sold_cnt=1, sold_price="900")),
        detected_at="2026-01-03T10:00:00",
    )

    app = FastAPI()
    app.include_router(results.router)
    client = TestClient(app)

    resp = client.post(
        "/api/results/demo_full_data.jsonl/status-reset",
        json={"status_raw_codes": [999]},
    )
    assert resp.status_code == 200
    assert resp.json()["reset_items"] == 0
    assert _status_row("1001")["listing_status"] == "sold"

    resp = client.post(
        "/api/results/demo_full_data.jsonl/status-reset",
        json={"status_raw_codes": [1]},
    )
    assert resp.status_code == 200
    assert resp.json()["reset_items"] == 1


def test_status_reset_rejects_unknown_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITEM_STATUS_RESET_ENABLED", "true")

    app = FastAPI()
    app.include_router(results.router)
    client = TestClient(app)

    resp = client.post(
        "/api/results/demo_full_data.jsonl/status-reset",
        json={"statuses": ["not-a-status"]},
    )
    assert resp.status_code == 400


def test_reset_records_are_queued_first_for_recheck(tmp_path, monkeypatch):
    """重置后 status_checked_at 为空 → 下一轮复核优先重判它们。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITEM_STATUS_RESET_ENABLED", "true")
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    _seed_status_records(jsonl_dir, ["1001", "1002"])

    apply_listing_status(
        "demo_full_data.jsonl",
        "1001",
        parse_listing_status(_listing_detail(item_status=1, sold_cnt=1, sold_price="900")),
        detected_at="2026-01-03T10:00:00",
    )
    # 1002 刚复核过，24 小时间隔内不会进队列
    apply_listing_status(
        "demo_full_data.jsonl",
        "1002",
        parse_listing_status(_listing_detail(item_status=4242)),
        detected_at=datetime.now().isoformat(),
    )

    app = FastAPI()
    app.include_router(results.router)
    client = TestClient(app)

    resp = client.post("/api/results/demo_full_data.jsonl/status-reset", json={})
    assert resp.status_code == 200

    queue = load_status_recheck_queue("demo_full_data.jsonl", limit=20, interval_hours=24)
    assert [entry["item_id"] for entry in queue] == ["1001"]


def test_status_reset_starts_recheck_with_limit_when_enabled(tmp_path, monkeypatch):
    """开启手动复核后，重置会顺带触发一次复核，并把重置条数作为上限。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITEM_STATUS_RESET_ENABLED", "true")
    monkeypatch.setenv("ITEM_STATUS_MANUAL_CHECK_ENABLED", "true")
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    _seed_status_records(jsonl_dir, ["1001", "1002"])

    for item_id in ("1001", "1002"):
        apply_listing_status(
            "demo_full_data.jsonl",
            item_id,
            parse_listing_status(_listing_detail(item_status=1, sold_cnt=1, sold_price="900")),
            detected_at="2026-01-03T10:00:00",
        )

    calls: dict = {}

    async def fake_run(*, keyword, result_filename, run_id=None, limit=None, max_seconds=None):
        calls["keyword"] = keyword
        calls["result_filename"] = result_filename
        calls["limit"] = limit
        return {"checked": 0, "sold": 0, "delisted": 0, "failed": 0}

    monkeypatch.setattr(results, "run_standalone_status_check", fake_run)

    app = FastAPI()
    app.include_router(results.router)
    client = TestClient(app)

    resp = client.post("/api/results/demo_full_data.jsonl/status-reset", json={})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["recheck_started"] is True
    assert payload["recheck_limit"] == 2

    # 再发一次请求，给事件循环机会把后台任务跑完
    client.get("/api/results/demo_full_data.jsonl")
    assert calls.get("limit") == 2
    assert calls.get("result_filename") == "demo_full_data.jsonl"


def test_status_reset_accepts_request_without_body(tmp_path, monkeypatch):
    """请求体可省略：等价于默认参数（重置该文件内全部已售 / 已下架记录）。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITEM_STATUS_RESET_ENABLED", "true")
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    _seed_status_records(jsonl_dir, ["1001"])

    apply_listing_status(
        "demo_full_data.jsonl",
        "1001",
        parse_listing_status(_listing_detail(item_status=1, sold_cnt=1, sold_price="900")),
        detected_at="2026-01-03T10:00:00",
    )

    app = FastAPI()
    app.include_router(results.router)
    client = TestClient(app)

    resp = client.post("/api/results/demo_full_data.jsonl/status-reset")
    assert resp.status_code == 200
    assert resp.json()["reset_items"] == 1
    assert _status_row("1001")["listing_status"] == "unknown"


# ── 人工标记已售（listing-status） ─────────────────────────────────────


def _client():
    app = FastAPI()
    app.include_router(results.router)
    return TestClient(app)


def test_manual_mark_sold_updates_listing_status_and_price(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    _seed_status_records(jsonl_dir, ["1001"])

    client = _client()

    resp = client.patch(
        "/api/results/demo_full_data.jsonl/items/1001/listing-status",
        json={"listing_status": "sold"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["listing_status"] == "sold"
    assert payload["changed"] is True
    assert payload["sold_price"] == 950.0
    assert payload["sold_price_source"] == "listing_fallback"

    list_resp = client.get("/api/results/demo_full_data.jsonl")
    assert list_resp.status_code == 200
    item = list_resp.json()["items"][0]
    assert item["_listing_status"] == "sold"
    assert item["_sold_price"] == 950.0
    assert item["_sold_price_display"] == "¥950"
    assert item["_sold_price_source"] == "listing_fallback"
    assert item["_status_source"] == "manual"


def test_manual_unmark_sold_resets_listing_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    _seed_status_records(jsonl_dir, ["1001"])

    client = _client()
    client.patch(
        "/api/results/demo_full_data.jsonl/items/1001/listing-status",
        json={"listing_status": "sold"},
    )

    resp = client.patch(
        "/api/results/demo_full_data.jsonl/items/1001/listing-status",
        json={"listing_status": "unknown"},
    )
    assert resp.status_code == 200
    assert resp.json()["changed"] is True
    assert resp.json()["listing_status"] == "unknown"

    item = client.get("/api/results/demo_full_data.jsonl").json()["items"][0]
    assert item["_listing_status"] == "unknown"
    assert item["_sold_price"] is None
    assert item["_sold_price_display"] == ""


def test_manual_mark_sold_updates_insights(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    _seed_status_records(jsonl_dir, ["1001"])

    client = _client()
    client.patch(
        "/api/results/demo_full_data.jsonl/items/1001/listing-status",
        json={"listing_status": "sold"},
    )

    insights = client.get("/api/results/demo_full_data.jsonl/insights").json()
    assert insights["sold_summary"]["sold_count"] == 1
    assert insights["sold_summary"]["estimated_count"] == 1
    assert insights["sold_trend"][0]["avg_price"] == 950.0

    client.patch(
        "/api/results/demo_full_data.jsonl/items/1001/listing-status",
        json={"listing_status": "unknown"},
    )
    insights = client.get("/api/results/demo_full_data.jsonl/insights").json()
    assert insights["sold_summary"]["sold_count"] == 0
    assert insights["sold_trend"] == []


def test_manual_mark_sold_does_not_change_visibility(tmp_path, monkeypatch):
    """在售状态与可见性是两套语义：标记已售不会隐藏记录。"""
    monkeypatch.chdir(tmp_path)
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    _seed_status_records(jsonl_dir, ["1001"])

    client = _client()
    client.patch(
        "/api/results/demo_full_data.jsonl/items/1001/listing-status",
        json={"listing_status": "sold"},
    )

    item = client.get("/api/results/demo_full_data.jsonl").json()["items"][0]
    assert item["_status"] == "active"
    assert item["_listing_status"] == "sold"
    assert client.get("/api/results/demo_full_data.jsonl").json()["total_items"] == 1


def test_manual_mark_sold_rejects_invalid_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    _seed_status_records(jsonl_dir, ["1001"])

    client = _client()
    resp = client.patch(
        "/api/results/demo_full_data.jsonl/items/1001/listing-status",
        json={"listing_status": "delisted"},
    )
    assert resp.status_code == 422


def test_manual_mark_sold_rejects_invalid_filename(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    client = _client()
    resp = client.patch(
        "/api/results/demo.txt/items/1001/listing-status",
        json={"listing_status": "sold"},
    )
    assert resp.status_code == 400


def test_manual_mark_sold_returns_404_for_missing_item(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    _seed_status_records(jsonl_dir, ["1001"])

    client = _client()
    resp = client.patch(
        "/api/results/demo_full_data.jsonl/items/404/listing-status",
        json={"listing_status": "sold"},
    )
    assert resp.status_code == 404


def test_manual_mark_sold_is_excluded_from_recheck_queue(tmp_path, monkeypatch):
    """人工标记的已售不会被自动巡检覆盖。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITEM_STATUS_RECHECK_TERMINAL", "true")
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    _seed_status_records(jsonl_dir, ["1001"])

    client = _client()
    client.patch(
        "/api/results/demo_full_data.jsonl/items/1001/listing-status",
        json={"listing_status": "sold"},
    )

    queue = load_status_recheck_queue("demo_full_data.jsonl", limit=20)
    assert "1001" not in {entry["item_id"] for entry in queue}

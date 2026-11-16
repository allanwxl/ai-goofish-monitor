import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import results
from src.services.item_status_service import apply_listing_status, parse_listing_status
from src.services.price_history_service import record_market_snapshots


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

"""商品状态巡检层的失败处理测试。

重点验证两条铁律：

* **探测失败不改业务状态**（接口级失败 / 返回了别的商品）
* **时间预算与条数上限不被绕过**
"""

import asyncio

from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.services import item_status_task_runner
from src.services.item_status_rechecker import run_status_recheck
from src.services.result_storage_service import save_result_record


def _detail(item_do=None, ret="SUCCESS::调用成功"):
    payload = {"ret": [ret], "data": {}}
    if item_do is not None:
        payload["data"]["itemDO"] = item_do
    return payload


def _save_item(item_id: str, *, keyword: str = "demo"):
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
                    "当前售价": "¥1000",
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


async def _no_sleep(min_seconds, max_seconds):
    return None


def _listing_status_of(item_id: str):
    with sqlite_connection() as conn:
        row = conn.execute(
            """
            SELECT listing_status, status_source, status_checked_at, status_raw_code
            FROM result_items WHERE item_id = ?
            """,
            (item_id,),
        ).fetchone()
    return dict(row) if row is not None else {}


def test_probe_failure_keeps_listing_status(tmp_path, monkeypatch):
    """会话过期时状态保持不变，只留复核时间戳与 error 来源。"""
    monkeypatch.chdir(tmp_path)
    _save_item("8001")

    async def fake_fetch_detail(context, url):
        return _detail(None, ret="FAIL_SYS_SESSION_EXPIRED::Session过期")

    summary = asyncio.run(
        run_status_recheck(
            context=None,
            page=None,
            keyword="demo",
            result_filename="demo_full_data.jsonl",
            run_id="run-1",
            fetch_detail=fake_fetch_detail,
            sleep=_no_sleep,
            limit=20,
            interval_hours=24,
            max_seconds=60,
            probe_mode="navigate",
        )
    )

    assert summary["checked"] == 0
    assert summary["failed"] == 1
    assert summary["sold"] == 0
    assert summary["delisted"] == 0

    row = _listing_status_of("8001")
    assert row["listing_status"] == "unknown"
    assert row["status_source"] == "error"
    assert row["status_checked_at"]


def test_item_id_mismatch_keeps_listing_status(tmp_path, monkeypatch):
    """接口返回了别的商品时不能把它的状态写到这个商品上。"""
    monkeypatch.chdir(tmp_path)
    _save_item("8002")

    async def fake_fetch_detail(context, url):
        return _detail({"itemId": "999999", "itemStatus": 7, "soldPrice": "3500"})

    summary = asyncio.run(
        run_status_recheck(
            context=None,
            page=None,
            keyword="demo",
            result_filename="demo_full_data.jsonl",
            run_id="run-1",
            fetch_detail=fake_fetch_detail,
            sleep=_no_sleep,
            limit=20,
            interval_hours=24,
            max_seconds=60,
            probe_mode="navigate",
        )
    )

    assert summary["checked"] == 0
    assert summary["failed"] == 1

    row = _listing_status_of("8002")
    assert row["listing_status"] == "unknown"
    assert row["status_source"] == "error"


def test_probe_failure_keeps_previous_raw_code(tmp_path, monkeypatch):
    """探测失败不该把上一轮留档的原始状态码冲掉（便于事后校准码表）。"""
    monkeypatch.chdir(tmp_path)
    _save_item("8003")

    async def fake_fetch_detail(context, url):
        return _detail({"itemId": "8003", "itemStatus": 7, "soldPrice": "3500"})

    async def failing_fetch_detail(context, url):
        return _detail(None, ret="FAIL_SYS_SESSION_EXPIRED::Session过期")

    asyncio.run(
        run_status_recheck(
            context=None,
            page=None,
            keyword="demo",
            result_filename="demo_full_data.jsonl",
            run_id="run-1",
            fetch_detail=fake_fetch_detail,
            sleep=_no_sleep,
            limit=20,
            interval_hours=0,
            max_seconds=60,
            probe_mode="navigate",
        )
    )
    assert _listing_status_of("8003")["status_raw_code"] == 7

    asyncio.run(
        run_status_recheck(
            context=None,
            page=None,
            keyword="demo",
            result_filename="demo_full_data.jsonl",
            run_id="run-2",
            fetch_detail=failing_fetch_detail,
            sleep=_no_sleep,
            limit=20,
            interval_hours=0,
            max_seconds=60,
            probe_mode="navigate",
        )
    )
    row = _listing_status_of("8003")
    assert row["status_raw_code"] == 7
    assert row["status_source"] == "error"


def test_limit_override_is_applied(tmp_path, monkeypatch):
    """limit 覆盖生效：一次复核能覆盖超过默认 20 条。"""
    monkeypatch.chdir(tmp_path)
    for index in range(25):
        _save_item(f"81{index:02d}")

    async def fake_fetch_detail(context, url):
        return _detail({"itemStatus": 1, "soldCnt": 0})

    summary = asyncio.run(
        run_status_recheck(
            context=None,
            page=None,
            keyword="demo",
            result_filename="demo_full_data.jsonl",
            run_id="run-1",
            fetch_detail=fake_fetch_detail,
            sleep=_no_sleep,
            limit=25,
            interval_hours=24,
            max_seconds=120,
            probe_mode="navigate",
        )
    )

    assert summary["checked"] == 25


def test_time_budget_is_not_opened_by_limit_override(tmp_path, monkeypatch):
    """条数上限放开不等于时间预算放开：两者是独立参数。"""
    monkeypatch.chdir(tmp_path)
    for index in range(3):
        _save_item(f"82{index:02d}")

    async def fake_fetch_detail(context, url):
        return _detail({"itemStatus": 1, "soldCnt": 0})

    summary = asyncio.run(
        run_status_recheck(
            context=None,
            page=None,
            keyword="demo",
            result_filename="demo_full_data.jsonl",
            run_id="run-1",
            fetch_detail=fake_fetch_detail,
            sleep=_no_sleep,
            limit=100,
            interval_hours=24,
            max_seconds=300,
            probe_mode="navigate",
        )
    )

    # 只跑了队列里有的 3 条，条数上限不会凭空造出更多请求
    assert summary["checked"] == 3


class _FakePage:
    async def goto(self, *args, **kwargs):
        return None


class _FakeContext:
    async def new_page(self):
        return _FakePage()


class _FakeBrowser:
    async def close(self):
        return None


class _FakePlaywright:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc_info):
        return False


def test_standalone_status_check_forwards_limit_and_budget(tmp_path, monkeypatch):
    """手动复核执行器把 limit / max_seconds 透传给巡检（清理后一次性重判要用）。"""
    monkeypatch.chdir(tmp_path)
    captured: dict = {}

    async def fake_run_status_recheck(**kwargs):
        captured.update(kwargs)
        return {"checked": 0, "failed": 0}

    async def fake_resolve_state_file(keyword):
        return "state/demo.json"

    async def fake_create_logged_in_context(playwright, state_file, channel=None):
        return _FakeBrowser(), _FakeContext()

    monkeypatch.setattr(
        item_status_task_runner, "run_status_recheck", fake_run_status_recheck
    )
    monkeypatch.setattr(
        item_status_task_runner, "resolve_state_file", fake_resolve_state_file
    )
    monkeypatch.setattr(
        item_status_task_runner, "create_logged_in_context", fake_create_logged_in_context
    )
    monkeypatch.setattr(
        item_status_task_runner, "async_playwright", lambda: _FakePlaywright()
    )

    summary = asyncio.run(
        item_status_task_runner.run_standalone_status_check(
            keyword="demo",
            result_filename="demo_full_data.jsonl",
            limit=99,
            max_seconds=120,
        )
    )

    assert captured["limit"] == 99
    assert captured["max_seconds"] == 120
    assert summary["run_id"]

"""MCP 数据服务的单元测试。

所有数据库相关用例都指向 tmp_path 下的临时 SQLite，
不会污染仓库内的 data/app.sqlite3。
"""
import asyncio
import json
import socket
import sqlite3

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from src.infrastructure.config.settings import mcp_settings
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.mcp.auth import (
    ApiKeyEntry,
    MCPConfigurationError,
    load_api_keys,
    parse_api_keys,
    verify_api_key,
)
from src.mcp.server import create_server
from src.mcp.tools import redact_record, search_items


TEST_KEY = "sk-unit-test-key"
TEST_KEY_WITH_TENANT = "sk-tenant-key:tenant-x"


# —— fixtures ——


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """建一个临时 SQLite 库并灌入种子数据，返回库路径。"""
    db_path = tmp_path / "mcp_test.sqlite3"
    monkeypatch.setenv("APP_DATABASE_FILE", str(db_path))

    bootstrap_sqlite_storage(
        str(db_path),
        legacy_config_file=None,
        legacy_result_dir=str(tmp_path / "empty_jsonl"),
        legacy_price_history_dir=str(tmp_path / "empty_history"),
    )

    keyword = "测试关键词"
    filename = f"{keyword}_full_data.jsonl"
    with sqlite_connection(str(db_path)) as conn:
        for index in range(5):
            item_id = f"item-{index}"
            record = {
                "爬取时间": f"2026-11-0{index + 1}T10:00:00",
                "搜索关键字": keyword,
                "任务名称": "测试任务",
                "商品信息": {
                    "商品ID": item_id,
                    "商品标题": f"测试商品 {index}",
                    "商品链接": f"https://www.goofish.com/item?id={item_id}",
                    "当前售价": f"¥{1000 + index}",
                    "卖家昵称": "不该外发的昵称",
                    "发货地区": "浙江",
                    "发布时间": "2026-10-01 10:00",
                },
                "卖家信息": {
                    "卖家昵称": "不该外发的昵称",
                    "卖家头像链接": "http://example.com/avatar.jpg",
                    "卖家个性签名": "不该外发的签名",
                    "卖家发布的商品列表": [{"商品ID": "other"}],
                    "卖家信用等级": "卖家信用优秀",
                },
                "ai_analysis": {
                    "analysis_source": "ai",
                    "is_recommended": index % 2 == 0,
                    "reason": "理由",
                    "keyword_hit_count": 1,
                    "error": "内部实现细节，不应外发",
                },
            }
            conn.execute(
                """
                INSERT INTO result_items (
                    result_filename, keyword, task_name, crawl_time, publish_time,
                    price, price_display, item_id, title, link, link_unique_key,
                    seller_nickname, is_recommended, analysis_source,
                    keyword_hit_count, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    filename,
                    keyword,
                    "测试任务",
                    f"2026-11-0{index + 1}T10:00:00",
                    "2026-10-01 10:00",
                    float(1000 + index),
                    f"¥{1000 + index}",
                    item_id,
                    f"测试商品 {index}",
                    f"https://www.goofish.com/item?id={item_id}",
                    f"https://www.goofish.com/item?id={item_id}",
                    "不该外发的昵称",
                    1 if index % 2 == 0 else 0,
                    "ai",
                    1,
                    json.dumps(record, ensure_ascii=False),
                ),
            )
        conn.commit()

    return str(db_path)


@pytest.fixture()
def api_keys(monkeypatch):
    """给配置写入测试用 API Key。"""
    monkeypatch.setattr(mcp_settings, "api_keys", f"{TEST_KEY},{TEST_KEY_WITH_TENANT}")
    monkeypatch.setattr(mcp_settings, "allowed_tools", "")
    return TEST_KEY


def _free_port() -> int:
    """取一个空闲端口，避免并发测试互相抢占。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# —— 鉴权 ——


def test_parse_api_keys_variants():
    entries = parse_api_keys("k1, k2:tenant-b ,,k3: ")
    assert entries == [
        ApiKeyEntry(key="k1", tenant="default"),
        ApiKeyEntry(key="k2", tenant="tenant-b"),
        ApiKeyEntry(key="k3", tenant=""),
    ]


def test_parse_api_keys_empty():
    assert parse_api_keys("") == []
    assert parse_api_keys(None) == []
    assert parse_api_keys("  ,  ") == []


def test_verify_api_key_accepts_valid_key(api_keys):
    entry = verify_api_key(f"Bearer {TEST_KEY}")
    assert entry is not None
    assert entry.key == TEST_KEY
    assert entry.tenant == "default"


def test_verify_api_key_without_bearer_prefix(api_keys):
    assert verify_api_key(TEST_KEY) is not None


def test_verify_api_key_extracts_tenant(api_keys):
    entry = verify_api_key(f"Bearer {TEST_KEY_WITH_TENANT}")
    assert entry is not None
    assert entry.tenant == "tenant-x"


def test_verify_api_key_rejects_wrong_key(api_keys):
    assert verify_api_key("Bearer wrong-key") is None


def test_verify_api_key_rejects_missing_header(api_keys):
    assert verify_api_key(None) is None
    assert verify_api_key("") is None
    assert verify_api_key("Bearer ") is None


def test_load_api_keys_raises_when_not_configured(monkeypatch):
    """未配置 API Key 时必须拒绝启动，而不是放行全部请求。"""
    monkeypatch.setattr(mcp_settings, "api_keys", "")
    with pytest.raises(MCPConfigurationError):
        load_api_keys()


# —— 字段裁剪 ——


def test_redact_record_strips_personal_information():
    record = {
        "爬取时间": "2026-11-01T10:00:00",
        "搜索关键字": "kw",
        "任务名称": "task",
        "商品信息": {
            "商品ID": "1",
            "商品标题": "标题",
            "当前售价": "¥100",
            "卖家昵称": "不该外发的昵称",
        },
        "卖家信息": {
            "卖家昵称": "不该外发的昵称",
            "卖家头像链接": "http://example.com/a.jpg",
            "卖家个性签名": "签名",
            "卖家发布的商品列表": [{"商品ID": "x"}],
            "卖家信用等级": "卖家信用优秀",
        },
        "ai_analysis": {
            "is_recommended": True,
            "reason": "理由",
            "error": "内部实现细节",
        },
    }

    redacted = redact_record(record)

    serialized = json.dumps(redacted, ensure_ascii=False)
    assert "不该外发的昵称" not in serialized
    assert "不该外发的签名" not in serialized
    assert "卖家头像链接" not in serialized
    assert "卖家发布的商品列表" not in serialized
    assert "内部实现细节" not in serialized

    # 业务字段必须保留
    assert redacted["商品信息"]["商品标题"] == "标题"
    assert redacted["商品信息"]["当前售价"] == "¥100"
    assert redacted["商品信息"]["商品ID"] == "1"
    assert redacted["卖家信息"]["卖家信用等级"] == "卖家信用优秀"
    assert redacted["ai_analysis"]["is_recommended"] is True
    assert redacted["ai_analysis"]["reason"] == "理由"


def test_redact_record_handles_non_dict():
    assert redact_record(None) == {}
    assert redact_record("not a dict") == {}


# —— 工具行为 ——


def test_search_items_paginates(temp_db):
    async def scenario():
        first = await search_items(keyword="测试关键词", page=1, limit=2)
        second = await search_items(keyword="测试关键词", page=2, limit=2)
        return first, second

    first, second = asyncio.run(scenario())

    assert first["total_items"] == 5
    assert first["page"] == 1
    assert first["limit"] == 2
    assert len(first["items"]) == 2
    assert len(second["items"]) == 2

    first_ids = {item["商品信息"]["商品ID"] for item in first["items"]}
    second_ids = {item["商品信息"]["商品ID"] for item in second["items"]}
    assert first_ids.isdisjoint(second_ids)


def test_search_items_limit_out_of_range(temp_db):
    async def scenario():
        with pytest.raises(ToolError):
            await search_items(keyword="测试关键词", limit=0)
        with pytest.raises(ToolError):
            await search_items(keyword="测试关键词", limit=101)

    asyncio.run(scenario())


def test_search_items_page_must_be_positive(temp_db):
    async def scenario():
        with pytest.raises(ToolError):
            await search_items(keyword="测试关键词", page=0)

    asyncio.run(scenario())


def test_search_items_recommended_filters_are_mutually_exclusive(temp_db):
    """与 REST 层保持一致：两个推荐筛选同时开启时报错。"""
    async def scenario():
        with pytest.raises(ToolError) as excinfo:
            await search_items(
                keyword="测试关键词",
                ai_recommended_only=True,
                keyword_recommended_only=True,
            )
        return str(excinfo.value)

    message = asyncio.run(scenario())
    assert "不能同时开启" in message


def test_search_items_invalid_sort(temp_db):
    async def scenario():
        with pytest.raises(ToolError):
            await search_items(keyword="测试关键词", sort_by="unknown_column")

    asyncio.run(scenario())


def test_search_items_unknown_keyword_returns_empty_not_error(temp_db):
    """查询不存在的关键词应返回空结果，而不是抛异常。"""
    async def scenario():
        return await search_items(keyword="根本不存在的关键词", limit=5)

    result = asyncio.run(scenario())
    assert result["total_items"] == 0
    assert result["items"] == []


def test_search_items_output_is_redacted(temp_db):
    """真实查询路径上的返回结果必须已脱敏。

    注意：断言针对**结构**（敏感键是否存在）与**真实昵称值**是否出现，
    而不是在全文里搜字段名 —— AI 分析的自然语言结论中可能引用
    "卖家发布的商品列表" 这类原始字段名作为描述依据，那不是数据泄露。
    """
    async def scenario():
        return await search_items(keyword="测试关键词", limit=10)

    result = asyncio.run(scenario())
    item = result["items"][0]

    # 结构层面：敏感键不得存在
    assert "卖家昵称" not in item.get("卖家信息", {})
    assert "卖家头像链接" not in item.get("卖家信息", {})
    assert "卖家个性签名" not in item.get("卖家信息", {})
    assert "卖家发布的商品列表" not in item.get("卖家信息", {})
    assert "卖家昵称" not in item.get("商品信息", {})

    # 值层面：真实昵称不得出现
    serialized = json.dumps(result, ensure_ascii=False)
    assert "不该外发的昵称" not in serialized

    # 内部实现细节不得外发
    assert "内部实现细节" not in serialized


def test_list_all_results_reports_stats(temp_db):
    from src.mcp.tools import list_all_results

    async def scenario():
        return await list_all_results()

    result = asyncio.run(scenario())
    assert result["total_result_sets"] == 1
    assert result["total_items"] == 5
    assert result["result_sets"][0]["keyword"] == "测试关键词"


# —— MCP 协议层 ——


def test_mcp_server_rejects_missing_or_wrong_key(temp_db, api_keys):
    """真实 HTTP 传输下：缺 Key / 错 Key 被拒，正确 Key 放行。"""
    import uvicorn

    from src.mcp.server import create_server as _create_server

    server = _create_server()
    port = _free_port()

    async def scenario():
        app = server.http_app()
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="error"
        )
        srv = uvicorn.Server(config)
        task = asyncio.create_task(srv.serve())
        for _ in range(100):
            await asyncio.sleep(0.1)
            if srv.started:
                break
        url = f"http://127.0.0.1:{port}/mcp"

        try:
            # 正确 Key —— 放行并能列出工具
            async with Client(url, auth=TEST_KEY) as client:
                tools = await client.list_tools()
                tool_names = {tool.name for tool in tools}
                assert "search_items" in tool_names
                assert "list_all_results" in tool_names

            # 错误 Key —— 拒绝
            with pytest.raises(Exception):
                async with Client(url, auth="wrong-key") as client:
                    await client.list_tools()

            # 无 Key —— 拒绝
            with pytest.raises(Exception):
                async with Client(url) as client:
                    await client.list_tools()
        finally:
            srv.should_exit = True
            await task

    asyncio.run(scenario())


def test_mcp_server_exposes_health_route(api_keys):
    """健康检查路由应挂载在 /health。"""
    server = create_server()
    app = server.http_app()
    paths = {getattr(route, "path", None) for route in getattr(app, "routes", [])}
    assert "/health" in paths
    assert "/mcp" in paths


def test_create_server_requires_api_keys(monkeypatch):
    """未配置 API Key 时无法创建服务。"""
    monkeypatch.setattr(mcp_settings, "api_keys", "")
    with pytest.raises(MCPConfigurationError):
        create_server()


def test_allowed_tools_whitelist_restricts_exposure(api_keys, monkeypatch):
    """配置白名单后只暴露白名单内的工具。"""
    monkeypatch.setattr(mcp_settings, "allowed_tools", "search_items")
    server = create_server()
    tools = asyncio.run(server.list_tools())
    tool_names = {tool.name for tool in tools}
    assert tool_names == {"search_items"}


def test_mcp_tools_do_not_write_to_database(temp_db):
    """MCP 为只读角色：调用工具不应改动结果库内容。"""
    with sqlite_connection(temp_db) as conn:
        before = conn.execute("SELECT COUNT(*) FROM result_items").fetchone()[0]

    async def scenario():
        from src.mcp.tools import list_all_results

        await list_all_results()
        await search_items(keyword="测试关键词", limit=5)

    asyncio.run(scenario())

    with sqlite_connection(temp_db) as conn:
        after = conn.execute("SELECT COUNT(*) FROM result_items").fetchone()[0]
    assert before == after


def test_database_path_is_resolved_from_environment(monkeypatch, tmp_path):
    """库路径来自 APP_DATABASE_FILE，便于测试与部署隔离。"""
    from src.infrastructure.persistence.sqlite_connection import get_database_path

    target = tmp_path / "custom.sqlite3"
    monkeypatch.setenv("APP_DATABASE_FILE", str(target))
    assert get_database_path() == str(target)


def test_missing_database_table_is_reported_as_tool_error(tmp_path, monkeypatch):
    """表不存在时应返回可读错误，而不是崩溃。"""
    empty_db = tmp_path / "empty.sqlite3"
    sqlite3.connect(str(empty_db)).close()
    monkeypatch.setenv("APP_DATABASE_FILE", str(empty_db))

    async def scenario():
        from src.mcp.tools import list_all_results

        with pytest.raises(ToolError):
            await list_all_results()

    asyncio.run(scenario())

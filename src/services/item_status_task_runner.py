"""
独立状态巡检执行器。

供 Web UI 手动触发一次商品状态复核：自己起一个带登录态的浏览器，只跑巡检，
不跑搜索与 AI 分析。

⚠️ 这个入口会额外向闲鱼发请求，和主抓取共用同一个账号，
默认关闭（需 ``ITEM_STATUS_MANUAL_CHECK_ENABLED=true`` 显式开启）。
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Optional

from playwright.async_api import async_playwright

from src.config import STATE_FILE
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.rotation import load_state_files
from src.services.account_strategy_service import resolve_account_runtime_plan
from src.services.browser_session import create_logged_in_context
from src.services.item_status_rechecker import run_status_recheck
from src.scraper import _resolve_browser_channel, fetch_item_detail


class StatusCheckBusyError(RuntimeError):
    """已有一次巡检在跑，避免同时开多个浏览器。"""


# 同一时间只允许一次手动巡检，避免并发开浏览器把账号风控打满。
_RUNNING = False
_RUN_LOCK = asyncio.Lock()


def is_manual_status_check_enabled() -> bool:
    raw = os.getenv("ITEM_STATUS_MANUAL_CHECK_ENABLED")
    if raw is None or not str(raw).strip():
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def is_status_reset_enabled() -> bool:
    """是否开放「手动重置商品状态」接口。

    重置比复核更具破坏性（会清空成交价、删除状态事件），所以用独立开关，
    默认关闭。
    """
    raw = os.getenv("ITEM_STATUS_RESET_ENABLED")
    if raw is None or not str(raw).strip():
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


async def resolve_state_file(keyword: str) -> str:
    """按关键词找到任务对应的登录状态文件。"""
    state_dir = os.getenv("ACCOUNT_STATE_DIR", "state").strip().strip('"').strip("'")
    account_files = load_state_files(state_dir)

    try:
        repository = SqliteTaskRepository()
        tasks = await repository.find_all()
    except Exception:
        tasks = []

    target = (keyword or "").strip().lower()
    for task in tasks:
        if (task.keyword or "").strip().lower() != target:
            continue
        runtime_plan = resolve_account_runtime_plan(
            strategy=task.account_strategy,
            account_state_file=task.account_state_file,
            has_root_state_file=os.path.exists(STATE_FILE),
            available_account_files=account_files,
        )
        forced_account = runtime_plan.get("forced_account")
        if forced_account and os.path.exists(forced_account):
            return forced_account
        if runtime_plan.get("prefer_root_state") and os.path.exists(STATE_FILE):
            return STATE_FILE
        if account_files:
            return account_files[0]

    if os.path.exists(STATE_FILE):
        return STATE_FILE
    if account_files:
        return account_files[0]
    raise FileNotFoundError("未找到可用的登录状态文件，无法执行状态复核。")


async def run_standalone_status_check(
    *,
    keyword: str,
    result_filename: str,
    run_id: Optional[str] = None,
    limit: Optional[int] = None,
    max_seconds: Optional[int] = None,
) -> dict:
    """起一个浏览器，只跑一次状态复核。

    ``limit`` / ``max_seconds`` 为 None 时沿用环境变量配置（默认 20 条 / 300 秒）。
    存量数据清理后需要一次性重判被重置的全部记录时，可以把 ``limit`` 调大；
    **时间预算不建议放开**，避免把账号打到风控。
    """
    global _RUNNING

    async with _RUN_LOCK:
        if _RUNNING:
            raise StatusCheckBusyError("已有一次状态复核在执行中，请稍后再试。")
        _RUNNING = True

    try:
        state_file = await resolve_state_file(keyword)
        effective_run_id = run_id or datetime.now().strftime("%Y%m%d%H%M%S")

        async with async_playwright() as playwright:
            browser, context = await create_logged_in_context(
                playwright,
                state_file,
                channel=_resolve_browser_channel(),
            )
            try:
                page = await context.new_page()
                # 先访问首页，让页面把 mtop 脚本准备好，巡检才能走直调通道
                await page.goto(
                    "https://www.goofish.com/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                summary = await run_status_recheck(
                    context=context,
                    page=page,
                    keyword=keyword,
                    result_filename=result_filename,
                    run_id=effective_run_id,
                    fetch_detail=fetch_item_detail,
                    limit=limit,
                    max_seconds=max_seconds,
                )
            finally:
                await browser.close()

        return {"run_id": effective_run_id, **summary}
    finally:
        async with _RUN_LOCK:
            _RUNNING = False

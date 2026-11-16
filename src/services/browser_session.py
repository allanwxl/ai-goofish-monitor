"""
浏览器会话与登录态上下文的公共封装。

主抓取链路（``src/scraper.py``）与「手动触发状态巡检」共用同一套启动参数与
登录态还原逻辑，避免两处实现漂移后行为不一致。

这里的函数保持与原来 ``src/scraper.py`` 中完全一致的实现，只做搬家不做改动。
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Optional

from src import config

CHROMIUM_CRASH_DUMPS_DIRNAME = "chromium-crashpad"


def build_chromium_launch_args(crash_dumps_dir: str) -> list[str]:
    """Chromium 启动参数。

    Docker 里的 Playwright Chromium 会拉起 chrome_crashpad_handler。数据库路径一旦落到
    ``/``，handler 会自监控重启并占住大量内存。这里禁用 crash reporter，同时把 dump
    目录固定到可写临时目录。
    """
    return [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-web-security",
        "--disable-breakpad",
        "--disable-crash-reporter",
        "--disable-features=IsolateOrigins,site-per-process,Crashpad",
        f"--crash-dumps-dir={crash_dumps_dir}",
    ]


def prepare_chromium_launch_args() -> list[str]:
    """Chromium 启动参数（把 dump 目录固定到临时目录）。"""
    crash_dumps_dir = os.path.join(tempfile.gettempdir(), CHROMIUM_CRASH_DUMPS_DIRNAME)
    os.makedirs(crash_dumps_dir, exist_ok=True)
    return build_chromium_launch_args(crash_dumps_dir)


def _clean_kwargs(options: dict) -> dict:
    return {k: v for k, v in options.items() if v is not None}


def _default_context_options() -> dict:
    return {
        "user_agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "permissions": ["geolocation"],
        "geolocation": {"longitude": 121.4737, "latitude": 31.2304},
        "color_scheme": "light",
    }


def _looks_like_mobile(ua: str) -> Optional[bool]:
    if not ua:
        return None
    ua_lower = ua.lower()
    if "mobile" in ua_lower or "android" in ua_lower or "iphone" in ua_lower:
        return True
    if "windows" in ua_lower or "macintosh" in ua_lower:
        return False
    return None


def _build_context_overrides(snapshot: dict) -> dict:
    env = snapshot.get("env") or {}
    headers = snapshot.get("headers") or {}
    navigator = env.get("navigator") or {}
    screen = env.get("screen") or {}
    intl = env.get("intl") or {}

    overrides = {}

    ua = (
        headers.get("User-Agent")
        or headers.get("user-agent")
        or navigator.get("userAgent")
    )
    if ua:
        overrides["user_agent"] = ua

    accept_language = headers.get("Accept-Language") or headers.get("accept-language")
    locale = None
    if accept_language:
        locale = accept_language.split(",")[0].strip()
    elif navigator.get("language"):
        locale = navigator["language"]
    if locale:
        overrides["locale"] = locale

    tz = intl.get("timeZone")
    if tz:
        overrides["timezone_id"] = tz

    width = screen.get("width")
    height = screen.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)):
        overrides["viewport"] = {"width": int(width), "height": int(height)}

    dpr = screen.get("devicePixelRatio")
    if isinstance(dpr, (int, float)):
        overrides["device_scale_factor"] = float(dpr)

    touch_points = navigator.get("maxTouchPoints")
    if isinstance(touch_points, (int, float)):
        overrides["has_touch"] = touch_points > 0

    mobile_flag = _looks_like_mobile(ua or "")
    if mobile_flag is not None:
        overrides["is_mobile"] = mobile_flag

    return _clean_kwargs(overrides)


# 这些头由浏览器按请求类型生成。扩展快照里常见的是某次 XHR 的值
# （Sec-Fetch-Dest: empty / Sec-Fetch-Mode: cors / Accept: */*）。
# 如果原样套到 BrowserContext，文档和脚本请求会被改成 CORS 空请求，搜索页只剩白屏。
_BROWSER_MANAGED_EXTRA_HEADERS = {
    "accept",
    "accept-encoding",
    "authorization",
    "connection",
    "content-length",
    "content-type",
    "cookie",
    "host",
    "origin",
    "proxy-authorization",
    "referer",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "sec-fetch-user",
    "upgrade-insecure-requests",
    "user-agent",
}


def _build_extra_headers(raw_headers: Optional[dict]) -> dict:
    if not raw_headers:
        return {}
    headers = {}
    for key, value in raw_headers.items():
        if not key or key.lower() in _BROWSER_MANAGED_EXTRA_HEADERS or value is None:
            continue
        headers[key] = value
    return headers


def read_state_snapshot(state_file: str) -> Any:
    """读取登录状态文件；失败时返回 None，按路径直接使用。"""
    if not os.path.exists(state_file):
        raise FileNotFoundError(f"登录状态文件不存在: {state_file}")
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"警告：读取登录状态文件失败，将直接按路径使用: {e}")
        return None


async def create_logged_in_context(
    playwright,
    state_file: str,
    *,
    channel: str,
    proxy_server: Optional[str] = None,
):
    """启动浏览器并创建带登录态的上下文，返回 ``(browser, context)``。

    扩展导出的增强快照会还原 UA、时区、视口等环境参数与额外请求头。
    """
    snapshot_data = read_state_snapshot(state_file)

    launch_args = prepare_chromium_launch_args()
    launch_kwargs: dict[str, Any] = {
        "headless": config.RUN_HEADLESS,
        "args": launch_args,
    }
    if proxy_server:
        launch_kwargs["proxy"] = {"server": proxy_server}
    launch_kwargs["channel"] = channel

    browser = await playwright.chromium.launch(**launch_kwargs)

    context_kwargs = _default_context_options()
    storage_state_arg = state_file
    if isinstance(snapshot_data, dict):
        if any(key in snapshot_data for key in ("env", "headers", "page", "storage")):
            print(f"检测到增强浏览器快照，应用环境参数: {state_file}")
            storage_state_arg = {"cookies": snapshot_data.get("cookies", [])}
            context_kwargs.update(_build_context_overrides(snapshot_data))
            extra_headers = _build_extra_headers(snapshot_data.get("headers"))
            if extra_headers:
                context_kwargs["extra_http_headers"] = extra_headers
        else:
            storage_state_arg = snapshot_data
    context_kwargs = _clean_kwargs(context_kwargs)
    context = await browser.new_context(storage_state=storage_state_arg, **context_kwargs)
    return browser, context

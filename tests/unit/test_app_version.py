"""应用版本号：解析优先级、版本接口，以及与界面徽标的契约。

版本号的作用就一件事：升级之后一眼看出「新版本到底生效没有」，
所以解析结果与界面取数路径都必须可靠。
"""
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import version as version_routes
from src.infrastructure.config import version as version_module


PROJECT_ROOT = Path(__file__).resolve().parents[2]
THE_HEADER_VUE = (
    PROJECT_ROOT / "web-ui" / "src" / "components" / "layout" / "TheHeader.vue"
)
VERSION_API_TS = PROJECT_ROOT / "web-ui" / "src" / "api" / "version.ts"


@pytest.fixture(autouse=True)
def _clear_version_cache():
    """版本解析带缓存，每个用例前后都要清掉，否则环境变量改了也不生效。"""
    version_module.resolve_app_version.cache_clear()
    version_module.resolve_commit_hash.cache_clear()
    yield
    version_module.resolve_app_version.cache_clear()
    version_module.resolve_commit_hash.cache_clear()


# ── 解析优先级 ────────────────────────────────────────────────────────


def test_version_prefers_environment_variable(monkeypatch):
    """Docker 镜像里没有 .git，APP_VERSION 是唯一可靠来源，优先级最高。"""
    monkeypatch.setenv("APP_VERSION", "2.5-18-gcb0e4cb")
    monkeypatch.setattr(version_module, "_read_version_file", lambda: "9.9.9")
    monkeypatch.setattr(version_module, "_run_git", lambda args: "1.0-1-gdeadbee")

    assert version_module.resolve_app_version() == "2.5-18-gcb0e4cb"


def test_version_falls_back_to_version_file(monkeypatch, tmp_path):
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.setattr(version_module, "project_root", lambda: tmp_path)
    (tmp_path / "VERSION").write_text("2.6\n", encoding="utf-8")
    monkeypatch.setattr(version_module, "_run_git", lambda args: "1.0-1-gdeadbee")

    assert version_module.resolve_app_version() == "2.6"


def test_version_falls_back_to_git_describe(monkeypatch):
    """源码部署（git pull 后直接跑）时，git describe 最准。"""
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.setattr(version_module, "_read_version_file", lambda: "")
    monkeypatch.setattr(version_module, "_run_git", lambda args: "2.5-18-gcb0e4cb")

    assert version_module.resolve_app_version() == "2.5-18-gcb0e4cb"


def test_version_is_unknown_when_nothing_available(monkeypatch):
    """宁可显示 unknown，也不显示一个假的版本号。"""
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.setattr(version_module, "_read_version_file", lambda: "")
    monkeypatch.setattr(version_module, "_run_git", lambda args: "")

    assert version_module.resolve_app_version() == version_module.UNKNOWN_VERSION


def test_commit_hash_prefers_environment_variable(monkeypatch):
    monkeypatch.setenv("APP_COMMIT", "cb0e4cb")
    monkeypatch.setattr(version_module, "_run_git", lambda args: "deadbee")

    assert version_module.resolve_commit_hash() == "cb0e4cb"


# ── 版本接口 ──────────────────────────────────────────────────────────


def test_version_endpoint_returns_version_and_commit(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "2.5-18-gcb0e4cb")
    monkeypatch.setenv("APP_COMMIT", "cb0e4cb")

    app = FastAPI()
    app.include_router(version_routes.router)
    client = TestClient(app)

    resp = client.get("/api/version")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["version"] == "2.5-18-gcb0e4cb"
    assert payload["commit"] == "cb0e4cb"


# ── 与界面的契约 ──────────────────────────────────────────────────────


def test_frontend_fetches_the_same_endpoint_as_the_backend():
    source = VERSION_API_TS.read_text(encoding="utf-8")
    match = re.search(r"http\('([^']+)'\)", source)
    assert match, "前端没有调用版本接口"

    app = FastAPI()
    app.include_router(version_routes.router)
    backend_paths = {
        route.path for route in version_routes.router.routes if hasattr(route, "path")
    }
    assert match.group(1) in backend_paths


def test_header_shows_version_instead_of_pro_badge():
    """顶部徽标显示版本号（不再是 PRO），并保留版本号取数逻辑。"""
    source = THE_HEADER_VUE.read_text(encoding="utf-8")

    assert "PRO" not in source
    assert "fetchAppVersion" in source
    assert "appVersion" in source
    assert "versionLabel" in source

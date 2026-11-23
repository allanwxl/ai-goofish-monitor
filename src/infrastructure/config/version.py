"""应用版本解析。

界面顶部的版本徽标靠它回答一个问题：**「我这次升级到底生效没有」**。

解析优先级（从可靠到兜底）：

1. 环境变量 ``APP_VERSION``：Docker 构建时由 CI 注入。镜像里没有 ``.git``，
   这是唯一可靠的来源。
2. 仓库根目录的 ``VERSION`` 文件：给手动打包、离线分发用的兜底。
3. ``git describe --tags --always --dirty``：源码部署（``git pull`` 后直接跑）
   时最准，能看出离最近一个 tag 差几个提交、当前是不是脏工作区。
4. 兜底 ``"unknown"``：宁可显示未知，也不显示一个假的版本号。
"""
from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path


UNKNOWN_VERSION = "unknown"
VERSION_ENV_KEY = "APP_VERSION"
COMMIT_ENV_KEY = "APP_COMMIT"
VERSION_FILE_NAME = "VERSION"
GIT_TIMEOUT_SECONDS = 5


def project_root() -> Path:
    """仓库根目录（src/infrastructure/config/version.py 往上三层）。"""
    return Path(__file__).resolve().parents[3]


def _run_git(args: list[str]) -> str:
    """跑一条 git 命令，失败返回空串（没装 git / 不是 git 仓库 / 超时都不算错误）。"""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(project_root()),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _read_version_file() -> str:
    version_file = project_root() / VERSION_FILE_NAME
    if not version_file.is_file():
        return ""
    try:
        return version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@lru_cache(maxsize=1)
def resolve_app_version() -> str:
    """当前运行的版本号（同一进程内只解析一次）。"""
    env_version = (os.getenv(VERSION_ENV_KEY) or "").strip()
    if env_version:
        return env_version

    file_version = _read_version_file()
    if file_version:
        return file_version

    described = _run_git(["describe", "--tags", "--always", "--dirty"])
    if described:
        return described

    return UNKNOWN_VERSION


@lru_cache(maxsize=1)
def resolve_commit_hash() -> str:
    """当前运行的提交短哈希（拿不到时返回空串）。"""
    env_commit = (os.getenv(COMMIT_ENV_KEY) or "").strip()
    if env_commit:
        return env_commit

    return _run_git(["rev-parse", "--short", "HEAD"])


def version_info() -> dict:
    """给接口用的版本信息。"""
    return {
        "version": resolve_app_version(),
        "commit": resolve_commit_hash(),
    }

"""熔断自动恢复的 cookie 路径一致性测试。

背景：
  record_failure 记录 cookie_mtime 时用的是"当时实际使用的那个登录态文件"
  （scraper 传入的 last_state_path，例如账号轮换选中的 state/jidan.json）；
  而 should_skip_start 之前拿"调用方传入的 cookie_path"去和这个 mtime 比较
  （任务配置里的 account_state_file，或默认的 state/xianyu_state.json）。

  两者不是同一个文件时：
    - 传入路径不存在 -> cookie_path 为 None -> 自动恢复永远不触发
    - 传入路径存在但更旧 -> 永远检测不到登录态更新
    - 传入路径存在但更新 -> 误判为"已恢复"，实际登录态没动
"""

from datetime import datetime, timedelta, timezone
import os

from src.failure_guard import FailureGuard


def _write_guard(path, task_key, *, cookie_path, cookie_mtime):
    payload = {
        "version": 1,
        "tasks": {
            task_key: {
                "consecutive_failures": 3,
                "cookie_mtime": cookie_mtime,
                "cookie_path": cookie_path,
                "last_failure_at": "2026-11-09T10:00:00+08:00",
                "last_failure_reason": "baxia-dialog",
                "paused_until": (
                    datetime.now(timezone(timedelta(hours=8))) + timedelta(hours=6)
                ).isoformat(),
            }
        },
    }
    import json

    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_uses_recorded_cookie_path_not_the_one_passed_in(tmp_path):
    """实际使用的登录态文件被更新后，即使传入的是另一个路径也应自动恢复。"""
    recorded = tmp_path / "jidan.json"
    recorded.write_text("{}", encoding="utf-8")
    other = tmp_path / "xianyu_state.json"
    other.write_text("{}", encoding="utf-8")

    guard_file = tmp_path / "task-failure-guard.json"
    # 失败时记录的 mtime 是 recorded 的旧时间
    old_mtime = recorded.stat().st_mtime - 60
    _write_guard(
        guard_file,
        "小米17promax",
        cookie_path=str(recorded),
        cookie_mtime=old_mtime,
    )
    # 随后用户更新了 recorded（重新扫码导出）
    recorded.write_text('{"cookies": []}', encoding="utf-8")

    guard = FailureGuard(path=str(guard_file))
    # 调用方传入的是另一个路径（任务配置里的 account_state_file）
    decision = guard.should_skip_start("小米17promax", cookie_path=str(other))

    assert decision.skip is False
    assert decision.reason == "cookie_updated"
    assert decision.consecutive_failures == 0


def test_falls_back_to_passed_path_when_entry_has_none(tmp_path):
    """老守卫文件没有 cookie_path 字段时，回退到调用方传入的路径。"""
    cookie = tmp_path / "state.json"
    cookie.write_text("{}", encoding="utf-8")

    guard_file = tmp_path / "task-failure-guard.json"
    _write_guard(
        guard_file,
        "任务X",
        cookie_path=None,
        cookie_mtime=cookie.stat().st_mtime - 60,
    )
    cookie.write_text('{"cookies": []}', encoding="utf-8")

    guard = FailureGuard(path=str(guard_file))
    decision = guard.should_skip_start("任务X", cookie_path=str(cookie))
    assert decision.skip is False
    assert decision.reason == "cookie_updated"


def test_does_not_falsely_recover_when_recorded_file_untouched(tmp_path):
    """实际使用的文件没动过，就不该判定为已恢复（避免误放行）。"""
    recorded = tmp_path / "jidan.json"
    recorded.write_text("{}", encoding="utf-8")
    # 把实际使用的文件的 mtime 固定在过去，模拟"失败后一直没动过"
    past = recorded.stat().st_mtime - 3600
    os.utime(recorded, (past, past))

    other = tmp_path / "xianyu_state.json"
    # 另一个文件被更新了，但实际使用的 recorded 没动
    other.write_text('{"cookies": []}', encoding="utf-8")

    guard_file = tmp_path / "task-failure-guard.json"
    _write_guard(
        guard_file,
        "小米17promax",
        cookie_path=str(recorded),
        cookie_mtime=past,
    )

    guard = FailureGuard(path=str(guard_file))
    decision = guard.should_skip_start("小米17promax", cookie_path=str(other))

    assert decision.skip is True
    assert decision.reason == "baxia-dialog"


def test_no_cookie_path_at_all_still_skips_without_crashing(tmp_path):
    """既没有记录路径也没传入路径时，安全地保持熔断。"""
    guard_file = tmp_path / "task-failure-guard.json"
    _write_guard(guard_file, "任务Y", cookie_path=None, cookie_mtime=None)

    guard = FailureGuard(path=str(guard_file))
    decision = guard.should_skip_start("任务Y")
    assert decision.skip is True

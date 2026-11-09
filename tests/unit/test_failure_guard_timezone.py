"""守卫文件时区健壮性测试。

背景：
  logs/task-failure-guard.json 是可以直接手工编辑的运维文件。
  如果里面的 paused_until 没有时区偏移（例如用编辑器改过、或来自旧版本），
  naive 与 aware 的 datetime 直接比较会抛 TypeError，
  导致整个任务启动接口 500 —— 必须保证任何格式都能安全解析。
"""

import json
from datetime import datetime, timedelta, timezone

from src.failure_guard import FailureGuard


def _write_guard(path, task_key, paused_until_value):
    payload = {
        "version": 1,
        "tasks": {
            task_key: {
                "consecutive_failures": 3,
                "cookie_mtime": None,
                "cookie_path": None,
                "last_failure_at": "2026-11-09T10:00:00+08:00",
                "last_failure_reason": "baxia-dialog",
                "paused_until": paused_until_value,
            }
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_naive_paused_until_does_not_crash_with_aware_now(tmp_path):
    """守卫文件里是无时区字符串时，不能因为比较而抛 TypeError。"""
    guard_file = tmp_path / "task-failure-guard.json"
    _write_guard(guard_file, "任务A", "2026-11-10T08:30:00")

    guard = FailureGuard(path=str(guard_file))
    decision = guard.should_skip_start("任务A")
    assert decision.skip is True
    assert decision.reason == "baxia-dialog"
    assert decision.consecutive_failures == 3


def test_aware_paused_until_still_works(tmp_path):
    """带时区的正常格式行为不变。"""
    guard_file = tmp_path / "task-failure-guard.json"
    _write_guard(guard_file, "任务B", "2026-11-10T08:30:00+08:00")

    guard = FailureGuard(path=str(guard_file))
    decision = guard.should_skip_start("任务B")
    assert decision.skip is True
    assert decision.paused_until is not None


def test_expired_naive_paused_until_allows_start(tmp_path):
    """已过期且无时区的时间应当放行，而不是崩掉。"""
    guard_file = tmp_path / "task-failure-guard.json"
    past = (datetime.now() - timedelta(hours=1)).replace(tzinfo=None)
    _write_guard(guard_file, "任务C", past.isoformat())

    guard = FailureGuard(path=str(guard_file))
    decision = guard.should_skip_start("任务C")
    assert decision.skip is False


def test_record_failure_with_naive_existing_entry_does_not_crash(tmp_path):
    """已有条目是无时区格式时，再次记录失败也不能崩。"""
    guard_file = tmp_path / "task-failure-guard.json"
    _write_guard(guard_file, "任务D", "2026-11-10T08:30:00")

    guard = FailureGuard(path=str(guard_file))
    result = guard.record_failure("任务D", "baxia-dialog")
    assert result["consecutive_failures"] == 4


def test_align_timezone_keeps_matching_awareness():
    aware = datetime(2026, 11, 10, 8, 30, tzinfo=timezone(timedelta(hours=8)))
    naive = datetime(2026, 11, 10, 8, 30)

    aligned = FailureGuard._align_timezone(naive, aware)
    assert aligned is not None
    assert aligned.tzinfo is not None
    assert aligned == aware

    aligned_back = FailureGuard._align_timezone(aware, naive)
    assert aligned_back is not None
    assert aligned_back.tzinfo is None

    assert FailureGuard._align_timezone(None, aware) is None

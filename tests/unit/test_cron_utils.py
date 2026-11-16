import re
from datetime import datetime
from pathlib import Path

from src.core.cron_utils import (
    build_cron_trigger,
    normalize_cron_expression,
    validate_cron_expression,
)

# 仓库根目录（tests/unit/test_cron_utils.py -> 上两级）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASK_FORM_VUE = PROJECT_ROOT / "web-ui" / "src" / "components" / "tasks" / "TaskForm.vue"

# 2026-01-05 是周一，作为采样起点可以覆盖完整的一周
_SAMPLE_START = datetime(2026, 1, 5, 0, 0)


def _weekdays_of(expression: str, limit: int = 40) -> set:
    """取出一个 cron 实际会命中的星期集合（0=周一 ... 6=周日）。"""
    trigger = build_cron_trigger(expression, timezone="Asia/Shanghai")
    current = _SAMPLE_START
    previous = None
    weekdays = set()
    for _ in range(limit):
        nxt = trigger.get_next_fire_time(previous, current)
        if nxt is None:
            break
        weekdays.add(nxt.weekday())
        previous = nxt
        current = nxt
    return weekdays


def _extract_frontend_presets() -> dict:
    """从前端 TaskForm.vue 的 cron 预设列表里解析出 {i18n key: cron}。"""
    source = TASK_FORM_VUE.read_text(encoding="utf-8")
    pattern = re.compile(
        r"\{\s*value:\s*'(?P<cron>[^']+)'\s*,\s*label:\s*t\('tasks\.form\.cron\.(?P<key>\w+)'\)"
    )
    return {match.group("key"): match.group("cron") for match in pattern.finditer(source)}


def test_validate_cron_expression_normalizes_alias():
    assert validate_cron_expression("@daily") == "0 0 * * *"


def test_validate_cron_expression_accepts_six_fields():
    assert validate_cron_expression("0 0 8 * * *") == "0 0 8 * * *"


def test_build_cron_trigger_accepts_alias_and_timezone():
    trigger = build_cron_trigger("@hourly", timezone="Asia/Shanghai")

    assert trigger is not None
    assert str(trigger.timezone) == "Asia/Shanghai"


def test_validate_cron_expression_rejects_invalid_value():
    try:
        validate_cron_expression("not-a-cron")
    except ValueError as exc:
        assert "支持 5 段" in str(exc)
        return

    raise AssertionError("非法 cron 应该抛出 ValueError")


def test_weekday_field_follows_apscheduler_convention():
    """周字段是 APScheduler 约定（0=周一 ... 6=周日），不是标准 crontab 的 0=周日。

    调度器走的是 CronTrigger.from_crontab，它不会把标准 crontab 的周字段
    转换成 APScheduler 的约定，直接按 0=周一 解释。
    """
    assert _weekdays_of("0 3 * * 0") == {0}
    assert _weekdays_of("0 3 * * 6") == {6}


def test_weekly_alias_fires_on_sunday():
    """@weekly 沿用标准 crontab 语义 = 周日 00:00。"""
    assert normalize_cron_expression("@weekly") == "0 0 * * 6"
    assert _weekdays_of("@weekly") == {6}


def test_frontend_cron_presets_match_their_weekday_intent():
    """前端 cron 预设的周字段必须真的落在标签宣称的日期上。

    历史上出现过「标签写工作日、实际跑周二到周六」的问题，原因是预设按
    标准 crontab 写的周字段，而调度器用的是 APScheduler 约定。这里直接读
    前端文件做校验，预设再被改错会立刻失败。
    """
    presets = _extract_frontend_presets()
    expectations = {
        "weekday9": {0, 1, 2, 3, 4},  # 工作日 = 周一至周五
        "weekend10": {5, 6},          # 周末 = 周六、周日
    }

    for key, expected in expectations.items():
        assert key in presets, f"前端缺少 cron 预设 {key}"
        actual = _weekdays_of(presets[key])
        assert actual == expected, (
            f"cron 预设 {key}（{presets[key]}）实际命中 {sorted(actual)}，"
            f"应为 {sorted(expected)}"
        )


def test_frontend_cron_presets_have_no_stale_weekday_expressions():
    """旧的预设写法（1-5 / 6,0）按标准 crontab 语义理解，与调度器不符，不应再出现。"""
    presets = _extract_frontend_presets()

    assert presets.get("weekday9") != "0 9 * * 1-5"
    assert presets.get("weekend10") != "0 10 * * 6,0"

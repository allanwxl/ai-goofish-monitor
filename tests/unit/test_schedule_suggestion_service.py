"""
定时建议服务的单元测试
"""
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional

import pytest

from src.domain.models.task import (
    DEFAULT_SCHEDULE_FREQUENCY,
    SCHEDULE_FREQUENCY_DAILY,
    SCHEDULE_FREQUENCY_EVERY_2_DAYS,
    SCHEDULE_FREQUENCY_EVERY_3_DAYS,
    SCHEDULE_FREQUENCY_WEEKLY,
    Task,
)
from src.services.schedule_suggestion_service import (
    CONFLICT_WINDOW_MINUTES,
    FALLBACK_HOURS,
    FALLBACK_MINUTES,
    FREQUENCY_LOOKAHEAD_HOURS,
    BusySlot,
    ScheduleSuggestionError,
    ScheduleSuggestionService,
    cron_matches_frequency,
    normalize_frequency,
)


def make_task(
    task_id: int,
    name: str,
    cron: Optional[str],
    enabled: bool = True,
) -> Task:
    return Task(
        id=task_id,
        task_name=name,
        enabled=enabled,
        keyword="kw",
        max_pages=3,
        personal_only=True,
        ai_prompt_base_file="prompts/base_prompt.txt",
        ai_prompt_criteria_file="prompts/test_criteria.txt",
        cron=cron,
    )


class FakeAIClient:
    """按预设脚本返回内容的假 AI 客户端。"""

    def __init__(self, responses: List[str], error: Optional[Exception] = None):
        self._responses = list(responses)
        self._error = error
        self.prompts: List[str] = []

    def is_available(self) -> bool:
        return True

    def refresh(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def _call_ai(self, messages, **kwargs) -> str:
        self.prompts.append(messages[0]["content"])
        if self._error is not None:
            raise self._error
        if not self._responses:
            return ""
        return self._responses.pop(0)


@pytest.fixture
def service() -> ScheduleSuggestionService:
    return ScheduleSuggestionService()


@pytest.fixture
def now(service: ScheduleSuggestionService) -> datetime:
    return datetime.now(service.timezone)


def test_collect_existing_schedules_skips_disabled_and_excluded(
    service: ScheduleSuggestionService, now: datetime
):
    tasks = [
        make_task(1, "启用任务", "0 8 * * *"),
        make_task(2, "禁用任务", "0 9 * * *", enabled=False),
        make_task(3, "无定时任务", None),
    ]

    schedules = service.collect_existing_schedules(tasks, now=now)
    assert [s.task_name for s in schedules] == ["启用任务"]

    schedules = service.collect_existing_schedules(tasks, exclude_task_id=1, now=now)
    assert schedules == []


def test_collect_fire_times_has_no_duplicates(
    service: ScheduleSuggestionService, now: datetime
):
    fires = service.collect_fire_times("0 8 * * *", now)
    assert len(fires) == len(set(fires))
    assert fires[0].strftime("%H:%M") == "08:00"


def test_collect_fire_times_respects_sampling_limit(
    service: ScheduleSuggestionService, now: datetime
):
    fires = service.collect_fire_times("*/1 * * * *", now, limit=10)
    assert len(fires) == 10


def test_collect_fire_times_returns_empty_for_invalid_cron(
    service: ScheduleSuggestionService, now: datetime
):
    assert service.collect_fire_times("not a cron", now) == []
    assert service.collect_fire_times(None, now) == []


def test_find_conflicts_respects_window(
    service: ScheduleSuggestionService, now: datetime
):
    # 已占用时刻取未来的那一次，否则今天的 08:00 已经过去了，构不成冲突
    busy_fire = service.collect_fire_times("0 8 * * *", now)[0]
    busy = [BusySlot(when=busy_fire, task_name="A")]

    same_time = service.collect_fire_times("0 8 * * *", now)
    assert service.find_conflicts(same_time, busy)

    within_window = service.collect_fire_times(
        f"{CONFLICT_WINDOW_MINUTES - 1} 8 * * *", now
    )
    assert service.find_conflicts(within_window, busy)

    outside_window = service.collect_fire_times(
        f"{CONFLICT_WINDOW_MINUTES + 5} 8 * * *", now
    )
    assert service.find_conflicts(outside_window, busy) == []


def test_parse_suggestion_variants(service: ScheduleSuggestionService):
    assert service.parse_suggestion("17 3 * * *") == ("17 3 * * *", "")

    assert service.parse_suggestion("推荐： `17 3 * * *`")[0] == "17 3 * * *"

    assert service.parse_suggestion("cron: 17 3 * * *")[0] == "17 3 * * *"

    cron, reason = service.parse_suggestion(
        '{"cron": "23 4 * * *", "reason": "避开整点"}'
    )
    assert cron == "23 4 * * *"
    assert reason == "避开整点"

    assert service.parse_suggestion("我不知道") == (None, "")


def test_suggest_returns_ai_cron_when_no_conflict(now: datetime):
    ai_client = FakeAIClient(["17 3 * * *"])
    service = ScheduleSuggestionService(ai_client=ai_client)
    tasks = [make_task(1, "MacBook", "0 8 * * *")]

    suggestion = asyncio.run(
        service.suggest(
            tasks=tasks,
            task_name="索尼A7M4",
            keyword="a7m4",
            frequency=SCHEDULE_FREQUENCY_DAILY,
            now=now,
        )
    )

    assert suggestion.cron == "17 3 * * *"
    assert suggestion.source == "ai"
    assert suggestion.considered_tasks == 1
    assert suggestion.avoided == ['任务「MacBook」cron=0 8 * * *']
    assert "MacBook" in ai_client.prompts[0]


def test_suggest_retries_when_ai_returns_conflicting_cron(now: datetime):
    ai_client = FakeAIClient(["0 8 * * *", "17 3 * * *"])
    service = ScheduleSuggestionService(ai_client=ai_client)
    tasks = [make_task(1, "MacBook", "0 8 * * *")]

    suggestion = asyncio.run(
        service.suggest(
            tasks=tasks,
            task_name="新任务",
            frequency=SCHEDULE_FREQUENCY_DAILY,
            now=now,
        )
    )

    assert suggestion.cron == "17 3 * * *"
    assert suggestion.source == "ai"
    assert len(ai_client.prompts) == 2
    # 第二轮提示里必须带上冲突反馈，让模型知道要避开哪个时刻
    assert "08:00" in ai_client.prompts[1]


def test_suggest_retries_when_ai_returns_invalid_cron(now: datetime):
    ai_client = FakeAIClient(["这不是 cron", "17 3 * * *"])
    service = ScheduleSuggestionService(ai_client=ai_client)

    suggestion = asyncio.run(
        service.suggest(
            tasks=[],
            task_name="新任务",
            frequency=SCHEDULE_FREQUENCY_DAILY,
            now=now,
        )
    )

    assert suggestion.cron == "17 3 * * *"
    assert suggestion.source == "ai"
    assert len(ai_client.prompts) == 2


def test_suggest_falls_back_when_ai_always_conflicts(now: datetime):
    ai_client = FakeAIClient(["0 8 * * *", "0 8 * * *", "0 8 * * *"])
    service = ScheduleSuggestionService(ai_client=ai_client)
    tasks = [make_task(1, "MacBook", "0 8 * * *")]

    suggestion = asyncio.run(
        service.suggest(tasks=tasks, task_name="新任务", now=now)
    )

    assert suggestion.source == "fallback"
    assert suggestion.cron
    # 兜底结果必须真的不冲突
    schedules = service.collect_existing_schedules(tasks, now=now)
    busy = service._busy_slots(schedules)
    fires = service.collect_fire_times(suggestion.cron, now)
    assert service.find_conflicts(fires, busy) == []


def test_suggest_falls_back_when_ai_call_fails(now: datetime):
    ai_client = FakeAIClient([], error=RuntimeError("网关超时"))
    service = ScheduleSuggestionService(ai_client=ai_client)

    suggestion = asyncio.run(
        service.suggest(tasks=[], task_name="新任务", now=now)
    )

    assert suggestion.source == "fallback"
    assert "网关超时" in suggestion.reason


def test_suggest_raises_when_ai_unavailable(now: datetime):
    class UnavailableAIClient(FakeAIClient):
        def is_available(self) -> bool:
            return False

    service = ScheduleSuggestionService(ai_client=UnavailableAIClient([]))

    with pytest.raises(ScheduleSuggestionError):
        asyncio.run(service.suggest(tasks=[], task_name="新任务", now=now))


def test_find_free_slot_returns_none_when_all_slots_taken(service, now: datetime):
    """所有低峰时段都被占满时，找不到空闲时间点。"""
    busy = [
        BusySlot(
            when=(now + timedelta(days=1)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            ),
            task_name="占位任务",
        )
        for hour in FALLBACK_HOURS
        for minute in FALLBACK_MINUTES
    ]

    assert service.find_free_slot(busy, now, frequency=SCHEDULE_FREQUENCY_DAILY) is None


def test_suggest_raises_when_no_free_slot(now: datetime):
    """所有低峰时段都被占满时，应当明确报错而不是给出冲突时间。"""
    ai_client = FakeAIClient([], error=RuntimeError("网关超时"))
    service = ScheduleSuggestionService(ai_client=ai_client)

    # 每个低峰时段都放一个任务，把兜底算法可选的时间点全部占满
    tasks = [
        make_task(index + 1, f"占位任务{index}", f"{minute} {hour} * * *")
        for index, (hour, minute) in enumerate(
            (hour, minute)
            for hour in FALLBACK_HOURS
            for minute in FALLBACK_MINUTES
        )
    ]

    with pytest.raises(ScheduleSuggestionError):
        asyncio.run(service.suggest(tasks=tasks, task_name="新任务", now=now))


def test_suggest_excludes_task_itself(now: datetime):
    ai_client = FakeAIClient(["17 3 * * *"])
    service = ScheduleSuggestionService(ai_client=ai_client)
    tasks = [make_task(1, "正在编辑的任务", "0 8 * * *")]

    suggestion = asyncio.run(
        service.suggest(
            tasks=tasks,
            task_name="正在编辑的任务",
            exclude_task_id=1,
            now=now,
        )
    )

    assert suggestion.considered_tasks == 0
    assert suggestion.avoided == []


# ----------------------------------------------------------------------
# 执行频率
# ----------------------------------------------------------------------


def test_normalize_frequency_falls_back_to_weekly():
    """未知频率或缺省值都按每周处理。"""
    assert normalize_frequency(None) == SCHEDULE_FREQUENCY_WEEKLY
    assert normalize_frequency("") == SCHEDULE_FREQUENCY_WEEKLY
    assert normalize_frequency("每天") == SCHEDULE_FREQUENCY_WEEKLY
    assert normalize_frequency("DAILY") == SCHEDULE_FREQUENCY_DAILY
    assert normalize_frequency("every_2_days") == SCHEDULE_FREQUENCY_EVERY_2_DAYS
    assert DEFAULT_SCHEDULE_FREQUENCY == SCHEDULE_FREQUENCY_WEEKLY


@pytest.mark.parametrize(
    "cron,frequency,expected",
    [
        # 每周：日字段为 *，周字段固定某一天
        ("17 3 * * 0", SCHEDULE_FREQUENCY_WEEKLY, True),
        ("17 3 * * 6", SCHEDULE_FREQUENCY_WEEKLY, True),
        ("17 3 * * *", SCHEDULE_FREQUENCY_WEEKLY, False),
        ("17 3 * * 1-5", SCHEDULE_FREQUENCY_WEEKLY, False),
        # 每两天 / 每 3 天：日字段是步长
        ("17 3 */2 * *", SCHEDULE_FREQUENCY_EVERY_2_DAYS, True),
        ("17 3 */3 * *", SCHEDULE_FREQUENCY_EVERY_2_DAYS, False),
        ("17 3 */3 * *", SCHEDULE_FREQUENCY_EVERY_3_DAYS, True),
        ("17 3 */2 * *", SCHEDULE_FREQUENCY_EVERY_3_DAYS, False),
        # 每天：日、月份、周都是 *
        ("17 3 * * *", SCHEDULE_FREQUENCY_DAILY, True),
        ("17 3 */2 * *", SCHEDULE_FREQUENCY_DAILY, False),
        ("17 3 * * 0", SCHEDULE_FREQUENCY_DAILY, False),
    ],
)
def test_cron_matches_frequency(cron: str, frequency: str, expected: bool):
    assert cron_matches_frequency(cron, frequency) is expected


def test_cron_matches_frequency_rejects_garbage():
    assert cron_matches_frequency("不是 cron", SCHEDULE_FREQUENCY_DAILY) is False
    assert cron_matches_frequency(None, SCHEDULE_FREQUENCY_DAILY) is False


def test_weekly_lookahead_covers_next_week():
    """每周执行的校验窗口必须覆盖一整周，否则下一次触发会落在窗口外。"""
    assert FREQUENCY_LOOKAHEAD_HOURS[SCHEDULE_FREQUENCY_WEEKLY] > 7 * 24
    # 每 2 天 / 每 3 天的窗口也要大于各自周期
    assert FREQUENCY_LOOKAHEAD_HOURS[SCHEDULE_FREQUENCY_EVERY_2_DAYS] > 2 * 24
    assert FREQUENCY_LOOKAHEAD_HOURS[SCHEDULE_FREQUENCY_EVERY_3_DAYS] > 3 * 24


def test_suggest_weekly_accepts_weekly_cron(now: datetime):
    ai_client = FakeAIClient(["17 3 * * 0"])
    service = ScheduleSuggestionService(ai_client=ai_client)

    suggestion = asyncio.run(
        service.suggest(
            tasks=[],
            task_name="新任务",
            frequency=SCHEDULE_FREQUENCY_WEEKLY,
            now=now,
        )
    )

    assert suggestion.cron == "17 3 * * 0"
    assert suggestion.source == "ai"
    # 提示词里必须写清频率要求与周字段取值约定
    assert "每周执行一次" in ai_client.prompts[0]
    assert "0=周一" in ai_client.prompts[0]


def test_suggest_retries_when_ai_ignores_frequency(now: datetime):
    """AI 无视频率要求给出「每天一次」时，要反馈期望的字段形态让它重写。"""
    ai_client = FakeAIClient(["17 3 * * *", "17 3 * * 0"])
    service = ScheduleSuggestionService(ai_client=ai_client)

    suggestion = asyncio.run(
        service.suggest(
            tasks=[],
            task_name="新任务",
            frequency=SCHEDULE_FREQUENCY_WEEKLY,
            now=now,
        )
    )

    assert suggestion.cron == "17 3 * * 0"
    assert suggestion.source == "ai"
    assert len(ai_client.prompts) == 2
    assert "不符合要求的执行频率" in ai_client.prompts[1]


@pytest.mark.parametrize(
    "frequency",
    [
        SCHEDULE_FREQUENCY_WEEKLY,
        SCHEDULE_FREQUENCY_EVERY_2_DAYS,
        SCHEDULE_FREQUENCY_EVERY_3_DAYS,
        SCHEDULE_FREQUENCY_DAILY,
    ],
)
def test_suggest_fallback_respects_frequency(frequency: str, now: datetime):
    """AI 不可用时，兜底算法给出的 cron 也必须符合所选频率。"""
    ai_client = FakeAIClient([], error=RuntimeError("网关超时"))
    service = ScheduleSuggestionService(ai_client=ai_client)

    suggestion = asyncio.run(
        service.suggest(
            tasks=[],
            task_name="新任务",
            frequency=frequency,
            now=now,
        )
    )

    assert suggestion.source == "fallback"
    assert cron_matches_frequency(suggestion.cron, frequency) is True


def test_weekly_fallback_rotates_weekdays(service, now: datetime):
    """每周兜底会从「下一个星期几」开始轮转，且冲突时顺延到后面的星期几。"""
    first = service.find_free_slot([], now, frequency=SCHEDULE_FREQUENCY_WEEKLY)
    assert first is not None

    # 日期推进一天，轮转起点也跟着后移
    later = service.find_free_slot(
        [], now + timedelta(days=1), frequency=SCHEDULE_FREQUENCY_WEEKLY
    )
    assert later is not None
    assert first.split()[-1] != later.split()[-1]

    # 第一个星期几被占用时，应当顺延到下一个星期几，而不是原地打转
    fires = service.collect_fire_times(
        first,
        now,
        lookahead_hours=FREQUENCY_LOOKAHEAD_HOURS[SCHEDULE_FREQUENCY_WEEKLY],
    )
    busy = [BusySlot(when=fires[0], task_name="占位任务")]
    second = service.find_free_slot(
        busy, now, frequency=SCHEDULE_FREQUENCY_WEEKLY
    )
    assert second is not None
    assert second.split()[-1] != first.split()[-1]

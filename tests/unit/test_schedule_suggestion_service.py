"""
定时建议服务的单元测试
"""
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional

import pytest

from src.domain.models.task import Task
from src.services.schedule_suggestion_service import (
    CONFLICT_WINDOW_MINUTES,
    FALLBACK_HOURS,
    FALLBACK_MINUTES,
    BusySlot,
    ScheduleSuggestionError,
    ScheduleSuggestionService,
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
        service.suggest(tasks=tasks, task_name="索尼A7M4", keyword="a7m4", now=now)
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
        service.suggest(tasks=tasks, task_name="新任务", now=now)
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
        service.suggest(tasks=[], task_name="新任务", now=now)
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

    assert service.find_free_slot(busy, now) is None


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

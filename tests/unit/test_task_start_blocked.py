"""熔断拒绝启动时的 409 响应相关测试。

覆盖：
  - TaskStartBlockedError 的结构化载荷
  - ProcessService.start_task 在熔断时抛出该异常
  - API 路由把熔断翻译成 409（而不是笼统的 500）
  - 调度器遇到熔断不会被打断
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.services.process_service import ProcessService, TaskStartBlockedError
from src.services.scheduler_service import SchedulerService


class TestTaskStartBlockedError:
    def test_payload_contains_structured_fields(self):
        paused_until = datetime(2026, 11, 10, 8, 30, 0)
        error = TaskStartBlockedError(
            "小米17ultra",
            reason="baxia-dialog",
            paused_until=paused_until,
            consecutive_failures=3,
            threshold=3,
        )
        payload = error.to_payload()
        assert payload["task_name"] == "小米17ultra"
        assert payload["reason"] == "baxia-dialog"
        assert payload["consecutive_failures"] == 3
        assert payload["threshold"] == 3
        assert payload["paused_until"] == paused_until.isoformat()
        assert "小米17ultra" in payload["message"]

    def test_message_is_human_readable(self):
        error = TaskStartBlockedError(
            "任务A",
            reason="未找到可用的登录状态文件",
            paused_until=datetime(2026, 11, 10, 8, 30, 0),
            consecutive_failures=3,
            threshold=3,
        )
        message = error.message
        assert "任务A" in message
        assert "3" in message
        assert "2026-11-10 08:30:00" in message
        assert "未找到可用的登录状态文件" in message

    def test_payload_tolerates_missing_pause_window(self):
        error = TaskStartBlockedError("任务B", reason="未知错误")
        payload = error.to_payload()
        assert payload["paused_until"] is None
        assert payload["consecutive_failures"] == 0
        assert payload["threshold"] == 0


class TestProcessServiceRaisesOnCircuitOpen:
    def test_start_task_raises_blocked_error_when_paused(self, monkeypatch):
        service = ProcessService()
        paused_until = datetime.now() + timedelta(hours=1)
        service.failure_guard.should_skip_start = lambda *args, **kwargs: SimpleNamespace(
            skip=True,
            should_notify=False,
            reason="baxia-dialog",
            consecutive_failures=3,
            paused_until=paused_until,
        )

        async def run_scenario():
            with pytest.raises(TaskStartBlockedError) as excinfo:
                await service.start_task(1, "小米17ultra")
            return excinfo.value

        error = asyncio.run(run_scenario())
        assert error.task_name == "小米17ultra"
        assert error.reason == "baxia-dialog"
        assert error.consecutive_failures == 3


class TestSchedulerToleratesBlockedTask:
    def test_scheduled_run_does_not_raise(self):
        service = ProcessService()

        async def blocked_start(task_id, task_name):
            raise TaskStartBlockedError(
                task_name,
                reason="baxia-dialog",
                consecutive_failures=3,
                threshold=3,
            )

        service.start_task = blocked_start
        scheduler = SchedulerService(service)

        asyncio.run(scheduler._run_task(1, "小米17ultra"))


class TestStartTaskApiReturns409:
    def test_api_returns_409_with_structured_detail(self, api_client, api_context):
        from src.services.process_service import TaskStartBlockedError

        api_client.post(
            "/api/tasks/",
            json={
                "task_name": "Sony A7M4",
                "enabled": True,
                "keyword": "sony a7m4",
                "description": "desc",
                "analyze_images": True,
                "max_pages": 2,
                "personal_only": True,
                "ai_prompt_base_file": "prompts/base_prompt.txt",
                "ai_prompt_criteria_file": "prompts/sony_a7m4_criteria.txt",
                "decision_mode": "ai",
                "keyword_rules": [],
            },
        )

        process_service = api_context["process_service"]

        async def blocked_start(task_id, task_name):
            raise TaskStartBlockedError(
                task_name,
                reason="baxia-dialog",
                paused_until=datetime(2026, 11, 10, 8, 30, 0),
                consecutive_failures=3,
                threshold=3,
            )

        process_service.start_task = blocked_start

        response = api_client.post("/api/tasks/start/0")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["task_name"] == "Sony A7M4"
        assert detail["reason"] == "baxia-dialog"
        assert detail["consecutive_failures"] == 3
        assert detail["threshold"] == 3
        assert detail["paused_until"] == "2026-11-10T08:30:00"
        assert detail["message"]

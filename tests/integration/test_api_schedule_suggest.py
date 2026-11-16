"""
POST /api/tasks/suggest-schedule 接口测试
"""
import asyncio

from src.services.schedule_suggestion_service import ScheduleSuggestionService


def test_suggest_schedule_returns_cron(api_client, sample_task_payload, monkeypatch):
    """接口应返回可直接使用的 cron 表达式。"""
    sample_task_payload["cron"] = "0 8 * * *"
    assert api_client.post("/api/tasks/", json=sample_task_payload).status_code == 200

    async def fake_request_ai(self, prompt: str) -> str:
        assert "任务「Sony A7M4」" in prompt
        return "17 3 * * *"

    monkeypatch.setattr(ScheduleSuggestionService, "request_ai", fake_request_ai)

    response = api_client.post(
        "/api/tasks/suggest-schedule",
        json={"task_name": "新任务", "keyword": "sony a7m4", "frequency": "daily"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["cron"] == "17 3 * * *"
    assert payload["source"] == "ai"
    assert payload["considered_tasks"] == 1


def test_suggest_schedule_defaults_to_weekly(api_client, monkeypatch):
    """不传 frequency 时按每周生成。"""
    captured = {}

    async def fake_request_ai(self, prompt: str) -> str:
        captured["prompt"] = prompt
        return "17 3 * * 0"

    monkeypatch.setattr(ScheduleSuggestionService, "request_ai", fake_request_ai)

    response = api_client.post("/api/tasks/suggest-schedule", json={"task_name": "新任务"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["cron"] == "17 3 * * 0"
    assert payload["source"] == "ai"
    # 提示词里必须写明每周执行的要求
    assert "每周执行一次" in captured["prompt"]


def test_suggest_schedule_honors_frequency(api_client, monkeypatch):
    """传入 frequency 时，AI 生成的 cron 必须符合该频率。"""
    captured = {}

    async def fake_request_ai(self, prompt: str) -> str:
        captured["prompt"] = prompt
        return "17 3 */2 * *"

    monkeypatch.setattr(ScheduleSuggestionService, "request_ai", fake_request_ai)

    response = api_client.post(
        "/api/tasks/suggest-schedule",
        json={"task_name": "新任务", "frequency": "every_2_days"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["cron"] == "17 3 */2 * *"
    assert payload["source"] == "ai"
    assert "每两天执行一次" in captured["prompt"]


def test_suggest_schedule_rejects_cron_with_wrong_frequency(
    api_client, monkeypatch
):
    """AI 无视频率要求时，接口不应把「每天一次」的结果当成每周返回。"""
    async def fake_request_ai(self, prompt: str) -> str:
        return "17 3 * * *"

    monkeypatch.setattr(ScheduleSuggestionService, "request_ai", fake_request_ai)

    response = api_client.post(
        "/api/tasks/suggest-schedule",
        json={"task_name": "新任务", "frequency": "weekly"},
    )

    assert response.status_code == 200
    payload = response.json()
    # 三轮都被判为频率不符，最终落到符合频率的兜底方案
    assert payload["source"] == "fallback"
    assert payload["cron"].split()[-1] in {"0", "1", "2", "3", "4", "5", "6"}


def test_suggest_schedule_excludes_current_task(
    api_client, sample_task_payload, monkeypatch
):
    """编辑已有任务时，该任务自身不应被算作冲突源。"""
    sample_task_payload["cron"] = "0 8 * * *"
    created = api_client.post("/api/tasks/", json=sample_task_payload).json()
    task_id = created["task"]["id"]

    async def fake_request_ai(self, prompt: str) -> str:
        assert "Sony A7M4" not in prompt
        return "17 3 * * *"

    monkeypatch.setattr(ScheduleSuggestionService, "request_ai", fake_request_ai)

    response = api_client.post(
        "/api/tasks/suggest-schedule",
        json={"task_id": task_id, "task_name": "Sony A7M4"},
    )

    assert response.status_code == 200
    assert response.json()["considered_tasks"] == 0


def test_suggest_schedule_falls_back_when_ai_fails(
    api_client, sample_task_payload, monkeypatch
):
    """AI 不可用时也要给出可用的兜底时间，而不是报错。"""
    sample_task_payload["cron"] = "0 8 * * *"
    assert api_client.post("/api/tasks/", json=sample_task_payload).status_code == 200

    async def failing_request_ai(self, prompt: str) -> str:
        raise RuntimeError("网关超时")

    monkeypatch.setattr(ScheduleSuggestionService, "request_ai", failing_request_ai)

    response = api_client.post("/api/tasks/suggest-schedule", json={"task_name": "新任务"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "fallback"
    assert payload["cron"]
    assert "网关超时" in payload["reason"]


def test_suggest_schedule_returns_409_when_no_slot(api_client, monkeypatch):
    """完全找不到空闲时段时，接口应返回 409 并说明原因。"""
    from src.services.schedule_suggestion_service import ScheduleSuggestionError

    async def raising_request_ai(self, prompt: str) -> str:
        raise ScheduleSuggestionError("当前已有任务的定时规则过于密集。")

    monkeypatch.setattr(ScheduleSuggestionService, "request_ai", raising_request_ai)

    response = api_client.post("/api/tasks/suggest-schedule", json={"task_name": "新任务"})

    assert response.status_code == 409
    assert "过于密集" in response.json()["detail"]

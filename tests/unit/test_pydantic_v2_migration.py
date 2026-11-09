"""Pydantic v2 迁移相关测试：禁止再使用已弃用的 .dict()。

背景：
  Pydantic V2 已弃用 BaseModel.dict()，V3 将移除。
  之前 spider_v2.py 用 task.dict() 拉取任务配置，运行时会打印
  PydanticDeprecatedSince20 警告，且未来版本会直接报错。
"""

import ast
from pathlib import Path

import pytest

from src.domain.models.task import Task

SPIDER_V2 = Path(__file__).resolve().parents[2] / "spider_v2.py"


def _deprecated_model_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"dict", "json"}:
            # 只统计作用在模型上的调用：x.dict() / x.json()
            if isinstance(func.value, (ast.Name, ast.Attribute)):
                offenders.append(f"line {node.lineno}: .{func.attr}()")
    return offenders


def test_spider_v2_has_no_deprecated_pydantic_calls():
    offenders = _deprecated_model_calls(SPIDER_V2)
    assert offenders == [], f"spider_v2.py 仍在使用已弃用的 Pydantic API: {offenders}"


def test_spider_v2_uses_model_dump():
    source = SPIDER_V2.read_text(encoding="utf-8")
    assert "model_dump()" in source
    assert ".dict()" not in source


def test_model_dump_preserves_task_fields():
    """model_dump() 产出的字段必须和原来 .dict() 一致（枚举已转成值）。"""
    task = Task(
        id=1,
        task_name="小米17promax",
        enabled=True,
        keyword="小米17promax",
        description="测试",
        analyze_images=True,
        max_pages=2,
        personal_only=False,
        ai_prompt_base_file="prompts/base_prompt.txt",
        ai_prompt_criteria_file="prompts/criteria.txt",
        decision_mode="ai",
        keyword_rules=["关键词一"],
        is_running=False,
    )
    dumped = task.model_dump()

    assert dumped["task_name"] == "小米17promax"
    assert dumped["keyword"] == "小米17promax"
    assert dumped["max_pages"] == 2
    assert dumped["keyword_rules"] == ["关键词一"]
    assert dumped["decision_mode"] == "ai"
    assert dumped["is_running"] is False
    assert "cron" in dumped
    assert "account_state_file" in dumped


def test_model_dump_values_are_json_serializable():
    """爬虫会把 task_config 传给 json 序列化，model_dump 结果必须可序列化。"""
    import json

    task = Task(
        id=2,
        task_name="K100 pro max",
        enabled=True,
        keyword="K100 pro max",
        description="",
        analyze_images=True,
        max_pages=1,
        personal_only=False,
        ai_prompt_base_file="prompts/base_prompt.txt",
        ai_prompt_criteria_file="prompts/criteria.txt",
        decision_mode="ai",
        keyword_rules=[],
        is_running=False,
    )
    payload = json.dumps([task.model_dump()], ensure_ascii=False)
    assert "K100 pro max" in payload

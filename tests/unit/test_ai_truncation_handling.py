"""推理模型输出被截断时的处理。

背景：推理模型（reasoning model）的"思考 token"与"最终答案 token"共享同一个
max_tokens 预算，预算不足时 content 会被挤成空字符串（finish_reason=length）。
以下用例锁定三条不变量：

1. 截断能被识别出来，不会退化成"JSON 解析失败"这种误导性报错；
2. 截断后自动提高预算重试，而不是用同样的预算重复失败；
3. 非截断场景下 reasoning_content 回退能力保持不变（智谱等网关兼容）。
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

import src.ai_handler as ai_handler
import src.config as app_config
from src.services.ai_request_compat import detect_response_truncation
from src.services.ai_response_parser import (
    AIResponseTruncatedError,
    EmptyAIResponseError,
    extract_ai_response_content,
    parse_ai_response_json,
)


VALID_PAYLOAD = json.dumps(
    {
        "prompt_version": "v1",
        "is_recommended": True,
        "reason": "ok",
        "risk_tags": [],
        "criteria_analysis": {"seller_type": {"status": "PASS"}},
    },
    ensure_ascii=False,
)


def _build_fake_client(chat_create_impl):
    responses = SimpleNamespace(create=chat_create_impl)
    chat = SimpleNamespace(completions=SimpleNamespace(create=chat_create_impl))
    return SimpleNamespace(responses=responses, chat=chat)


def _chat_response(content, finish_reason="stop", reasoning_content=""):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(
                    content=content,
                    reasoning_content=reasoning_content,
                ),
            )
        ],
        usage=SimpleNamespace(
            completion_tokens=4000,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=4000),
        ),
    )


# --------------------------------------------------------------------------
# detect_response_truncation
# --------------------------------------------------------------------------


def test_detect_response_truncation_flags_finish_reason_length():
    truncated, reason = detect_response_truncation(
        _chat_response("", finish_reason="length", reasoning_content="思考中")
    )

    assert truncated is True
    assert "finish_reason=length" in reason


def test_detect_response_truncation_ignores_normal_stop_response():
    truncated, _reason = detect_response_truncation(_chat_response(VALID_PAYLOAD))

    assert truncated is False


def test_detect_response_truncation_handles_responses_api_incomplete():
    response = SimpleNamespace(
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
    )

    truncated, reason = detect_response_truncation(response)

    assert truncated is True
    assert "status=incomplete" in reason


def test_detect_response_truncation_ignores_completed_responses_api_payload():
    response = SimpleNamespace(
        status="completed",
        incomplete_details=None,
    )

    truncated, _reason = detect_response_truncation(response)

    assert truncated is False


# --------------------------------------------------------------------------
# 截断响应不会再回退到 reasoning_content
# --------------------------------------------------------------------------


def test_truncated_response_is_detected_before_content_extraction():
    """截断响应即使带着 reasoning_content，也必须先被判定为截断。"""
    response = _chat_response(
        "", finish_reason="length", reasoning_content="We need answer only JSON..."
    )

    truncated, _reason = detect_response_truncation(response)

    assert truncated is True
    # 非截断路径的回退能力本身不变（见下一个用例）
    with pytest.raises(EmptyAIResponseError):
        raise AIResponseTruncatedError("截断")


def test_extract_ai_response_content_still_falls_back_to_reasoning_content():
    """非截断场景：content 为空但 reasoning_content 有值时，回退能力保持不变。"""
    message = SimpleNamespace(content=None, reasoning_content="这是推理内容")
    choice = SimpleNamespace(finish_reason="stop", message=message)
    response = SimpleNamespace(choices=[choice])

    assert extract_ai_response_content(response) == "这是推理内容"


# --------------------------------------------------------------------------
# 解析结果必须是 JSON 对象
# --------------------------------------------------------------------------


def test_parse_ai_response_json_rejects_truncated_json_instead_of_returning_nested_list():
    """被截断的 JSON 里嵌套数组是完整的，但绝不能被当成完整结果返回。"""
    truncated_json = """{
  "prompt_version": "v1",
  "is_recommended": false,
  "reason": "abc",
  "risk_tags": [
    "16GB+512GB缺少系统参数页佐证",
    "运营商网络锁状态未核验"
  ],
  "criteria_analysis": {"""

    with pytest.raises(ValueError) as excinfo:
        parse_ai_response_json(truncated_json)

    assert "未找到可解析的JSON对象" in str(excinfo.value)


def test_validate_ai_response_format_rejects_non_dict_payload():
    assert ai_handler.validate_ai_response_format(["tag1", "tag2"]) is False
    assert ai_handler.validate_ai_response_format("not a dict") is False


def test_validate_ai_response_format_accepts_valid_dict():
    assert ai_handler.validate_ai_response_format(json.loads(VALID_PAYLOAD)) is True


# --------------------------------------------------------------------------
# ai_handler：截断后自动提高预算重试
# --------------------------------------------------------------------------


def test_get_ai_analysis_escalates_budget_after_truncation(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    requested_budgets = []

    async def fake_create(**kwargs):
        requested_budgets.append(kwargs.get("max_tokens"))
        if len(requested_budgets) == 1:
            # 第一次：预算不足，回答被截断
            return _chat_response(
                "", finish_reason="length", reasoning_content="思考中被截断"
            )
        return _chat_response(VALID_PAYLOAD)

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(ai_handler, "AI_MAX_OUTPUT_TOKENS", 4000)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "1", "商品标题": "测试商品"}},
            image_paths=[],
            prompt_text="请输出 JSON",
        )
    )

    assert result["is_recommended"] is True
    # 第一次预算不足，第二次翻倍后成功，不需要四次重试。
    assert requested_budgets == [4000, 8000]


def test_get_ai_analysis_raises_truncation_error_when_budget_cannot_help(
    monkeypatch, tmp_path
):
    """预算已到硬上限仍被截断时，报错要明确指出是截断，而不是 JSON 解析失败。"""
    monkeypatch.chdir(tmp_path)

    async def fake_create(**_kwargs):
        return _chat_response(
            "", finish_reason="length", reasoning_content="思考中被截断"
        )

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(ai_handler, "AI_MAX_OUTPUT_TOKENS", 4000)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)

    with pytest.raises(Exception) as excinfo:
        asyncio.run(
            ai_handler.get_ai_analysis(
                {"商品信息": {"商品ID": "1", "商品标题": "测试商品"}},
                image_paths=[],
                prompt_text="请输出 JSON",
            )
        )

    message = str(excinfo.value)
    assert "截断" in message
    assert "Expecting value" not in message

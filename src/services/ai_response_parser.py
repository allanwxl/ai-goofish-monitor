"""
AI 响应解析工具
"""
import json
from typing import Any


class EmptyAIResponseError(ValueError):
    """AI 返回了空内容。"""


class AIResponseTruncatedError(EmptyAIResponseError):
    """AI 输出因 max_tokens 上限被截断。

    推理模型的推理 token 与最终答案共享同一个 max_tokens 预算，预算不足时
    content 会被挤成空字符串。调用方应当提高预算后重试，而不是把空内容当成
    "JSON 格式错误"。
    """


def extract_ai_response_content(response: Any) -> str:
    """从不同形态的 AI 响应中提取文本内容。"""
    if response is None:
        raise EmptyAIResponseError("AI响应对象为空。")

    if isinstance(response, (bytes, bytearray)):
        text = response.decode("utf-8", errors="replace")
        return _normalize_text_content(text)

    if isinstance(response, str):
        return _normalize_text_content(response)

    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        return _normalize_text_content(output_text)

    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        if message is None:
            raise EmptyAIResponseError("AI响应缺少 message。")
        content = getattr(message, "content", None)
        
        # 智谱等 OpenAI 兼容网关在某些模式下会把输出放在 reasoning_content 而非 content
        try:
            return _normalize_text_content(_coerce_content_parts(content))
        except EmptyAIResponseError:
            reasoning_content = getattr(message, "reasoning_content", None)
            if reasoning_content:
                return _normalize_text_content(_coerce_content_parts(reasoning_content))
            raise

    raise ValueError(f"无法识别的AI响应类型: {type(response).__name__}")


def parse_ai_response_json(content: str) -> Any:
    """解析 AI 文本响应中的 JSON。

    正常情况下返回 dict。若整个响应本身就是 JSON 数组则原样返回 list，
    调用方需自行校验类型（见 ai_handler.validate_ai_response_format）。
    """
    cleaned = _strip_code_fences(content)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return _extract_first_json_value(cleaned)


def _coerce_content_parts(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (bytes, bytearray)):
        return content.decode("utf-8", errors="replace")
    if not isinstance(content, list):
        raise ValueError(f"AI响应内容类型不受支持: {type(content).__name__}")

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
            continue
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _normalize_text_content(content: str) -> str:
    text = str(content).strip()
    if not text:
        raise EmptyAIResponseError("AI响应内容为空。")
    return text


def _strip_code_fences(content: str) -> str:
    cleaned = content.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def _extract_first_json_value(content: str):
    """从文本中提取第一个可解析的 JSON 对象。

    本项目的输出 schema 强制要求顶层是 JSON 对象，因此这里只尝试 ``{`` 位置。
    之前的实现同时接受 ``[``，在响应被 max_tokens 截断时会把嵌套数组
    （例如 risk_tags）误当成完整结果返回，导致后续校验报出误导性的
    "缺少必需字段"。
    """
    decoder = json.JSONDecoder()
    last_error: json.JSONDecodeError | None = None

    for start_index, char in enumerate(content):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(content[start_index:])
            return parsed
        except json.JSONDecodeError as exc:
            last_error = exc

    preview = content.strip()[:200]
    message = f"AI响应中未找到可解析的JSON对象（已尝试所有 '{{' 位置）: {preview!r}"
    if last_error is not None:
        message += f"；最后一次解析错误: {last_error}"
    raise json.JSONDecodeError(message, content, 0)

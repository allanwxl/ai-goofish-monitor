import asyncio
import base64
import hashlib
import hmac

import pytest

from src.infrastructure.external.notification_clients.base import NotificationClient
from src.infrastructure.external.notification_clients.feishu_client import FeishuBotClient
from src.infrastructure.external.notification_clients.pushplus_client import PushPlusClient
from src.infrastructure.external.notification_clients.webhook_client import WebhookClient
from src.services.notification_service import NotificationService


class _OkClient(NotificationClient):
    channel_key = "ok"
    display_name = "OK"

    async def send(self, product_data, reason):
        return None


class _FailClient(NotificationClient):
    channel_key = "fail"
    display_name = "FAIL"

    async def send(self, product_data, reason):
        raise RuntimeError("boom")


def test_notification_service_collects_success_and_failure_results():
    service = NotificationService([_OkClient(enabled=True), _FailClient(enabled=True)])

    results = asyncio.run(
        service.send_notification({"商品标题": "Sony A7M4"}, "价格合适")
    )

    assert results["ok"]["success"] is True
    assert results["ok"]["message"] == "发送成功"
    assert results["fail"]["success"] is False
    assert results["fail"]["message"] == "boom"


def test_webhook_client_renders_json_templates(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

    def _fake_post(url, headers=None, json=None, data=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["data"] = data
        return _FakeResponse()

    monkeypatch.setattr("requests.post", _fake_post)

    client = WebhookClient(
        webhook_url="https://hooks.example.com/notify",
        webhook_method="POST",
        webhook_headers='{"Authorization":"Bearer token"}',
        webhook_content_type="JSON",
        webhook_query_parameters='{"task":"{{title}}"}',
        webhook_body='{"message":"{{content}}","link":"{{desktop_link}}"}',
        pcurl_to_mobile=False,
    )

    asyncio.run(
        client.send(
            {
                "商品标题": "Sony A7M4",
                "当前售价": "9999",
                "商品链接": "https://www.goofish.com/item/123",
            },
            "价格合适",
        )
    )

    assert "task=%F0%9F%9A%A8+%E6%96%B0%E6%8E%A8%E8%8D%90%21+Sony+A7M4" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer token"
    assert captured["json"]["message"].startswith("价格: 9999")
    assert captured["json"]["link"] == "https://www.goofish.com/item/123"
    assert captured["data"] is None


def _product():
    return {
        "商品标题": "Sony A7M4",
        "当前售价": "9999",
        "商品链接": "https://www.goofish.com/item/123",
    }


class _FakeFeishuResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_pushplus_client_sends_text_template_and_requires_business_code(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 200, "msg": "请求成功"}

    def _fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr("requests.post", _fake_post)
    client = PushPlusClient("test-token", "group-code", pcurl_to_mobile=False)

    asyncio.run(client.send(_product(), "价格合适"))

    assert captured["url"] == "https://www.pushplus.plus/send"
    assert captured["json"]["token"] == "test-token"
    assert captured["json"]["topic"] == "group-code"
    assert captured["json"]["template"] == "txt"
    assert captured["json"]["content"].startswith("价格: 9999\n原因: 价格合适")


def test_pushplus_client_rejects_business_error_with_http_200(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 903, "msg": "无效的用户token"}

    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: _FakeResponse(),
    )
    client = PushPlusClient("bad-token", pcurl_to_mobile=False)

    with pytest.raises(RuntimeError, match="无效的用户token"):
        asyncio.run(client.send(_product(), "价格合适"))


def test_feishu_client_sends_interactive_card_payload(monkeypatch):
    captured = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeFeishuResponse({"code": 0, "msg": "success"})

    monkeypatch.setattr("requests.post", _fake_post)

    client = FeishuBotClient(
        "https://open.feishu.cn/open-apis/bot/v2/hook/demo-token",
        pcurl_to_mobile=False,
    )

    asyncio.run(client.send(_product(), "价格合适"))

    payload = captured["json"]
    card = payload["card"]
    assert captured["url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/demo-token"
    assert payload["msg_type"] == "interactive"
    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "blue"
    assert card["header"]["title"]["content"] == "🚨 新推荐! Sony A7M4"

    body_text = "\n".join(
        element.get("content", "")
        for element in card["body"]["elements"]
        if element.get("tag") == "markdown"
    )
    assert "价格: **9999**" in body_text
    assert "原因: 价格合适" in body_text

    actions = [
        element
        for element in card["body"]["elements"]
        if element.get("tag") == "button"
    ]
    assert len(actions) == 1
    button = actions[0]
    assert button["text"]["content"] == "💻 电脑端查看"
    # 2.0 用 behaviors 的 open_url 声明跳转，不能用 1.0 的 url 字段
    assert button["behaviors"] == [
        {"type": "open_url", "default_url": "https://www.goofish.com/item/123"}
    ]
    assert "url" not in button

    # 未配置签名密钥时不下发签名字段
    assert "timestamp" not in payload
    assert "sign" not in payload


def test_feishu_card_v2_avoids_schema_1_only_properties(monkeypatch):
    """schema 2.0 对属性校验比 1.0 严格，1.0 专有属性会直接被飞书拒绝。

    回归点：曾经把 2.0 外壳（schema/body）和 1.0 组件（action/note/wide_screen_mode）
    混在一起发，卡片被拒后静默降级成纯文本。
    """
    captured = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _FakeFeishuResponse({"code": 0, "msg": "success"})

    monkeypatch.setattr("requests.post", _fake_post)

    client = FeishuBotClient(
        "https://open.feishu.cn/open-apis/bot/v2/hook/demo-token",
        pcurl_to_mobile=False,
    )

    asyncio.run(client.send(_product(), "价格合适"))

    card = captured["json"]["card"]
    assert card["schema"] == "2.0"
    assert "wide_screen_mode" not in card.get("config", {})

    tags = [element.get("tag") for element in card["body"]["elements"]]
    # 2.0 不支持交互模块与备注组件
    assert "action" not in tags
    assert "note" not in tags
    assert "button" in tags
    # 2.0 没有 note 组件，页脚改用普通文本组件
    assert "div" in tags


def test_feishu_card_contains_extra_product_fields(monkeypatch):
    captured = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _FakeFeishuResponse({"code": 0, "msg": "success"})

    monkeypatch.setattr("requests.post", _fake_post)

    client = FeishuBotClient(
        "https://open.feishu.cn/open-apis/bot/v2/hook/demo-token",
        pcurl_to_mobile=False,
    )

    product = {
        **_product(),
        "商品原价": "12999",
        "发货地区": "上海",
        "卖家昵称": "阿呆的小店",
        "发布时间": "2026-11-20 09:30",
        "“想要”人数": "12",
        "商品标签": ["包邮", "验货宝"],
    }
    asyncio.run(client.send(product, "价格合适"))

    card = captured["json"]["card"]
    body_text = "\n".join(
        element.get("content", "")
        for element in card["body"]["elements"]
        if element.get("tag") == "markdown"
    )
    assert "原价: 12999" in body_text
    assert "地区: 上海" in body_text
    assert "卖家: 阿呆的小店" in body_text
    assert "发布: 2026-11-20 09:30" in body_text
    assert "想要: 12 人" in body_text
    assert "标签: 包邮 / 验货宝" in body_text


def test_feishu_client_builds_alert_card_for_task_failure(monkeypatch):
    captured = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _FakeFeishuResponse({"code": 0, "msg": "success"})

    monkeypatch.setattr("requests.post", _fake_post)

    client = FeishuBotClient(
        "https://open.feishu.cn/open-apis/bot/v2/hook/demo-token",
        pcurl_to_mobile=False,
    )

    asyncio.run(
        client.send(
            {
                "商品标题": "[任务异常] MacBook",
                "当前售价": "N/A",
                "商品链接": "#",
            },
            "任务运行失败",
        )
    )

    card = captured["json"]["card"]
    assert card["header"]["template"] == "red"
    assert card["header"]["title"]["content"] == "⚠️ [任务异常] MacBook"
    assert all(
        element.get("tag") != "action" for element in card["body"]["elements"]
    )
    body_text = "\n".join(
        element.get("content", "")
        for element in card["body"]["elements"]
        if element.get("tag") == "markdown"
    )
    assert "原因: 任务运行失败" in body_text


def test_feishu_client_falls_back_to_legacy_card_then_text(monkeypatch):
    """两种卡片都被拒时才降级纯文本：2.0 → 1.0 → 文本。"""
    payloads = []

    def _fake_post(url, json=None, headers=None, timeout=None):
        payloads.append(json)
        if len(payloads) <= 2:
            return _FakeFeishuResponse({"code": 9499, "msg": "Bad Request"})
        return _FakeFeishuResponse({"code": 0, "msg": "success"})

    monkeypatch.setattr("requests.post", _fake_post)

    client = FeishuBotClient(
        "https://open.feishu.cn/open-apis/bot/v2/hook/demo-token",
        pcurl_to_mobile=False,
    )

    asyncio.run(client.send(_product(), "价格合适"))

    assert len(payloads) == 3
    assert payloads[0]["msg_type"] == "interactive"
    assert payloads[0]["card"]["schema"] == "2.0"
    assert payloads[1]["msg_type"] == "interactive"
    assert "schema" not in payloads[1]["card"]
    assert payloads[2]["msg_type"] == "text"
    assert payloads[2]["content"]["text"].startswith("🚨 新推荐! Sony A7M4")
    assert "价格: 9999" in payloads[2]["content"]["text"]
    assert "原因: 价格合适" in payloads[2]["content"]["text"]
    assert "https://www.goofish.com/item/123" in payloads[2]["content"]["text"]


def test_feishu_client_falls_back_to_legacy_card_when_v2_rejected(monkeypatch):
    """2.0 卡片被拒时，退回 1.0 卡片，保证群���里仍然是卡片而不是纯文本。"""
    payloads = []

    def _fake_post(url, json=None, headers=None, timeout=None):
        payloads.append(json)
        if len(payloads) == 1:
            return _FakeFeishuResponse({"code": 200861, "msg": "card json parse err"})
        return _FakeFeishuResponse({"code": 0, "msg": "success"})

    monkeypatch.setattr("requests.post", _fake_post)

    client = FeishuBotClient(
        "https://open.feishu.cn/open-apis/bot/v2/hook/demo-token",
        pcurl_to_mobile=False,
    )

    asyncio.run(client.send(_product(), "价格合适"))

    assert len(payloads) == 2
    legacy_card = payloads[1]["card"]
    assert "schema" not in legacy_card
    assert legacy_card["config"]["wide_screen_mode"] is True
    assert any(element.get("tag") == "action" for element in legacy_card["elements"])


def test_feishu_client_signs_payload_when_secret_configured(monkeypatch):
    captured = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _FakeFeishuResponse({"StatusCode": 0, "StatusMessage": "success"})

    monkeypatch.setattr("requests.post", _fake_post)

    client = FeishuBotClient(
        "https://open.feishu.cn/open-apis/bot/v2/hook/demo-token",
        "demo-secret",
        pcurl_to_mobile=False,
    )

    asyncio.run(client.send(_product(), "价格合适"))

    timestamp = captured["json"]["timestamp"]
    expected_sign = base64.b64encode(
        hmac.new(
            f"{timestamp}\ndemo-secret".encode("utf-8"),
            b"",
            digestmod=hashlib.sha256,
        ).digest()
    ).decode("utf-8")
    assert captured["json"]["sign"] == expected_sign
    assert timestamp.isdigit()


def test_feishu_client_rejects_business_error_with_http_200(monkeypatch):
    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: _FakeFeishuResponse(
            {"code": 19021, "msg": "sign match fail"}
        ),
    )
    client = FeishuBotClient(
        "https://open.feishu.cn/open-apis/bot/v2/hook/demo-token",
        pcurl_to_mobile=False,
    )

    with pytest.raises(RuntimeError, match="sign match fail"):
        asyncio.run(client.send(_product(), "价格合适"))


def test_feishu_client_requires_webhook_url():
    client = FeishuBotClient(pcurl_to_mobile=False)

    assert client.is_enabled() is False
    with pytest.raises(RuntimeError, match="飞书机器人 未启用"):
        asyncio.run(client.send(_product(), "价格合适"))

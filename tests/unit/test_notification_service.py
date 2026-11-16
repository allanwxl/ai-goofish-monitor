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


def test_feishu_client_sends_text_payload(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 0, "msg": "success"}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse()

    monkeypatch.setattr("requests.post", _fake_post)

    client = FeishuBotClient(
        "https://open.feishu.cn/open-apis/bot/v2/hook/demo-token",
        pcurl_to_mobile=False,
    )

    asyncio.run(client.send(_product(), "价格合适"))

    assert captured["url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/demo-token"
    assert captured["json"]["msg_type"] == "text"
    assert captured["json"]["content"]["text"].startswith("🚨 新推荐! Sony A7M4")
    assert "价格: 9999" in captured["json"]["content"]["text"]
    assert "原因: 价格合适" in captured["json"]["content"]["text"]
    assert "https://www.goofish.com/item/123" in captured["json"]["content"]["text"]
    # 未配置签名密钥时不下发签名字段
    assert "timestamp" not in captured["json"]
    assert "sign" not in captured["json"]


def test_feishu_client_signs_payload_when_secret_configured(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"StatusCode": 0, "StatusMessage": "success"}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _FakeResponse()

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
    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 19021, "msg": "sign match fail"}

    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: _FakeResponse(),
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

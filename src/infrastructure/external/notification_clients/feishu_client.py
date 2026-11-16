"""
飞书群机器人通知客户端
支持自定义机器人 Webhook，可选开启签名校验
"""
import asyncio
import base64
import hashlib
import hmac
import time
from typing import Dict, Tuple

import requests

from .base import NotificationClient


class FeishuBotClient(NotificationClient):
    """飞书机器人通知客户端"""

    channel_key = "feishu"
    display_name = "飞书"

    def __init__(
        self,
        webhook_url: str | None = None,
        secret: str | None = None,
        pcurl_to_mobile: bool = True,
    ):
        super().__init__(enabled=bool(webhook_url), pcurl_to_mobile=pcurl_to_mobile)
        self.webhook_url = webhook_url
        self.secret = secret

    def _build_signature(self) -> Tuple[str, str]:
        """生成飞书自定义机器人签名：timestamp + sign"""
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{self.secret}"
        digest = hmac.new(
            string_to_sign.encode("utf-8"),
            b"",
            digestmod=hashlib.sha256,
        ).digest()
        return timestamp, base64.b64encode(digest).decode("utf-8")

    async def send(self, product_data: Dict, reason: str) -> None:
        if not self.is_enabled():
            raise RuntimeError("飞书机器人 未启用")

        message = self._build_message(product_data, reason)
        text_lines = [message.notification_title, ""]
        text_lines.append(f"价格: {message.price}")
        text_lines.append(f"原因: {message.reason}")
        if message.mobile_link:
            text_lines.append(f"手机端链接: {message.mobile_link}")
            text_lines.append(f"电脑端链接: {message.desktop_link}")
        else:
            text_lines.append(f"链接: {message.desktop_link}")

        payload = {
            "msg_type": "text",
            "content": {"text": "\n".join(text_lines)},
        }
        if self.secret:
            timestamp, sign = self._build_signature()
            payload["timestamp"] = timestamp
            payload["sign"] = sign

        headers = {"Content-Type": "application/json"}
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.post(
                self.webhook_url,
                json=payload,
                headers=headers,
                timeout=10,
            ),
        )
        response.raise_for_status()
        result = response.json()
        # 新版返回 code/msg，旧版返回 StatusCode/StatusMessage
        code = result.get("code", result.get("StatusCode", 0))
        if code != 0:
            message_text = (
                result.get("msg")
                or result.get("StatusMessage")
                or f"飞书返回错误码 {code}"
            )
            raise RuntimeError(message_text)

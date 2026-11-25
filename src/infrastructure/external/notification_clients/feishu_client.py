"""
飞书群机器人通知客户端
支持自定义机器人 Webhook，可选开启签名校验

推送优先使用交互式卡片：先发 schema 2.0 卡片，被拒后退回 schema 1.0 卡片，
两种卡片都被拒时才降级为纯文本，避免因卡片格式或机器人安全策略导致漏推。
"""
import asyncio
import base64
import hashlib
import hmac
import time
from datetime import datetime
from typing import Dict, List, Tuple

import requests

from .base import NotificationClient, NotificationMessage


CARD_TITLE_MAX_LENGTH = 40
CARD_REASON_MAX_LENGTH = 800
CARD_FOOTER_TEXT = "闲鱼智能监控"
CARD_TEMPLATE_RECOMMEND = "blue"
CARD_TEMPLATE_ALERT = "red"


class FeishuApiError(RuntimeError):
    """飞书接口明确拒绝该请求（HTTP 状态码异常或业务 code != 0）。"""


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
        # 卡片优先：先发 schema 2.0，再退回 schema 1.0，两种卡片都被拒才降级纯文本。
        # 网络超时等不确定情况不重发，避免重复推送。
        attempts = (
            ("卡片(2.0)", self._build_card_payload(message)),
            ("卡片(1.0)", self._build_legacy_card_payload(message)),
        )
        errors: List[str] = []
        for label, payload in attempts:
            try:
                await self._post_payload(payload)
                return
            except FeishuApiError as card_error:
                errors.append(f"{label}: {card_error}")
                print(f"[飞书] {label} 发送失败: {card_error}")

        # 卡片被拒（关键词策略、格式不兼容等）时降级为纯文本，避免漏推
        print(f"[飞书] 卡片消息发送失败，降级为纯文本: {'；'.join(errors)}")
        await self._post_payload(self._build_text_payload(message))

    async def _post_payload(self, payload: Dict) -> None:
        """向飞书 webhook 投递一次请求，失败时抛出 FeishuApiError。"""
        request_payload = dict(payload)
        if self.secret:
            timestamp, sign = self._build_signature()
            request_payload["timestamp"] = timestamp
            request_payload["sign"] = sign

        headers = {"Content-Type": "application/json"}
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.post(
                self.webhook_url,
                json=request_payload,
                headers=headers,
                timeout=10,
            ),
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise FeishuApiError(f"飞书 HTTP 错误: {exc}") from exc

        result = response.json()
        # 新版返回 code/msg，旧版返回 StatusCode/StatusMessage
        code = result.get("code", result.get("StatusCode", 0))
        if code != 0:
            message_text = (
                result.get("msg")
                or result.get("StatusMessage")
                or f"飞书返回错误码 {code}"
            )
            raise FeishuApiError(message_text)

    # ------------------------------------------------------------------
    # 纯文本消息（卡片降级用）
    # ------------------------------------------------------------------
    def _build_text_payload(self, message: NotificationMessage) -> Dict:
        text_lines = [message.notification_title, ""]
        text_lines.append(f"价格: {message.price}")
        text_lines.append(f"原因: {message.reason}")
        if message.mobile_link:
            text_lines.append(f"手机端链接: {message.mobile_link}")
            text_lines.append(f"电脑端链接: {message.desktop_link}")
        else:
            text_lines.append(f"链接: {message.desktop_link}")

        return {
            "msg_type": "text",
            "content": {"text": "\n".join(text_lines)},
        }

    # ------------------------------------------------------------------
    # 交互式卡片
    # ------------------------------------------------------------------
    def _build_card_payload(self, message: NotificationMessage) -> Dict:
        """schema 2.0 卡片。

        2.0 的属性校验比 1.0 严格：传入不支持的属性会直接报错。
        下列写法都是 1.0 专有、在 2.0 会被拒：
        - 交互模块 {"tag": "action"}：2.0 要把 button 直接放进 elements
        - 备注组件 {"tag": "note"}：2.0 用普通文本 div + notation 字号 + grey 颜色代替
        - config.wide_screen_mode：2.0 改用 config.width_mode
        """
        return {
            "msg_type": "interactive",
            "card": self._build_card_v2(message),
        }

    def _build_card_v2(self, message: NotificationMessage) -> Dict:
        is_alert = self._is_alert(message)
        if is_alert:
            header = {
                "template": CARD_TEMPLATE_ALERT,
                "title": {
                    "tag": "plain_text",
                    "content": _truncate(f"⚠️ {message.title}", CARD_TITLE_MAX_LENGTH),
                },
            }
            elements = [
                {"tag": "markdown", "content": _reason_markdown(message.reason)},
            ]
        else:
            header = {
                "template": CARD_TEMPLATE_RECOMMEND,
                "title": {
                    "tag": "plain_text",
                    "content": _truncate(
                        message.notification_title, CARD_TITLE_MAX_LENGTH
                    ),
                },
            }
            elements = [
                {"tag": "markdown", "content": "\n".join(self._build_info_lines(message))},
                {"tag": "hr"},
                {"tag": "markdown", "content": _reason_markdown(message.reason)},
            ]
            elements.extend(self._build_buttons_v2(message))

        elements.append(self._build_footer_v2())

        return {
            "schema": "2.0",
            "config": {"width_mode": "fill"},
            "header": header,
            "body": {"elements": elements, "vertical_spacing": "8px"},
        }

    @staticmethod
    def _build_buttons_v2(message: NotificationMessage) -> List[Dict]:
        """2.0 按钮：直接放在 elements 中，链接用 behaviors 的 open_url 声明。"""
        buttons: List[Dict] = []
        if message.mobile_link and message.mobile_link != message.desktop_link:
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "📱 手机端查看"},
                    "type": "primary",
                    "width": "fill",
                    "behaviors": [
                        {"type": "open_url", "default_url": message.mobile_link}
                    ],
                }
            )
        buttons.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "💻 电脑端查看"},
                "type": "default",
                "width": "fill",
                "behaviors": [
                    {"type": "open_url", "default_url": message.desktop_link}
                ],
            }
        )
        return buttons

    @staticmethod
    def _build_footer_v2() -> Dict:
        """2.0 没有 note 组件，用普通文本组件 + notation 字号 + grey 颜色代替。"""
        return {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": (
                    f"{CARD_FOOTER_TEXT} · "
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M')}"
                ),
                "text_size": "notation",
                "text_color": "grey",
            },
        }

    def _build_legacy_card_payload(self, message: NotificationMessage) -> Dict:
        """schema 1.0 卡片：2.0 被拒时的第二选择，老客户端兼容性更好。"""
        return {
            "msg_type": "interactive",
            "card": self._build_card_v1(message),
        }

    def _build_card_v1(self, message: NotificationMessage) -> Dict:
        if self._is_alert(message):
            header = {
                "template": CARD_TEMPLATE_ALERT,
                "title": {
                    "tag": "plain_text",
                    "content": _truncate(f"⚠️ {message.title}", CARD_TITLE_MAX_LENGTH),
                },
            }
            elements = [
                {"tag": "div", "text": {"tag": "lark_md", "content": _reason_markdown(message.reason)}},
            ]
        else:
            header = {
                "template": CARD_TEMPLATE_RECOMMEND,
                "title": {
                    "tag": "plain_text",
                    "content": _truncate(
                        message.notification_title, CARD_TITLE_MAX_LENGTH
                    ),
                },
            }
            elements = [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "\n".join(self._build_info_lines(message)),
                    },
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": _reason_markdown(message.reason)},
                },
                {"tag": "action", "actions": self._build_actions(message)},
            ]

        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": (
                            f"{CARD_FOOTER_TEXT} · "
                            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}"
                        ),
                    }
                ],
            }
        )

        return {
            "config": {"wide_screen_mode": True},
            "header": header,
            "elements": elements,
        }

    @staticmethod
    def _is_alert(message: NotificationMessage) -> bool:
        """任务异常/暂停这类通知没有商品链接，用告警样式渲染。"""
        return message.desktop_link in {"#", ""} or message.price == "N/A"

    @staticmethod
    def _build_info_lines(message: NotificationMessage) -> List[str]:
        """商品概要：价格、地区、卖家、发布时间、想要人数、标签。"""
        lines: List[str] = []

        price_line = f"💰 价格: **{message.price}**"
        if message.original_price and message.original_price not in {"N/A", "暂无", "-"}:
            price_line += f"　🏷️ 原价: {message.original_price}"
        lines.append(price_line)

        meta: List[str] = []
        if message.location and message.location != "地区未知":
            meta.append(f"📍 地区: {message.location}")
        if message.publish_time and message.publish_time != "未知时间":
            meta.append(f"🕒 发布: {message.publish_time}")
        if message.seller and message.seller != "匿名卖家":
            meta.append(f"👤 卖家: {message.seller}")
        if message.wants_count and message.wants_count != "NaN":
            meta.append(f"🔥 想要: {message.wants_count} 人")
        if message.tags:
            meta.append(f"🎁 标签: {' / '.join(message.tags)}")

        for index in range(0, len(meta), 2):
            lines.append("　".join(meta[index:index + 2]))

        return lines

    @staticmethod
    def _build_actions(message: NotificationMessage) -> List[Dict]:
        """schema 1.0 按钮：放在交互模块 action 里，链接直接写 url。"""
        actions: List[Dict] = []
        if message.mobile_link and message.mobile_link != message.desktop_link:
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "📱 手机端查看"},
                    "url": message.mobile_link,
                    "type": "primary",
                    "width": "default",
                }
            )
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "💻 电脑端查看"},
                "url": message.desktop_link,
                "type": "default",
                "width": "default",
            }
        )
        return actions


def _truncate(text: str, limit: int) -> str:
    """按长度截断文本并补省略号。"""
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


def _reason_markdown(reason: str) -> str:
    """把推荐原因渲染成卡片正文，单行时同行展示，多行时另起一行。"""
    text = (reason or "").strip()
    if len(text) > CARD_REASON_MAX_LENGTH:
        text = text[:CARD_REASON_MAX_LENGTH] + "…"
    if not text:
        return "🤖 原因: 无"
    if "\n" in text:
        return f"🤖 原因:\n{text}"
    return f"🤖 原因: {text}"

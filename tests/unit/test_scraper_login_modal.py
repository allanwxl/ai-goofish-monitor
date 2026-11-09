"""登录弹窗遮挡的处理。

背景：闲鱼的登录弹窗（``ant-modal-wrap login-modal-wrap-*``）会覆盖整个页面并
拦截所有指针事件。卖家主页采集时点击"信用及评价"页签时会被它挡住，Playwright
只能等到超时——旧实现每个卖家白白卡满 30 秒，还会把 Playwright 的长串调用栈
打进日志。修复后先尝试关闭弹窗，关闭不了就快速跳过评价采集，保留已经拿到的
头部信息和商品列表。
"""
import asyncio

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import src.scraper as scraper
from src.scraper import (
    LOGIN_MODAL_CLOSE_SELECTORS,
    LOGIN_MODAL_SELECTOR,
    _dismiss_login_modal,
)


class _FakeLocator:
    def __init__(self, count: int = 0):
        self._count = count

    async def count(self) -> int:
        return self._count


class _FakePage:
    """最小化的 page 替身，只实现 _dismiss_login_modal 用到的接口。"""

    def __init__(self, modal_count: int = 0, clickable_selectors=()):
        self._modal_count = modal_count
        self._clickable = set(clickable_selectors)
        self.clicked: list[str] = []

    def locator(self, selector: str) -> _FakeLocator:
        if selector == LOGIN_MODAL_SELECTOR:
            return _FakeLocator(self._modal_count)
        return _FakeLocator(0)

    async def click(self, selector: str, timeout=None):
        if selector in self._clickable:
            self.clicked.append(selector)
            return None
        raise PlaywrightTimeoutError(f"timeout: {selector}")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(*_args, **_kwargs):
        return None

    monkeypatch.setattr(scraper, "random_sleep", _instant)


def test_login_modal_selector_targets_the_overlay():
    assert "login-modal-wrap" in LOGIN_MODAL_SELECTOR
    # CSS module hash 是动态的，选择器不能写死
    assert "Tb8DyHnb" not in LOGIN_MODAL_SELECTOR


def test_close_selectors_all_target_inside_the_modal():
    assert LOGIN_MODAL_CLOSE_SELECTORS
    for selector in LOGIN_MODAL_CLOSE_SELECTORS:
        assert "login-modal-wrap" in selector


def test_dismiss_returns_false_when_no_modal_present():
    page = _FakePage(modal_count=0)

    assert asyncio.run(_dismiss_login_modal(page)) is False
    assert page.clicked == []


def test_dismiss_closes_modal_when_close_button_available():
    page = _FakePage(
        modal_count=1,
        clickable_selectors={LOGIN_MODAL_CLOSE_SELECTORS[0]},
    )

    assert asyncio.run(_dismiss_login_modal(page)) is True
    assert page.clicked == [LOGIN_MODAL_CLOSE_SELECTORS[0]]


def test_dismiss_tries_every_close_selector_before_giving_up():
    page = _FakePage(
        modal_count=1,
        clickable_selectors={LOGIN_MODAL_CLOSE_SELECTORS[-1]},
    )

    assert asyncio.run(_dismiss_login_modal(page)) is True
    # 前面几个选择器都点不动，最后一个才成功
    assert page.clicked == [LOGIN_MODAL_CLOSE_SELECTORS[-1]]


def test_dismiss_returns_false_when_modal_cannot_be_closed():
    page = _FakePage(modal_count=1, clickable_selectors=())

    assert asyncio.run(_dismiss_login_modal(page)) is False
    # 三个关闭选择器都尝试过
    assert len(page.clicked) == 0

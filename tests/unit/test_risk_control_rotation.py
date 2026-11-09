"""风控(baxia)轮换重试相关测试。

覆盖：
  - can_rotate_after_failure 的判定规则
  - scraper 的 RiskControlError 分支确实会在有轮换资源时重试
"""

import inspect

from src import scraper
from src.rotation import can_rotate_after_failure


class TestCanRotateAfterFailure:
    def test_disabled_rotation_never_rotates(self):
        assert can_rotate_after_failure() is False

    def test_account_rotation_requires_on_failure_mode(self):
        assert (
            can_rotate_after_failure(
                account_enabled=True,
                account_mode="per_task",
                available_account_count=3,
            )
            is False
        )

    def test_account_rotation_needs_more_than_one_available(self):
        """只剩一个账号时换不掉，轮换没有意义。"""
        assert (
            can_rotate_after_failure(
                account_enabled=True,
                account_mode="on_failure",
                available_account_count=1,
            )
            is False
        )

    def test_account_rotation_allows_retry_with_multiple_accounts(self):
        assert (
            can_rotate_after_failure(
                account_enabled=True,
                account_mode="on_failure",
                available_account_count=2,
            )
            is True
        )

    def test_forced_account_disables_account_rotation(self):
        """任务固定了账号时不能换账号，只能靠代理。"""
        assert (
            can_rotate_after_failure(
                account_enabled=True,
                account_mode="on_failure",
                available_account_count=3,
                forced_account=True,
            )
            is False
        )

    def test_proxy_rotation_allows_retry_with_multiple_proxies(self):
        assert (
            can_rotate_after_failure(
                proxy_enabled=True,
                proxy_mode="on_failure",
                available_proxy_count=2,
            )
            is True
        )

    def test_proxy_rotation_requires_on_failure_mode(self):
        assert (
            can_rotate_after_failure(
                proxy_enabled=True,
                proxy_mode="per_task",
                available_proxy_count=5,
            )
            is False
        )

    def test_proxy_rotation_needs_more_than_one_available(self):
        assert (
            can_rotate_after_failure(
                proxy_enabled=True,
                proxy_mode="on_failure",
                available_proxy_count=1,
            )
            is False
        )


class TestScraperRiskControlBranch:
    def test_risk_control_branch_checks_rotation_resource(self):
        """风控分支必须先看有没有轮换资源，再决定放弃还是重试。"""
        source = inspect.getsource(scraper)
        assert "_has_rotation_resource" in source

        branch = source.split("except RiskControlError", 1)[1]
        branch = branch.split("except Exception", 1)[0]
        assert "_has_rotation_resource()" in branch
        assert "continue" in branch
        # 没有轮换资源时仍然直接放弃，不能无限重试
        assert "break" in branch

    def test_rotation_helper_is_wired_to_shared_policy(self):
        """scraper 内部的判定必须复用 rotation 模块的同一套规则。"""
        source = inspect.getsource(scraper)
        helper = source.split("def _has_rotation_resource", 1)[1]
        helper = helper.split("async def _run_scrape_attempt", 1)[0]
        assert "can_rotate_after_failure(" in helper
        assert "available_account_count" in helper
        assert "available_proxy_count" in helper

import os
import random
import time
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class RotationItem:
    value: str
    last_error: Optional[str] = None


class RotationPool:
    def __init__(self, items: List[str], blacklist_ttl: int = 300, name: str = ""):
        self.items = [RotationItem(value=item) for item in items if item]
        self.blacklist_ttl = max(0, int(blacklist_ttl))
        self.name = name or "rotation"
        self._blacklist: Dict[str, float] = {}

    def _cleanup_blacklist(self) -> None:
        now = time.time()
        expired = [key for key, ts in self._blacklist.items() if ts <= now]
        for key in expired:
            self._blacklist.pop(key, None)

    def available_items(self) -> List[RotationItem]:
        self._cleanup_blacklist()
        return [item for item in self.items if item.value not in self._blacklist]

    def pick_random(self) -> Optional[RotationItem]:
        candidates = self.available_items()
        if not candidates:
            return None
        return random.choice(candidates)

    def mark_bad(self, item: Optional[RotationItem], reason: str = "") -> None:
        if not item:
            return
        item.last_error = reason
        if self.blacklist_ttl <= 0:
            return
        self._blacklist[item.value] = time.time() + self.blacklist_ttl


def parse_proxy_pool(value: Optional[str]) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [entry.strip() for entry in str(value).split(",") if entry.strip()]


def can_rotate_after_failure(
    *,
    account_enabled: bool = False,
    account_mode: str = "per_task",
    available_account_count: int = 0,
    forced_account: bool = False,
    proxy_enabled: bool = False,
    proxy_mode: str = "per_task",
    available_proxy_count: int = 0,
) -> bool:
    """判断失败后是否还有可切换的账号/代理资源。

    闲鱼风控(baxia)分两种成因，处理方式不同：
      - 账号/设备指纹级：换一个独立登录态的账号通常能绕开
      - IP 级：必须换代理才有效，光换账号没用

    因此只有池里确实还剩下第二个可用资源时，失败后轮换重试才有意义；
    否则应当立即放弃，避免无意义重试拖长任务。

    available_*_count 指的是"当前未被拉黑"的可用数量，
    因此 > 1 才代表换掉当前这个之后还有别的可选。
    """
    if (
        account_enabled
        and not forced_account
        and account_mode == "on_failure"
        and available_account_count > 1
    ):
        return True
    if (
        proxy_enabled
        and proxy_mode == "on_failure"
        and available_proxy_count > 1
    ):
        return True
    return False


def load_state_files(state_dir: str) -> List[str]:
    if not state_dir:
        return []
    if not os.path.isdir(state_dir):
        return []
    files = []
    for name in os.listdir(state_dir):
        if name.endswith(".json"):
            files.append(os.path.join(state_dir, name))
    return sorted(files)

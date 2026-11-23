"""前后端「人工标记在售状态」的取值契约测试。

后端 ``ManualListingStatus`` 枚举与前端 ``api/results.ts`` 的 ``listingStatus``
联合类型必须一致：

* 前端多写了后端不支持的值 → 请求直接 422，按钮点了没反应；
* 后端多开了前端永远发不出的值 → 功能加了但界面点不到。

另外锁住「结果卡片必须同时提供标记已售与标记已下架两个入口」。
"""
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROUTES_PY = PROJECT_ROOT / "src" / "api" / "routes" / "results.py"
RESULTS_API_TS = PROJECT_ROOT / "web-ui" / "src" / "api" / "results.ts"
RESULT_CARD_VUE = (
    PROJECT_ROOT / "web-ui" / "src" / "components" / "results" / "ResultCard.vue"
)


def _backend_statuses() -> set[str]:
    source = RESULTS_ROUTES_PY.read_text(encoding="utf-8")
    assert "class ManualListingStatus(str, Enum):" in source, "找不到 ManualListingStatus 枚举"
    body = source.split("class ManualListingStatus(str, Enum):", 1)[1]
    body = body.split("\n\nclass ", 1)[0].split("\n\n@", 1)[0]
    return set(re.findall(r'"([a-z_]+)"', body))


def _frontend_statuses() -> set[str]:
    source = RESULTS_API_TS.read_text(encoding="utf-8")
    match = re.search(r"listingStatus:\s*((?:'[^']+'\s*\|\s*)*'[^']+')", source)
    assert match, "找不到 updateItemListingStatus 的 listingStatus 联合类型"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_frontend_and_backend_listing_statuses_match():
    assert _frontend_statuses() == _backend_statuses()


def test_backend_accepts_sold_delisted_and_undo():
    assert _backend_statuses() == {"sold", "delisted", "unknown"}


def test_result_card_offers_sold_and_delisted_buttons():
    source = RESULT_CARD_VUE.read_text(encoding="utf-8")
    assert "results.card.markSold" in source
    assert "results.card.markDelisted" in source
    assert "results.card.unmarkDelisted" in source

"""结果查看页商品卡片网格的列数回归测试。

2026-11-23：结果页在超大屏（xl）下原本是一排 4 张卡片，
按需求改为最多一排 3 张。这里锁住这个布局约定，防止后续又被改回 4 列。
"""
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_GRID_VUE = (
    PROJECT_ROOT / "web-ui" / "src" / "components" / "results" / "ResultsGrid.vue"
)

MAX_COLUMNS_PER_ROW = 3
GRID_CLASS_PATTERN = re.compile(r"class=\"([^\"]*grid-cols-[^\"]*)\"")
COLUMN_COUNT_PATTERN = re.compile(r"(?:[a-z]+:)?grid-cols-(\d+)")


def _grid_class_strings() -> list[str]:
    source = RESULTS_GRID_VUE.read_text(encoding="utf-8")
    return GRID_CLASS_PATTERN.findall(source)


def test_results_grid_never_exceeds_three_columns_per_row():
    class_strings = _grid_class_strings()
    assert class_strings, "没有解析到 ResultsGrid.vue 的网格样式，模板结构可能变了"

    for class_string in class_strings:
        counts = [int(value) for value in COLUMN_COUNT_PATTERN.findall(class_string)]
        assert counts, f"网格样式缺少列数声明: {class_string}"
        assert max(counts) <= MAX_COLUMNS_PER_ROW, (
            f"结果页一排最多 {MAX_COLUMNS_PER_ROW} 张卡片，实际样式: {class_string}"
        )


def test_results_grid_uses_three_columns_on_large_screens():
    source = RESULTS_GRID_VUE.read_text(encoding="utf-8")
    assert "lg:grid-cols-3" in source
    assert "xl:grid-cols-4" not in source

"""
结果导出服务
"""
import csv
from io import StringIO


EXPORT_HEADERS = [
    "任务名称",
    "搜索关键字",
    "商品ID",
    "商品标题",
    "当前售价",
    "发布时间",
    "卖家昵称",
    "AI是否推荐",
    "分析来源",
    "原因",
    "价格观察次数",
    "价格最低值",
    "价格最高值",
    "市场均价",
    "性价比分数",
    "性价比标签",
    "商品状态",
    "成交价",
    "成交价口径",
    "状态判定时间",
    "商品链接",
]

LISTING_STATUS_LABELS = {
    "active": "在售",
    "sold": "已售",
    "delisted": "已下架",
    "unknown": "未知",
}

SOLD_PRICE_SOURCE_LABELS = {
    "sold_price_api": "平台成交价",
    "listing_fallback": "挂牌价估算",
}


def build_results_csv(records: list[dict]) -> str:
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_HEADERS)
    writer.writeheader()

    for record in records:
        item = record.get("商品信息", {}) or {}
        seller = record.get("卖家信息", {}) or {}
        ai_analysis = record.get("ai_analysis", {}) or {}
        price_insight = record.get("price_insight", {}) or {}
        listing_status = str(record.get("_listing_status") or "unknown")
        sold_price = record.get("_sold_price")
        sold_price_source = str(record.get("_sold_price_source") or "")
        writer.writerow(
            {
                "任务名称": record.get("任务名称", ""),
                "搜索关键字": record.get("搜索关键字", ""),
                "商品ID": item.get("商品ID", ""),
                "商品标题": item.get("商品标题", ""),
                "当前售价": item.get("当前售价", ""),
                "发布时间": item.get("发布时间", ""),
                "卖家昵称": seller.get("卖家昵称") or item.get("卖家昵称", ""),
                "AI是否推荐": "是" if ai_analysis.get("is_recommended") else "否",
                "分析来源": ai_analysis.get("analysis_source", ""),
                "原因": ai_analysis.get("reason", ""),
                "价格观察次数": price_insight.get("observation_count", ""),
                "价格最低值": price_insight.get("min_price", ""),
                "价格最高值": price_insight.get("max_price", ""),
                "市场均价": price_insight.get("market_avg_price", ""),
                "性价比分数": ai_analysis.get("value_score", price_insight.get("deal_score", "")),
                "性价比标签": ai_analysis.get("value_summary", price_insight.get("deal_label", "")),
                "商品状态": LISTING_STATUS_LABELS.get(listing_status, listing_status),
                "成交价": "" if sold_price is None else sold_price,
                "成交价口径": SOLD_PRICE_SOURCE_LABELS.get(sold_price_source, ""),
                "状态判定时间": record.get("_status_checked_at") or "",
                "商品链接": item.get("商品链接", ""),
            }
        )

    return buffer.getvalue()

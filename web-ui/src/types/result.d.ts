// Based on the data structure from web_server.py and scraper.py

export interface ProductInfo {
  "商品标题": string;
  "当前售价": string;
  "商品原价"?: string;
  "“想要”人数"?: string | number;
  "商品标签"?: string[];
  "发货地区"?: string;
  "卖家昵称"?: string;
  "商品链接": string;
  "发布时间"?: string;
  "商品ID": string;
  "商品图片列表"?: string[];
  "商品主图链接"?: string;
  "浏览量"?: string | number;
}

export interface SellerInfo {
  "卖家昵称"?: string;
  "卖家头像链接"?: string;
  "卖家个性签名"?: string;
  "卖家在售/已售商品数"?: string;
  "卖家收到的评价总数"?: string;
  "卖家信用等级"?: string;
  "买家信用等级"?: string;
  "卖家芝麻信用"?: string;
  "卖家注册时长"?: string;
  "作为卖家的好评数"?: string;
  "作为卖家的好评率"?: string;
  "作为买家的好评数"?: string;
  "作为买家的好评率"?: string;
  "卖家发布的商品列表"?: any[]; // Define more strictly if needed
  "卖家收到的评价列表"?: any[]; // Define more strictly if needed
}

export interface AiAnalysis {
  is_recommended: boolean;
  reason: string;
  analysis_source?: 'ai' | 'keyword';
  keyword_hit_count?: number;
  value_score?: number;
  value_summary?: string;
  prompt_version?: string;
  risk_tags?: string[];
  criteria_analysis?: Record<string, any>;
  matched_keywords?: string[];
  error?: string;
}

export interface PriceInsight {
  observation_count: number;
  current_price?: number | null;
  avg_price?: number | null;
  median_price?: number | null;
  min_price?: number | null;
  max_price?: number | null;
  market_avg_price?: number | null;
  market_median_price?: number | null;
  price_change_amount?: number | null;
  price_change_percent?: number | null;
  deal_score?: number | null;
  deal_label?: string;
  first_seen_at?: string | null;
  last_seen_at?: string | null;
}

export interface SoldTrendPoint {
  day: string;
  sold_count: number;
  avg_price: number | null;
  median_price: number | null;
  min_price: number | null;
  max_price: number | null;
  estimated_count: number;
}

export interface SoldSummary {
  active_count: number;
  sold_count: number;
  delisted_count: number;
  unknown_count: number;
  sold_avg_price: number | null;
  sold_median_price: number | null;
  sold_min_price: number | null;
  sold_max_price: number | null;
  estimated_count: number;
  last_sold_at?: string | null;
}

export interface ResultInsights {
  market_summary: {
    sample_count: number;
    avg_price: number | null;
    median_price: number | null;
    min_price: number | null;
    max_price: number | null;
    snapshot_time?: string | null;
  };
  history_summary: {
    unique_items: number;
    sample_count: number;
    avg_price: number | null;
    median_price: number | null;
    min_price: number | null;
    max_price: number | null;
  };
  daily_trend: Array<{
    day: string;
    sample_count: number;
    avg_price: number | null;
    median_price: number | null;
    min_price: number | null;
    max_price: number | null;
  }>;
  sold_summary?: SoldSummary;
  sold_trend?: SoldTrendPoint[];
  latest_snapshot_at?: string | null;
}

export interface ResultItem {
  "爬取时间": string;
  "搜索关键字": string;
  "任务名称": string;
  "商品信息": ProductInfo;
  "卖家信息": SellerInfo;
  ai_analysis: AiAnalysis;
  price_insight?: PriceInsight;
  _status?: 'active' | 'hidden' | 'expired';
  _effective_hidden?: boolean;
  _hidden_reason?: 'manual' | 'rule' | 'expired' | null;
  _matched_blacklist_keywords?: string[];
  /** 商品在闲鱼上的在售状态，与 _status（可见性）无关 */
  _listing_status?: 'active' | 'sold' | 'delisted' | 'unknown';
  _sold_price?: number | null;
  _sold_price_display?: string;
  _sold_price_source?: 'sold_price_api' | 'listing_fallback' | '';
  _sold_at?: string | null;
  _status_checked_at?: string | null;
  _status_source?: string;
  _status_raw_code?: number | null;
}

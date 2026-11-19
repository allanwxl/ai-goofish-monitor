-- =============================================================================
-- 清理被误判为「已售 / 已下架」的商品状态
-- =============================================================================
-- 背景：
--   旧版判定逻辑把「soldCnt > 0 或 soldPrice 有值」当成已售的充分条件，
--   而 soldPrice 在**在售商品上恒有值**（它就是当前售价），导致所有被复核到的
--   商品都被误判成「已售」，成交价列与成交价曲线全部是挂牌价冒充的假数据。
--
--   新版判定只认 itemStatus 码表（见 src/services/item_status_service.py）。
--
-- 执行前请先备份数据库（服务器容器内路径 /app/data/app.sqlite3）：
--   docker exec ai-goofish sh -c 'cp /app/data/app.sqlite3 /app/data/app.sqlite3.bak'
--
-- ⚠️ 所有 UPDATE / DELETE 都带显式 WHERE，请勿去掉 WHERE 后执行。
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 第 1 步：先看影响面（只读，不改数据）
-- -----------------------------------------------------------------------------
-- 哪些结果文件、各有多少条误判记录
SELECT result_filename,
       SUM(CASE WHEN listing_status = 'sold'     THEN 1 ELSE 0 END) AS sold_cnt,
       SUM(CASE WHEN listing_status = 'delisted' THEN 1 ELSE 0 END) AS delisted_cnt,
       COUNT(*) AS total
  FROM result_items
 WHERE listing_status IN ('sold', 'delisted')
 GROUP BY result_filename;

-- 原始状态码分布：只有当某个 status_raw_code 明确对应「在售」时，
-- 才能把该码的记录整批重置（否则会误伤真正已成交的商品）
SELECT status_raw_code,
       listing_status,
       COUNT(*) AS c,
       MIN(detected_at) AS first_detected,
       MAX(detected_at) AS last_detected
  FROM result_items
 WHERE listing_status IN ('sold', 'delisted')
 GROUP BY status_raw_code, listing_status
 ORDER BY c DESC;

-- 抽查原始响应，确认当时的判定依据到底是什么
SELECT item_id,
       from_status,
       to_status,
       status_raw_code,
       price_display AS sold_price_display,
       price_source,
       detected_at,
       substr(detail_json, 1, 900) AS detail
  FROM item_status_events
 WHERE to_status = 'sold'
 ORDER BY detected_at DESC
 LIMIT 3;


-- -----------------------------------------------------------------------------
-- 第 2 步：清理（把下面每处 '替换成第 1 步查到的结果文件名' 换成真实文件名）
-- -----------------------------------------------------------------------------

-- 2.1 删除误判产生的状态事件（成交价曲线的数据源）
DELETE FROM item_status_events
 WHERE result_filename = '替换成第 1 步查到的结果文件名'
   AND to_status IN ('sold', 'delisted');

-- 2.2 重置记录状态并清空成交价
--     status_checked_at 置空，保证下一轮复核优先挑中这批记录
UPDATE result_items
   SET listing_status = 'unknown',
       sold_price = NULL,
       sold_price_display = '',
       sold_price_source = '',
       sold_at = NULL,
       delisted_streak = 0,
       status_checked_at = '',
       status_source = NULL
 WHERE result_filename = '替换成第 1 步查到的结果文件名'
   AND listing_status IN ('sold', 'delisted');


-- -----------------------------------------------------------------------------
-- 第 3 步（可选）：按 status_raw_code 精确收敛，只清误判的那一批
-- -----------------------------------------------------------------------------
-- 校准出真实码表后，用下面的语句替代第 2 步，避免误伤真正已成交的商品。
-- 例如确认「原始码 0 其实是商品在售」后，只重置这一批：

-- DELETE FROM item_status_events
--  WHERE result_filename = '替换成第 1 步查到的结果文件名'
--    AND to_status IN ('sold', 'delisted')
--    AND status_raw_code = 0;

-- UPDATE result_items
--    SET listing_status = 'unknown',
--        sold_price = NULL,
--        sold_price_display = '',
--        sold_price_source = '',
--        sold_at = NULL,
--        delisted_streak = 0,
--        status_checked_at = '',
--        status_source = NULL
--  WHERE result_filename = '替换成第 1 步查到的结果文件名'
--    AND listing_status IN ('sold', 'delisted')
--    AND status_raw_code = 0;


-- -----------------------------------------------------------------------------
-- 第 4 步：校验（只读）
-- -----------------------------------------------------------------------------
-- 应该查不到任何 sold / delisted 的误判记录
SELECT result_filename, listing_status, COUNT(*) AS c
  FROM result_items
 WHERE listing_status IN ('sold', 'delisted')
 GROUP BY result_filename, listing_status;

-- 成交价曲线的数据源应该已经空了
SELECT result_filename, to_status, COUNT(*) AS c
  FROM item_status_events
 WHERE to_status IN ('sold', 'delisted')
 GROUP BY result_filename, to_status;

-- 重置后的记录排在复核队列最前面（status_checked_at 为空）
SELECT item_id, listing_status, status_checked_at, sold_price
  FROM result_items
 WHERE result_filename = '替换成第 1 步查到的结果文件名'
   AND listing_status = 'unknown'
   AND status_checked_at = ''
 LIMIT 10;


-- =============================================================================
-- 清理之后
-- =============================================================================
-- 被重置的记录显示为「未知」（琥珀色）——这是刻意的中间态：
-- 清理那一刻我们并不知道商品此刻的真实状态（可能刚被买走、刚下架）。
--
-- 接下来触发一次状态复核把它们重判成真实状态：
--   POST /api/results/<结果文件>/status-check
-- 需要 ITEM_STATUS_MANUAL_CHECK_ENABLED=true；单次条数上限与时间预算仍受
-- ITEM_STATUS_RECHECK_LIMIT / ITEM_STATUS_RECHECK_MAX_SECONDS 保护，
-- 没跑完的记录下一轮继续。
-- =============================================================================

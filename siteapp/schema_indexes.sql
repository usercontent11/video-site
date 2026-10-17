-- Apply once, outside a transaction if using CONCURRENTLY.
-- Existing primary keys and category relationship indexes are retained.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_videos_id_desc ON videos (id DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_vc_category_video_desc ON video_categories (category_slug,video_id DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_vc_video_category ON video_categories (video_id,category_slug);
-- No COUNT(*) per page, no random ORDER BY, no full-table sitemap scan.

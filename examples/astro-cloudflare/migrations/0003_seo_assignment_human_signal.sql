-- Human / organic signal for the SEO bandit (impression-level).
--
-- Two columns on seo_assignments, captured at SSR-render time by the edge
-- middleware (middleware.ts):
--
--   referrer_host — host of the PAGE request's Referer header, i.e. the real
--                   navigation source: 'www.google.com' for an organic search
--                   click, '' / NULL for a direct visit. This is what lets the
--                   agent score a variant on organic-search humans
--                   (SEO_IMPRESSION_MODE='organic'). The event-side
--                   referrer_host on seo_outcomes can't: that POST is
--                   same-origin, so it always records your own host.
--
--   bot_score     — Cloudflare Bot Management score at assignment time
--                   (1 = almost-certainly automated .. 99 = almost-certainly
--                   human), or NULL when the signal isn't present on the plan.
--                   Stored even when the middleware doesn't hard-gate on it, so
--                   bot contamination can be measured and filtered post-hoc.
--
-- Both default NULL so existing rows are untouched and the write path stays
-- backward compatible. Required for SEO_IMPRESSION_MODE='organic'; harmless
-- for 'engaged' (the default) and 'assignments'.

ALTER TABLE seo_assignments ADD COLUMN referrer_host TEXT;
ALTER TABLE seo_assignments ADD COLUMN bot_score INTEGER;

CREATE INDEX IF NOT EXISTS idx_seo_assignments_variant_referrer
  ON seo_assignments(variant_id, referrer_host);

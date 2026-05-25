-- SEO optimizer: variants, assignments, outcomes, GSC daily.
--
-- A "slot" is a copy surface that can be varied (e.g. home.title,
-- home.h1). page_match is a JSON predicate joining a slot to specific
-- pages — '{"path":"/example/"}' for one city, '{}' for global.
--
-- Variants live in seo_variants. The middleware reads active variants per
-- (slot, page_match), Thompson-samples one for each new visitor, and writes
-- the assignment to seo_assignments. In-page JS posts engagement events to
-- /api/seo-events which insert into seo_outcomes. A nightly Python poller
-- joins GSC pageData against seo_assignments and writes per-variant rank /
-- CTR rows to seo_gsc_daily.
--
-- See ../../seo_agent/ for the loop that proposes new variants and
-- updates the Beta posteriors (alpha, beta).

CREATE TABLE IF NOT EXISTS seo_variants (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  slot            TEXT NOT NULL,
  page_match      TEXT NOT NULL,         -- JSON predicate, e.g. '{"path":"/example/"}'
  treatment       TEXT NOT NULL,         -- JSON treatment payload (copy or schema fragment)
  status          TEXT NOT NULL,         -- 'active' | 'paused' | 'killed' | 'champion'
  created_at      TEXT NOT NULL,         -- ISO-8601 UTC
  created_by      TEXT NOT NULL,         -- 'seo_agent' | 'manual'
  hypothesis      TEXT,
  alpha           REAL NOT NULL DEFAULT 1.0,
  beta            REAL NOT NULL DEFAULT 1.0,
  impressions     INTEGER NOT NULL DEFAULT 0,
  rewards_sum     REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_seo_variants_slot_status
  ON seo_variants(slot, status);

CREATE INDEX IF NOT EXISTS idx_seo_variants_status_created
  ON seo_variants(status, created_at);

CREATE TABLE IF NOT EXISTS seo_assignments (
  session_id      TEXT NOT NULL,
  slot            TEXT NOT NULL,
  variant_id      INTEGER NOT NULL,
  page_path       TEXT NOT NULL,
  assigned_at     TEXT NOT NULL,
  PRIMARY KEY (session_id, slot, page_path)
);

CREATE INDEX IF NOT EXISTS idx_seo_assignments_variant
  ON seo_assignments(variant_id);

CREATE INDEX IF NOT EXISTS idx_seo_assignments_page_assigned
  ON seo_assignments(page_path, assigned_at);

CREATE TABLE IF NOT EXISTS seo_outcomes (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id      TEXT NOT NULL,
  variant_id      INTEGER NOT NULL,
  page_path       TEXT NOT NULL,
  event           TEXT NOT NULL,         -- 'view'|'scroll_50'|'scroll_90'|'lead_click'|'cta_click'|'dwell_30s'|'ga4_reconcile'
  value           REAL NOT NULL DEFAULT 1.0,
  recorded_at     TEXT NOT NULL,
  referrer_host   TEXT
);

CREATE INDEX IF NOT EXISTS idx_seo_outcomes_variant_recorded
  ON seo_outcomes(variant_id, recorded_at);

CREATE INDEX IF NOT EXISTS idx_seo_outcomes_recorded
  ON seo_outcomes(recorded_at);

CREATE TABLE IF NOT EXISTS seo_gsc_daily (
  variant_id      INTEGER NOT NULL,
  date            TEXT NOT NULL,         -- YYYY-MM-DD
  page_path       TEXT NOT NULL,
  impressions     INTEGER NOT NULL,
  clicks          INTEGER NOT NULL,
  position        REAL NOT NULL,
  PRIMARY KEY (variant_id, date, page_path)
);

CREATE INDEX IF NOT EXISTS idx_seo_gsc_daily_date
  ON seo_gsc_daily(date);

-- Sequential SERP evaluation state (seo_agent/serp_evaluator.py).
-- One row per champion tenure of a SERP-visible slot (<title> /
-- meta_description). variant_id NULL = the static template fallback
-- held the seat (no champion row existed). Metrics are filled from the
-- GSC API once the tenure is old enough for search data to have
-- matured (finalized=1); adjusted_ctr = ctr / expected_ctr(position).
-- The evaluator also creates this table defensively on first --commit.
CREATE TABLE IF NOT EXISTS seo_serp_tenures (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slot TEXT NOT NULL,
  variant_id INTEGER,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  impressions INTEGER,
  clicks INTEGER,
  avg_position REAL,
  adjusted_ctr REAL,
  finalized INTEGER NOT NULL DEFAULT 0,
  note TEXT
);

CREATE INDEX IF NOT EXISTS idx_serp_tenures_slot ON seo_serp_tenures (slot, ended_at);

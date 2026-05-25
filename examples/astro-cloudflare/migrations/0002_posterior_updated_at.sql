-- Add posterior_updated_at to seo_variants so the allocator can update
-- posteriors INCREMENTALLY instead of re-counting the same 24h window
-- on every tick.
--
-- Without this, _update_posteriors_for_slot ran every 10 min and added
-- the FULL last-24h aggregate of (impressions, reward) to alpha/beta
-- each time. Over 24h the same data got added ~144x. The bandit's
-- relative ranking stayed roughly correct, but the variance shrank to
-- zero — Thompson sampling devolved to greedy exploitation, no
-- exploration of newer variants.
--
-- Post-migration: each tick queries seo_assignments / seo_outcomes
-- WHERE timestamp > posterior_updated_at, applies the delta to
-- alpha/beta, then bumps posterior_updated_at to now(). True
-- incremental accounting.
--
-- BACKFILL: we set posterior_updated_at = created_at AND reset
-- alpha/beta to 1.0 + impressions/rewards_sum to 0. This wipes any
-- inflated priors from the pre-migration over-counting era. The first
-- tick post-migration accumulates each variant's full history once
-- (correctly), and subsequent ticks add only the delta.

ALTER TABLE seo_variants ADD COLUMN posterior_updated_at TEXT;

UPDATE seo_variants
   SET posterior_updated_at = created_at,
       alpha = 1.0,
       beta = 1.0,
       impressions = 0,
       rewards_sum = 0.0;

CREATE INDEX IF NOT EXISTS idx_seo_variants_posterior_updated_at
  ON seo_variants(posterior_updated_at);

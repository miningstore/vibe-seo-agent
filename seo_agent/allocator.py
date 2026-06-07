"""Bandit allocator: update Beta posteriors, classify kill/promote.

Reads engagement rows from D1, joins to assignments, computes a per-
variant reward, and updates `alpha`/`beta` on `seo_variants`. Run once
per loop tick after variant generation.

Posterior interpretation:
- `alpha` = 1 + total positive reward observed
- `beta` = 1 + total non-reward observed
- Beta(alpha, beta) is the posterior on the variant's "expected reward
  per impression", which the middleware samples from at request time.

Composite reward per impression is layered:
- 0.5 * normalize(GSC CTR delta vs champion) over last 7d
- 0.3 * normalize(GA4 engagement delta) over last 24h
- 0.2 * normalize(D1 engagement score) over last 24h

For the first 48h of a variant's life, GSC data is sparse / absent, so
the formula falls back to D1 engagement only. The loop runner can call
`composite_reward` with whatever it has.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config as cfg, d1_client


@dataclass
class VariantStats:
    variant_id: int
    slot: str
    page_match: str
    status: str
    alpha: float
    beta: float
    impressions: int
    rewards_sum: float
    created_at: str
    # Timestamp of the last posterior update. Used by the loop to
    # request engagement_score_d1 with `since=` for incremental
    # accounting (so each tick adds only NEW data, not the full
    # rolling window re-counted). Falls back to created_at when the
    # variant has never been ticked.
    posterior_updated_at: str = ""


def load_variant_stats(slot: str) -> list[VariantStats]:
    rows = d1_client.query(
        """SELECT id, slot, page_match, status, alpha, beta, impressions, rewards_sum,
                  created_at, COALESCE(posterior_updated_at, created_at) AS posterior_updated_at
           FROM seo_variants WHERE slot = ?1 AND status IN ('active','champion')""",
        [slot],
    )
    return [
        VariantStats(
            variant_id=int(r["id"]),
            slot=r["slot"],
            page_match=r["page_match"],
            status=r["status"],
            alpha=float(r["alpha"]),
            beta=float(r["beta"]),
            impressions=int(r["impressions"]),
            rewards_sum=float(r["rewards_sum"]),
            created_at=r["created_at"],
            posterior_updated_at=r["posterior_updated_at"] or r["created_at"],
        )
        for r in rows
    ]


# Default per-event reward weights. The conversion goal (Slot.goal_event)
# overrides one of these to Slot.goal_weight when scoring a variant —
# typically 5.0, making one conversion equivalent to 5 lead_clicks.
DEFAULT_WEIGHTS = {
    "view": 0.0,
    "scroll_50": 0.2,
    "scroll_90": 0.4,
    "dwell_30s": 0.4,
    "cta_click": 0.6,
    "lead_click": 1.0,
}


def engagement_score_d1(
    variant_id: int,
    hours: int = 24,
    slot: "cfg.Slot | None" = None,
    since: str | None = None,
) -> tuple[int, float]:
    """Return (impressions, weighted_reward_sum) over a window.

    Two windowing modes:

    - `since=<iso-timestamp>` (incremental, used by the loop): count
      only rows newer than the given timestamp. The loop passes each
      variant's `posterior_updated_at` here, then update_posterior
      bumps that timestamp to now(). This is what prevents the
      "every-tick re-counts the last 24h" overcounting bug.

    - `since=None` + `hours=N` (rolling window, used for ad-hoc
      reports / health checks): count rows in the last N hours. Same
      semantics as the original implementation.

    Per-event weights default to DEFAULT_WEIGHTS. When `slot.goal_event`
    is set, the goal event gets `slot.goal_weight` (default 5.0) so
    conversions dominate the reward signal once they start flowing.

    Impression source: seo_assignments, NOT view events. SSR sites
    (average-rent) get a `view` row per pageload via the in-page
    tracker; static sites (miningstore) only get a single assignment
    row when the Worker first sees a session. Assignments work for
    both — and they're the bandit's natural impression unit
    (one cookie shown one variant = one trial).
    """
    if since:
        # Wrap both sides in datetime() so SQLite normalizes the format
        # comparison. assigned_at / recorded_at are ISO 8601 written by
        # the Worker / Astro middleware (`new Date().toISOString()` =
        # 2026-05-25T17:59:12.348Z); `posterior_updated_at` is written
        # by `datetime('now')` (= 2026-05-25 17:59:12). Raw string
        # comparison treats 'T' > ' ' (ASCII 84 vs 32), so Worker
        # timestamps always test as greater than SQL cursor timestamps
        # regardless of actual time — cursor never advances. datetime()
        # on both sides parses to SQLite's internal format and
        # compares correctly.
        imp_sql = (
            "SELECT COUNT(*) AS n FROM seo_assignments "
            "WHERE variant_id = ?1 AND datetime(assigned_at) > datetime(?2)"
        )
        out_sql = (
            "SELECT event, COUNT(*) AS n FROM seo_outcomes "
            "WHERE variant_id = ?1 AND datetime(recorded_at) > datetime(?2) "
            "GROUP BY event"
        )
        imp_params = [variant_id, since]
        out_params = [variant_id, since]
    else:
        # Wrap assigned_at / recorded_at in datetime() for the SAME reason
        # the since= branch above does: those columns are ISO-8601 strings
        # written by the Worker / Astro middleware (`new Date().toISOString()`
        # = 2026-05-25T17:59:12.348Z) while datetime('now', ...) renders the
        # space-separated form (2026-05-25 17:59:12). A raw string compare
        # treats 'T' (ASCII 84) > ' ' (ASCII 32) at index 10, so every row
        # whose date-portion equals the cutoff day tests as "in window"
        # regardless of its time-of-day — silently over-counting up to a
        # full extra day of impressions/outcomes at the window boundary.
        # datetime() on both sides normalizes the comparison.
        imp_sql = (
            "SELECT COUNT(*) AS n FROM seo_assignments "
            "WHERE variant_id = ?1 "
            "AND datetime(assigned_at) > datetime('now', '-' || ?2 || ' hours')"
        )
        out_sql = (
            "SELECT event, COUNT(*) AS n FROM seo_outcomes "
            "WHERE variant_id = ?1 "
            "AND datetime(recorded_at) > datetime('now', '-' || ?2 || ' hours') "
            "GROUP BY event"
        )
        imp_params = [variant_id, hours]
        out_params = [variant_id, hours]

    imp_rows = d1_client.query(imp_sql, imp_params)
    impressions = int(imp_rows[0]["n"]) if imp_rows else 0

    weights = dict(DEFAULT_WEIGHTS)
    if slot is not None and getattr(slot, "goal_event", ""):
        weights[slot.goal_event] = float(getattr(slot, "goal_weight", 5.0))

    out_rows = d1_client.query(out_sql, out_params)
    reward = 0.0
    for r in out_rows:
        n = int(r["n"])
        ev = r["event"]
        reward += n * weights.get(ev, 0.0)
    return impressions, reward


def update_posterior(
    variant_id: int,
    new_impressions: int,
    new_reward: float,
    bump_timestamp: bool = True,
) -> None:
    """Incrementally apply (impressions, reward) to a variant's Beta posterior.

    We treat reward as a fractional positive-event count: 1 unit of
    reward = 1 unit of alpha bump, the rest of the impressions go to
    beta. Clamped to non-negative.

    When `bump_timestamp=True` (default, used by the loop), the variant's
    `posterior_updated_at` is set to now() in the same UPDATE so the
    next tick's `engagement_score_d1(since=...)` only counts events
    that arrived after this update. Set to False for ad-hoc updates
    where you don't want to shift the incremental cursor.
    """
    if new_impressions <= 0:
        return
    reward_clamped = max(0.0, min(float(new_impressions), new_reward))
    if bump_timestamp:
        # strftime ISO format matches `new Date().toISOString()` used by
        # the Worker and Astro middleware (2026-05-25T17:59:12.348Z) so
        # the cursor compares cleanly against assigned_at / recorded_at
        # under both the datetime()-normalized path in engagement_score_d1
        # AND any future raw-string comparison.
        d1_client.query(
            """UPDATE seo_variants
               SET impressions = impressions + ?2,
                   rewards_sum = rewards_sum + ?3,
                   alpha = alpha + ?3,
                   beta = beta + (?2 - ?3),
                   posterior_updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
               WHERE id = ?1""",
            [variant_id, new_impressions, reward_clamped],
        )
    else:
        d1_client.query(
            """UPDATE seo_variants
               SET impressions = impressions + ?2,
                   rewards_sum = rewards_sum + ?3,
                   alpha = alpha + ?3,
                   beta = beta + (?2 - ?3)
               WHERE id = ?1""",
            [variant_id, new_impressions, reward_clamped],
        )


def prob_beats_champion(variant: VariantStats, champion: VariantStats, samples: int = 4000) -> float:
    """Monte-Carlo probability that Beta(variant) > Beta(champion).

    For the loop runner — not for middleware. Heavyweight (~ms).
    """
    import random

    wins = 0
    for _ in range(samples):
        v = _beta_sample(variant.alpha, variant.beta, random)
        c = _beta_sample(champion.alpha, champion.beta, random)
        if v > c:
            wins += 1
    return wins / samples


def _beta_sample(a: float, b: float, rng) -> float:
    return rng.betavariate(max(1e-6, a), max(1e-6, b))

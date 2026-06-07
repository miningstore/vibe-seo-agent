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


# The on-page event that proves a real (JS-executing) pageview. In the
# 'engaged'/'organic' impression modes it is the unit of one bandit trial.
IMPRESSION_VIEW_EVENT = "view"


def engagement_score_d1(
    variant_id: int,
    hours: int = 24,
    slot: "cfg.Slot | None" = None,
    since: str | None = None,
) -> tuple[int, float]:
    """Return (impressions, weighted_reward_sum) for a variant over a window.

    Two WINDOW modes (orthogonal to the impression mode below):
    - `since=<iso-timestamp>` (incremental, used by the loop): only rows
      newer than the cursor. The loop passes each variant's
      `posterior_updated_at`; update_posterior then bumps it to now().
    - `since=None` + `hours=N` (rolling window, ad-hoc reports/health checks).

    The IMPRESSION mode (cfg.SEO_IMPRESSION_MODE) decides what one trial is:
    - 'engaged'     (default): one `view` outcome event = one impression.
      Counting JS-confirmed pageviews instead of raw seo_assignments drops
      assignments from non-JS bots that never fired the tracker and only
      diluted the posterior.
    - 'organic': as 'engaged', but restricted to pageviews whose assignment
      carried an organic-search referrer (cfg.ORGANIC_REFERRER_LIKE). Reward
      is filtered to the same sessions so the per-impression rate stays
      consistent.
    - 'assignments' (legacy): COUNT(*) of seo_assignments — includes bots.

    Reward is the weighted sum of outcome events (DEFAULT_WEIGHTS, with the
    slot's goal_event boosted to goal_weight). `view` has weight 0, so in
    engaged/organic mode a viewed-but-not-engaged pageview is one impression
    with zero reward — exactly the Beta(alpha, beta) trial we want.

    Timestamp columns are wrapped in datetime() on both sides because they
    are ISO-8601 ('2026-05-25T17:59:12.348Z') while datetime('now') is
    space-separated; a raw compare mis-orders them at the 'T'/' ' boundary.
    Placeholders are anonymous '?' (not ?N) so the variable-length organic
    clause binds positionally without a numbering clash.
    """
    mode = getattr(cfg, "SEO_IMPRESSION_MODE", "engaged")

    # Window predicate + its bound value, per table.
    if since:
        win_assign = "datetime(assigned_at) > datetime(?)"
        win_out = "datetime(o.recorded_at) > datetime(?)"
        win_param: object = since
    else:
        win_assign = "datetime(assigned_at) > datetime('now', '-' || ? || ' hours')"
        win_out = "datetime(o.recorded_at) > datetime('now', '-' || ? || ' hours')"
        win_param = hours

    # Organic EXISTS sub-clause, correlated on the outcome's session+variant.
    organic_sql = ""
    organic_params: list = []
    if mode == "organic":
        likes = list(getattr(cfg, "ORGANIC_REFERRER_LIKE", ()))
        if likes:
            ors = " OR ".join(["a.referrer_host LIKE ?"] * len(likes))
            organic_sql = (
                " AND EXISTS (SELECT 1 FROM seo_assignments a "
                "WHERE a.session_id = o.session_id "
                "AND a.variant_id = o.variant_id "
                f"AND ({ors}))"
            )
            organic_params = likes

    # --- impressions ---
    if mode == "assignments":
        imp_sql = (
            "SELECT COUNT(*) AS n FROM seo_assignments "
            f"WHERE variant_id = ? AND {win_assign}"
        )
        imp_params: list = [variant_id, win_param]
    else:
        imp_sql = (
            "SELECT COUNT(*) AS n FROM seo_outcomes o "
            f"WHERE o.variant_id = ? AND o.event = ? AND {win_out}{organic_sql}"
        )
        imp_params = [variant_id, IMPRESSION_VIEW_EVENT, win_param, *organic_params]

    imp_rows = d1_client.query(imp_sql, imp_params)
    impressions = int(imp_rows[0]["n"]) if imp_rows else 0

    weights = dict(DEFAULT_WEIGHTS)
    if slot is not None and getattr(slot, "goal_event", ""):
        weights[slot.goal_event] = float(getattr(slot, "goal_weight", 5.0))

    # --- reward (weighted events; same window + organic filter) ---
    out_sql = (
        "SELECT o.event AS event, COUNT(*) AS n FROM seo_outcomes o "
        f"WHERE o.variant_id = ? AND {win_out}{organic_sql} "
        "GROUP BY o.event"
    )
    out_params = [variant_id, win_param, *organic_params]

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

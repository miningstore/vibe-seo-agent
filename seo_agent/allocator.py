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


def load_variant_stats(slot: str) -> list[VariantStats]:
    rows = d1_client.query(
        """SELECT id, slot, page_match, status, alpha, beta, impressions, rewards_sum, created_at
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
) -> tuple[int, float]:
    """Return (impressions, weighted_reward_sum) over the last N hours.

    Per-event weights default to DEFAULT_WEIGHTS. When `slot.goal_event`
    is set, the goal event gets `slot.goal_weight` (default 5.0) so
    conversions dominate the reward signal once they start flowing.
    Pass `slot=None` for callers that don't know which slot they're
    scoring (falls back to engagement-only weights).

    Impression source: seo_assignments, NOT view events. SSR sites
    (average-rent) get a `view` row per pageload via the in-page
    tracker; static sites (miningstore) only get a single assignment
    row when the Worker first sees a session. Using assignments means
    the impression count works for both — SSR sites lose a little
    accuracy on repeat visits (one assignment row per
    session/slot/path regardless of repeat views), static sites get
    the right count instead of zero. Both keep their reward signal
    intact (engagement events still get their weights from
    DEFAULT_WEIGHTS).
    """
    imp_rows = d1_client.query(
        """SELECT COUNT(*) AS n FROM seo_assignments
           WHERE variant_id = ?1
             AND assigned_at > datetime('now', '-' || ?2 || ' hours')""",
        [variant_id, hours],
    )
    impressions = int(imp_rows[0]["n"]) if imp_rows else 0

    weights = dict(DEFAULT_WEIGHTS)
    if slot is not None and getattr(slot, "goal_event", ""):
        weights[slot.goal_event] = float(getattr(slot, "goal_weight", 5.0))

    out_rows = d1_client.query(
        """SELECT event, COUNT(*) AS n
           FROM seo_outcomes
           WHERE variant_id = ?1
             AND recorded_at > datetime('now', '-' || ?2 || ' hours')
           GROUP BY event""",
        [variant_id, hours],
    )
    reward = 0.0
    for r in out_rows:
        n = int(r["n"])
        ev = r["event"]
        reward += n * weights.get(ev, 0.0)
    return impressions, reward


def update_posterior(variant_id: int, new_impressions: int, new_reward: float) -> None:
    """Incrementally apply (impressions, reward) to a variant's Beta posterior.

    We treat reward as a fractional positive-event count: 1 unit of
    reward = 1 unit of alpha bump, the rest of the impressions go to
    beta. Clamped to non-negative.
    """
    if new_impressions <= 0:
        return
    reward_clamped = max(0.0, min(float(new_impressions), new_reward))
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

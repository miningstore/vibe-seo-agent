"""One-shot: re-baseline variant posteriors onto the clean-signal epoch.

The on-page engagement tracker typically comes online days after the optimizer
first launches: `scroll_50`/`scroll_90` (the bulk of the reward weight) only
begin firing once the tracker ships. Variants alive before that accrued reward
under a near-zero signal, so their Beta posteriors are permanently depressed
relative to later variants. Because mean = alpha/(alpha+beta) never decays, the
bandit can't recover on its own, and a mis-promoted early champion stays frozen
in place blocking stronger arms.

This script recomputes every variant's posterior from seo_outcomes counting
ONLY events at/after the stable-signal epoch, under the configured
SEO_IMPRESSION_MODE, then re-picks each slot's champion as the best arm by
posterior mean (demoting a mis-promoted champion to 'active'). It also resets
posterior_updated_at to now() so the live loop continues incrementally from a
clean cursor (no double counting).

Dry-run by default; pass --apply to write.

    python -m seo_agent.rebaseline_posteriors               # dry-run, auto epoch
    python -m seo_agent.rebaseline_posteriors --since 2026-05-27
    python -m seo_agent.rebaseline_posteriors --apply
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from . import allocator, config as cfg, d1_client


def _auto_epoch() -> str | None:
    """First scroll_50 event = when the reward signal stabilized."""
    rows = d1_client.query(
        "SELECT MIN(recorded_at) AS e FROM seo_outcomes WHERE event = 'scroll_50'"
    )
    return rows[0]["e"] if rows and rows[0].get("e") else None


def rebaseline(since: str, apply: bool) -> int:
    rows = d1_client.query(
        "SELECT id, slot, status, alpha, beta, impressions FROM seo_variants "
        "WHERE status IN ('active','champion','paused') ORDER BY slot, id"
    )
    if not rows:
        print("no variants to re-baseline")
        return 0

    mode = getattr(cfg, "SEO_IMPRESSION_MODE", "engaged")
    # isoformat() (matches how insert_variant writes created_at) — a valid,
    # SQLite-datetime()-parseable timestamp. Do NOT use
    # strftime('%Y-%m-%dT%H:%M:%fZ'): in Python %f is microseconds-only (no
    # %S), so it yields an invalid '...T19:13:477233Z' that datetime() can't
    # parse, which would freeze every re-baselined variant's incremental
    # cursor. (SQLite's strftime %f means SS.SSS, hence the mismatch.)
    now_iso = datetime.now(timezone.utc).isoformat()
    print(
        f"re-baselining {len(rows)} variants onto epoch >= {since} "
        f"(impression mode={mode}, apply={apply})\n"
    )

    by_slot: dict[str, list[dict]] = {}
    for r in rows:
        vid = int(r["id"])
        slot = cfg.get_slot(r["slot"])
        imps, reward = allocator.engagement_score_d1(vid, slot=slot, since=since)
        reward_clamped = max(0.0, min(float(imps), reward))
        new_alpha = 1.0 + reward_clamped
        new_beta = 1.0 + (imps - reward_clamped)
        new_mean = new_alpha / (new_alpha + new_beta)
        old_a, old_b = float(r["alpha"]), float(r["beta"])
        old_mean = old_a / (old_a + old_b) if (old_a + old_b) > 0 else 0.0
        print(
            f"  [{r['status']:8}] {r['slot']:30} id={vid:>4} "
            f"imp {int(r['impressions']):>6}->{imps:<6} "
            f"mean {old_mean:.3f}->{new_mean:.3f}"
        )
        by_slot.setdefault(r["slot"], []).append(
            {**r, "_imps": imps, "_mean": new_mean}
        )
        if apply:
            d1_client.query(
                "UPDATE seo_variants SET alpha=?, beta=?, impressions=?, "
                "rewards_sum=?, posterior_updated_at=? WHERE id=?",
                [new_alpha, new_beta, imps, reward_clamped, now_iso, vid],
            )

    # --- re-pick each slot's champion as the best arm by posterior mean ---
    print("\nchampion re-pick:")
    swaps = 0
    for slot_name, recs in sorted(by_slot.items()):
        eligible = [x for x in recs if x["_imps"] >= cfg.PROMOTE_MIN_IMPRESSIONS]
        cur = next((x for x in recs if x["status"] == "champion"), None)
        if not eligible:
            print(
                f"  {slot_name:30} no arm >= {cfg.PROMOTE_MIN_IMPRESSIONS} imps; "
                f"leave champion as-is"
            )
            continue
        best = max(eligible, key=lambda x: x["_mean"])
        if cur and int(cur["id"]) == int(best["id"]):
            print(f"  {slot_name:30} champion id={best['id']} already best (mean={best['_mean']:.3f})")
            continue
        msg = f"  {slot_name:30} promote id={best['id']} (mean={best['_mean']:.3f})"
        if cur:
            msg += f", demote id={cur['id']} (mean={cur['_mean']:.3f})"
        print(msg)
        swaps += 1
        if apply:
            if cur and int(cur["id"]) != int(best["id"]):
                d1_client.query("UPDATE seo_variants SET status='active' WHERE id=?", [int(cur["id"])])
            d1_client.query("UPDATE seo_variants SET status='champion' WHERE id=?", [int(best["id"])])

    print(
        f"\n{'APPLIED' if apply else 'DRY RUN'}: {len(rows)} variants re-baselined, "
        f"{swaps} champion swap(s)" + ("" if apply else "  (pass --apply to write)")
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-baseline SEO variant posteriors")
    ap.add_argument("--since", help="Clean-epoch ISO timestamp/date (default: first scroll_50 event)")
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    args = ap.parse_args()
    since = args.since or _auto_epoch()
    if not since:
        print("could not auto-detect epoch (no scroll_50 events yet); pass --since explicitly")
        return 1
    return rebaseline(since, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())

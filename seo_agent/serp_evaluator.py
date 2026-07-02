"""Sequential SERP evaluator: champion-tenure GSC-CTR testing for
SERP-visible slots (<title>, meta description).

Why this exists
---------------
The parallel Thompson bandit that runs the rest of the agent is
causally EMPTY for SERP-visible slots:

- Googlebot always renders the CHAMPION, so the snippet searchers see
  never varies with the on-site assignment split.
- A meta description is rendered nowhere on-page, so on-site engagement
  "rewards" for its arms are pure noise. A <title> is nearly as
  invisible (browser tab text).

Left on the engagement bandit, those slots promote whatever noise
favors. In the reference deployment this crowned paywall-scented
copy as the site's actual search snippets and held SERP CTR near
0.1% at page-one positions until it was caught by a manual audit.

What it does instead
--------------------
One arm at a time holds the champion seat for a tenure of
`SERP_TENURE_DAYS`. When a tenure ends, the arm's effect is measured
from Google Search Console directly: impressions, clicks, and average
position over the slot's page family (PATTERN_PATH_REGEX, set per site
in site_config.py), scored as

    adjusted_ctr = (clicks / impressions) / expected_ctr(avg_position)

so a tenure that ran at position 12 isn't penalized against one that
ran at position 6. A tenure with a NULL variant_id measures the static
template fallback (what crawlers get when no champion row exists) --
the fallback participates as a first-class arm.

Rotation: candidates that have never held a tenure go next (oldest
first). When every live arm has been measured, the best conclusive arm
is crowned champion, decisively-worse arms are killed, and the winner
keeps accumulating tenure data (so a later, better generation can
challenge it).

GSC data lags ~2-3 days, so closed tenures are only finalized (measured)
once `SERP_GSC_LAG_DAYS` have passed; winner selection uses finalized
tenures only.

State lives in the `seo_serp_tenures` D1 table (the evaluator creates
it on first --commit run; a copy of the DDL ships in
examples/astro-cloudflare/migrations/). Runs inside the agent loop
(`loop._apply_kill_promote` delegates serp_visible slots here) or
standalone:

    python -m seo_agent.serp_evaluator                 # dry-run, all serp slots
    python -m seo_agent.serp_evaluator --commit        # apply
    python -m seo_agent.serp_evaluator --slot home.title
"""
from __future__ import annotations

import argparse
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import config as cfg, d1_client

log = logging.getLogger("seo_agent.serp_evaluator")


# ---------------------------------------------------------------------------
# Expected CTR by SERP position. Coarse industry-shaped curve; absolute
# accuracy doesn't matter because every tenure is scored against the SAME
# curve — it only needs to be monotone and roughly proportionate so that
# position drift between tenures doesn't masquerade as snippet quality.
_EXPECTED_CTR = {
    1: 0.28, 2: 0.15, 3: 0.10, 4: 0.07, 5: 0.05,
    6: 0.040, 7: 0.032, 8: 0.026, 9: 0.022, 10: 0.019,
}


def expected_ctr(position: float) -> float:
    if position <= 1:
        return _EXPECTED_CTR[1]
    if position < 10:
        lo = int(math.floor(position))
        hi = lo + 1
        frac = position - lo
        return _EXPECTED_CTR[lo] * (1 - frac) + _EXPECTED_CTR[hi] * frac
    # Long tail: decay toward a floor. Positions past ~30 are all
    # "effectively unclicked" territory; the floor keeps the adjustment
    # from exploding for deep positions.
    return max(0.004, _EXPECTED_CTR[10] * math.exp(-0.12 * (position - 10)))


# ---------------------------------------------------------------------------
# D1 state


def table_exists() -> bool:
    rows = d1_client.query(
        "SELECT 1 AS x FROM sqlite_master WHERE type='table' AND name='seo_serp_tenures'"
    )
    return bool(rows)


def ensure_table(commit: bool) -> None:
    """Create seo_serp_tenures if missing (DDL also in examples/astro-cloudflare/migrations)."""
    if not commit:
        return
    d1_client.query(
        """CREATE TABLE IF NOT EXISTS seo_serp_tenures (
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
           )"""
    )
    d1_client.query(
        "CREATE INDEX IF NOT EXISTS idx_serp_tenures_slot ON seo_serp_tenures (slot, ended_at)"
    )


@dataclass
class Tenure:
    id: int
    slot: str
    variant_id: int | None
    started_at: str
    ended_at: str | None
    impressions: int | None
    clicks: int | None
    avg_position: float | None
    adjusted_ctr: float | None
    finalized: int
    note: str | None


def _row_to_tenure(r: dict) -> Tenure:
    return Tenure(
        id=int(r["id"]),
        slot=r["slot"],
        variant_id=int(r["variant_id"]) if r["variant_id"] is not None else None,
        started_at=r["started_at"],
        ended_at=r["ended_at"],
        impressions=int(r["impressions"]) if r["impressions"] is not None else None,
        clicks=int(r["clicks"]) if r["clicks"] is not None else None,
        avg_position=float(r["avg_position"]) if r["avg_position"] is not None else None,
        adjusted_ctr=float(r["adjusted_ctr"]) if r["adjusted_ctr"] is not None else None,
        finalized=int(r["finalized"]),
        note=r.get("note"),
    )


def _open_tenure(slot_name: str) -> Tenure | None:
    rows = d1_client.query(
        "SELECT * FROM seo_serp_tenures WHERE slot = ?1 AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
        [slot_name],
    )
    return _row_to_tenure(rows[0]) if rows else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_days(iso_at: str) -> float:
    ts = datetime.fromisoformat(iso_at.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0


# ---------------------------------------------------------------------------
# GSC aggregation


def _path_from_url(url: str) -> str | None:
    m = re.match(r"^https?://[^/]+(/.*)$", url)
    if not m:
        return None
    path = m.group(1).split("?")[0].split("#")[0]
    if not path.endswith("/"):
        path += "/"
    return path


def gather_gsc(pattern: str, start_iso: str, end_iso: str) -> tuple[int, int, float]:
    """(impressions, clicks, weighted_avg_position) for a page family
    over [start, end] calendar dates, straight from the GSC API."""
    from . import gsc_client
    from .gsc_poller import _site_url

    rx = re.compile(cfg.PATTERN_PATH_REGEX[pattern])
    start_date = start_iso[:10]
    end_date = end_iso[:10]
    imp = 0
    clk = 0
    pos_weighted = 0.0
    for row in gsc_client.fetch_page_daily(_site_url(), start_date, end_date):
        keys = row.get("keys") or []
        if len(keys) < 2:
            continue
        path = _path_from_url(keys[1])
        if path is None or not rx.match(path):
            continue
        r_imp = int(row.get("impressions", 0))
        imp += r_imp
        clk += int(row.get("clicks", 0))
        pos_weighted += float(row.get("position", 0.0)) * r_imp
    wpos = (pos_weighted / imp) if imp > 0 else 0.0
    return imp, clk, wpos


# ---------------------------------------------------------------------------
# Core state machine


def _current_champion_id(slot_name: str) -> int | None:
    rows = d1_client.query(
        "SELECT id FROM seo_variants WHERE slot = ?1 AND status = 'champion' ORDER BY id LIMIT 1",
        [slot_name],
    )
    return int(rows[0]["id"]) if rows else None


def _active_arm_ids(slot_name: str) -> list[int]:
    rows = d1_client.query(
        "SELECT id FROM seo_variants WHERE slot = ?1 AND status = 'active' ORDER BY created_at, id",
        [slot_name],
    )
    return [int(r["id"]) for r in rows]


def _tenured_variant_ids(slot_name: str) -> set[int | None]:
    rows = d1_client.query(
        "SELECT DISTINCT variant_id FROM seo_serp_tenures WHERE slot = ?1",
        [slot_name],
    )
    return {int(r["variant_id"]) if r["variant_id"] is not None else None for r in rows}


def _set_status(variant_id: int, status: str, commit: bool) -> None:
    if not commit:
        log.info("DRY RUN: would set variant %d -> %s", variant_id, status)
        return
    d1_client.query("UPDATE seo_variants SET status = ?1 WHERE id = ?2", [status, variant_id])


def _start_tenure(slot_name: str, variant_id: int | None, note: str, commit: bool) -> None:
    if not commit:
        log.info("DRY RUN: would open tenure slot=%s variant=%s (%s)", slot_name, variant_id, note)
        return
    d1_client.query(
        "INSERT INTO seo_serp_tenures (slot, variant_id, started_at, note) VALUES (?1, ?2, ?3, ?4)",
        [slot_name, variant_id, _now_iso(), note],
    )
    log.info("opened tenure slot=%s variant=%s (%s)", slot_name, variant_id, note)


def _close_tenure(tenure: Tenure, note: str, commit: bool) -> None:
    if not commit:
        log.info("DRY RUN: would close tenure id=%d (%s)", tenure.id, note)
        return
    d1_client.query(
        "UPDATE seo_serp_tenures SET ended_at = ?1, note = COALESCE(note, '') || ' | ' || ?2 WHERE id = ?3",
        [_now_iso(), note, tenure.id],
    )
    log.info("closed tenure id=%d slot=%s variant=%s (%s)", tenure.id, tenure.slot, tenure.variant_id, note)


def finalize_closed_tenures(slot: "cfg.Slot", commit: bool) -> None:
    """Measure every closed-but-unfinalized tenure old enough for GSC
    data to have matured. Safe to call repeatedly."""
    rows = d1_client.query(
        """SELECT * FROM seo_serp_tenures
           WHERE slot = ?1 AND ended_at IS NOT NULL AND finalized = 0""",
        [slot.name],
    )
    for r in rows:
        t = _row_to_tenure(r)
        if _age_days(t.ended_at) < cfg.SERP_GSC_LAG_DAYS:
            log.info("tenure id=%d closed %.1fd ago — waiting for GSC lag window", t.id, _age_days(t.ended_at))
            continue
        imp, clk, wpos = gather_gsc(slot.pattern, t.started_at, t.ended_at)
        adj = None
        if imp >= cfg.SERP_TENURE_MIN_IMPRESSIONS and wpos > 0:
            adj = (clk / imp) / expected_ctr(wpos)
        if not commit:
            log.info(
                "DRY RUN: would finalize tenure id=%d imp=%d clk=%d wpos=%.1f adj=%s",
                t.id, imp, clk, wpos, f"{adj:.3f}" if adj is not None else "inconclusive",
            )
            continue
        d1_client.query(
            """UPDATE seo_serp_tenures
               SET impressions = ?1, clicks = ?2, avg_position = ?3, adjusted_ctr = ?4, finalized = 1
               WHERE id = ?5""",
            [imp, clk, wpos, adj, t.id],
        )
        log.info(
            "finalized tenure id=%d slot=%s variant=%s imp=%d clk=%d wpos=%.1f adj=%s",
            t.id, t.slot, t.variant_id, imp, clk, wpos,
            f"{adj:.3f}" if adj is not None else "inconclusive",
        )


def _aggregate_by_arm(slot_name: str) -> dict[int | None, dict]:
    """Aggregate finalized tenures per arm: total imp/clk, weighted pos,
    recomputed adjusted CTR. Only conclusive aggregates get a score."""
    rows = d1_client.query(
        """SELECT variant_id, SUM(impressions) AS imp, SUM(clicks) AS clk,
                  SUM(avg_position * impressions) AS posw
           FROM seo_serp_tenures
           WHERE slot = ?1 AND finalized = 1 AND impressions IS NOT NULL
           GROUP BY variant_id""",
        [slot_name],
    )
    out: dict[int | None, dict] = {}
    for r in rows:
        vid = int(r["variant_id"]) if r["variant_id"] is not None else None
        imp = int(r["imp"] or 0)
        clk = int(r["clk"] or 0)
        wpos = (float(r["posw"]) / imp) if imp > 0 else 0.0
        score = None
        if imp >= cfg.SERP_TENURE_MIN_IMPRESSIONS and wpos > 0:
            score = (clk / imp) / expected_ctr(wpos)
        out[vid] = {"impressions": imp, "clicks": clk, "wpos": wpos, "score": score}
    return out


def apply(slot: "cfg.Slot", commit: bool = True) -> None:
    """One evaluation pass for a serp_visible slot. Idempotent; all state
    is in D1. Called from the loop's kill/promote path and the CLI."""
    if not getattr(slot, "serp_visible", False):
        log.warning("slot %s is not serp_visible — skipping", slot.name)
        return
    if slot.pattern not in cfg.PATTERN_PATH_REGEX:
        log.warning("slot %s pattern %r has no PATTERN_PATH_REGEX entry — skipping", slot.name, slot.pattern)
        return

    ensure_table(commit)
    if not table_exists():
        # Dry-run before the first --commit: no state exists, so the
        # only possible move is opening the baseline tenure.
        log.info(
            "DRY RUN: seo_serp_tenures missing — would open baseline tenure "
            "slot=%s variant=%s", slot.name, _current_champion_id(slot.name),
        )
        return
    finalize_closed_tenures(slot, commit)

    champion_id = _current_champion_id(slot.name)
    open_t = _open_tenure(slot.name)

    # External intervention (manual kill/promote in D1) invalidates the
    # open tenure: what Googlebot saw changed mid-window.
    if open_t and open_t.variant_id != champion_id:
        _close_tenure(open_t, f"champion changed externally to {champion_id}", commit)
        open_t = None

    if open_t is None:
        _start_tenure(slot.name, champion_id, "baseline" if champion_id is None else "tenure", commit)
        return

    age = _age_days(open_t.started_at)
    if age < cfg.SERP_TENURE_DAYS:
        log.info(
            "slot=%s tenure id=%d (variant=%s) day %.1f/%d — holding",
            slot.name, open_t.id, open_t.variant_id, age, cfg.SERP_TENURE_DAYS,
        )
        return

    # Tenure complete: close it, then rotate or crown.
    _close_tenure(open_t, "tenure complete", commit)

    tenured = _tenured_variant_ids(slot.name)
    candidates = [vid for vid in _active_arm_ids(slot.name) if vid not in tenured]

    if candidates:
        next_id = candidates[0]
        if champion_id is not None:
            _set_status(champion_id, "paused", commit)
        _set_status(next_id, "champion", commit)
        _start_tenure(slot.name, next_id, "rotation", commit)
        log.info("slot=%s rotating champion %s -> %s (%d candidates left)",
                 slot.name, champion_id, next_id, len(candidates) - 1)
        return

    # Every live arm has been tenured. Crown the best conclusive arm.
    # NOTE: winner selection happens on a LATER pass than the close above
    # when the just-closed tenure hasn't been finalized yet — that's
    # deliberate. A new tenure for the current champion opens below, and
    # the crowning comparison re-runs at its end with full data.
    agg = _aggregate_by_arm(slot.name)
    scored = {vid: a for vid, a in agg.items() if a["score"] is not None}
    if not scored:
        _start_tenure(slot.name, champion_id, "re-measure (no conclusive tenures yet)", commit)
        return

    best_vid, best = max(scored.items(), key=lambda kv: kv[1]["score"])
    log.info(
        "slot=%s arm scores: %s",
        slot.name,
        {str(v): round(a["score"], 3) for v, a in sorted(scored.items(), key=lambda kv: -kv[1]["score"])},
    )

    # Kill decisively-worse arms (conclusive volume, clearly below best).
    # The fallback pseudo-arm (None) can't be killed; a losing champion is
    # paused rather than killed only if it IS the winner's predecessor —
    # otherwise killed like any other loser.
    live_statuses = {
        int(r["id"]): r["status"]
        for r in d1_client.query(
            "SELECT id, status FROM seo_variants WHERE slot = ?1 AND status IN ('active','champion','paused')",
            [slot.name],
        )
    }
    for vid, a in scored.items():
        if vid is None or vid == best_vid:
            continue
        if a["score"] < best["score"] * cfg.SERP_LOSER_KILL_RATIO and vid in live_statuses:
            _set_status(vid, "killed", commit)
            log.info("slot=%s killed arm %d (adj %.3f vs best %.3f)", slot.name, vid, a["score"], best["score"])

    if best_vid is None:
        # The static template fallback wins: no champion row at all.
        if champion_id is not None:
            _set_status(champion_id, "paused", commit)
        _start_tenure(slot.name, None, "fallback crowned", commit)
        log.info("slot=%s fallback literal crowned (adj %.3f)", slot.name, best["score"])
        return

    if best_vid != champion_id:
        if champion_id is not None:
            _set_status(champion_id, "paused", commit)
        if live_statuses.get(best_vid) in ("active", "paused", "champion"):
            _set_status(best_vid, "champion", commit)
            _start_tenure(slot.name, best_vid, "crowned best", commit)
            log.info("slot=%s crowned %d (adj %.3f)", slot.name, best_vid, best["score"])
        else:
            _start_tenure(slot.name, None, "winner no longer live; fallback", commit)
        return

    # Champion is already the best arm: keep measuring it.
    _start_tenure(slot.name, champion_id, "champion re-tenure", commit)


# ---------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Sequential SERP champion-tenure evaluator")
    ap.add_argument("--commit", action="store_true", help="apply changes (default: dry-run)")
    ap.add_argument("--slot", help="run a single slot (default: every serp_visible slot)")
    args = ap.parse_args()

    slots = [s for s in cfg.SLOTS if getattr(s, "serp_visible", False)]
    if args.slot:
        slots = [s for s in slots if s.name == args.slot]
        if not slots:
            log.error("no serp_visible slot named %r", args.slot)
            return 2
    for slot in slots:
        try:
            apply(slot, commit=args.commit)
        except Exception as e:
            log.error("slot %s failed: %s", slot.name, e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

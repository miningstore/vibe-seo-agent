"""Main loop for the SEO optimizer.

Lifecycle of one tick:

1. Pick a target slot under exploration budget.
   Priority: enabled slots with `<3` active variants for the current
   page_match family; then slots with one variant > 1k impressions but
   no decisive winner yet (re-explore).

2. Build the context block (recent GSC, current champion, GA4 hint).

3. Invoke Claude CLI to propose 2–4 variants.

4. Validate each variant against the slot's hard rules. Reject any
   that fail; if no variants survive, log and skip to next tick.

5. Insert surviving variants into seo_variants (status='active'). The
   middleware picks them up on the next cache-bucket refresh (~60s).

6. Update Beta posteriors for ALL active variants for the slot family
   based on the last 24h of D1 engagement events.

7. Apply kill/promote rules:
   - Kill if posterior P(beats champion) < 5% AND impressions >= 2000.
   - Promote if posterior P(beats champion) > 95% AND impressions >=
     5000 AND age >= 14 days.

Usage:
    python -m seo_agent.loop                    # run continuously
    python -m seo_agent.loop --dry-run          # one tick, no D1 writes
    python -m seo_agent.loop --iterations 3     # bounded runs
    python -m seo_agent.loop --slot apartments_listing.h1   # restrict to one slot
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import allocator, config as cfg, d1_client, variant_generator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("seo_agent")


def _is_night() -> bool:
    h = datetime.now(ZoneInfo("America/Chicago")).hour
    return cfg.NIGHT_START_HOUR <= h < cfg.NIGHT_END_HOUR


def _spend_today_usd() -> float:
    path = cfg.METRICS_DIR / "spending.json"
    if not path.exists():
        return 0.0
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return 0.0
    today = datetime.utcnow().date().isoformat()
    return float(data.get(today, 0.0))


def _record_spend(amount: float) -> None:
    path = cfg.METRICS_DIR / "spending.json"
    cfg.METRICS_DIR.mkdir(parents=True, exist_ok=True)
    data: dict[str, float] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            data = {}
    today = datetime.utcnow().date().isoformat()
    data[today] = float(data.get(today, 0.0)) + amount
    path.write_text(json.dumps(data, indent=2))


def _variants_today() -> int:
    path = cfg.METRICS_DIR / "history.jsonl"
    if not path.exists():
        return 0
    today = datetime.utcnow().date().isoformat()
    n = 0
    with path.open() as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("kind") == "variant_inserted" and row.get("at", "").startswith(today):
                n += 1
    return n


def _append_history(event: dict) -> None:
    path = cfg.METRICS_DIR / "history.jsonl"
    cfg.METRICS_DIR.mkdir(parents=True, exist_ok=True)
    event = {**event, "at": datetime.now(timezone.utc).isoformat()}
    with path.open("a") as f:
        f.write(json.dumps(event) + "\n")


# --- Target picking -----------------------------------------------------

def _pick_target(commit: bool) -> tuple[cfg.Slot, dict] | None:
    """Return (slot, page_match) for the next experiment to run.

    Without --commit we only consult the in-process config; we don't
    query D1 for active counts so the dry-run path doesn't need DB
    creds. With --commit we count active variants in D1 and skip slots
    that are already saturated.
    """
    enabled = cfg.enabled_slots()
    if not enabled:
        return None
    # Shuffle so we don't always hit the same slot first.
    random.shuffle(enabled)
    for slot in enabled:
        page_match = _default_page_match(slot)
        if not commit:
            return slot, page_match
        try:
            n = d1_client.count_active_variants(slot.name, json.dumps(page_match))
        except d1_client.D1Error as e:
            log.warning("D1 count failed for %s: %s", slot.name, e)
            continue
        if n < cfg.MAX_ACTIVE_VARIANTS_PER_SLOT:
            return slot, page_match
    return None


def _default_page_match(slot: cfg.Slot) -> dict:
    # Launch all slots globally (empty page_match). Per-city targeting
    # comes later once we have GSC data confirming city-level variance.
    return {}


# --- Context block ------------------------------------------------------

def _context_block(slot: cfg.Slot, page_match: dict) -> str:
    # In v1 we hand Claude a brief paragraph telling it the launch
    # situation. Once gsc_poller.py is wired, this also includes the
    # latest GSC top-queries / striking-distance summary. For now,
    # leave a clear placeholder Claude can act on.
    return f"""
The site is **average-rent.com**. The launch surface for this slot is
the `{slot.pattern}` page family. We have just enabled this slot, so
no GSC variant-level data exists yet; propose variants from first
principles using the rubric.

If you want recent traffic data, call `mcp__analytics-mcp__run_report`
ONCE with `dimensions=['pagePath']`, `metrics=['screenPageViews',
'engagementRate','averageSessionDuration']`, and a `pagePath` filter
matching `/city/.*/{slot.pattern.replace('_', '/')}/?$` if your
analytics MCP supports regex; otherwise use the literal path
`/city/austin-tx/apartments/` (Austin is our best-data city).
""".strip()


# --- One tick -----------------------------------------------------------

def run_one_tick(commit: bool) -> bool:
    """Run a single optimizer tick. Returns True if work was done."""
    if commit and _spend_today_usd() >= cfg.DAILY_SPEND_CAP_USD:
        log.info("daily spend cap reached; idle")
        return False
    if commit and _variants_today() >= cfg.MAX_VARIANTS_GENERATED_PER_DAY:
        log.info("daily variant cap reached; idle")
        return False

    target = _pick_target(commit=commit)
    if not target:
        log.info("no eligible slot under cap; idle")
        return False

    slot, page_match = target
    log.info("picked target: slot=%s page_match=%s", slot.name, json.dumps(page_match))

    champion = _load_champion(slot, page_match) if commit else None
    ctx = _context_block(slot, page_match)

    result = variant_generator.generate(
        slot=slot,
        page_match=page_match,
        champion_treatment=champion,
        context_block=ctx,
        dry_run=not commit,
    )

    surviving = [v for v in result.variants if not v.validation_errors]
    rejected = [v for v in result.variants if v.validation_errors]

    log.info(
        "generation: proposed=%d surviving=%d rejected=%d spend_usd=%.4f",
        len(result.variants), len(surviving), len(rejected), result.spent_usd,
    )
    for v in rejected:
        log.warning("rejected variant errors=%s hypothesis=%r",
                    v.validation_errors, v.hypothesis[:120])

    if commit:
        _record_spend(result.spent_usd)

    if not surviving:
        _append_history({"kind": "no_variants", "slot": slot.name, "page_match": page_match})
        return False

    if not commit:
        # Dry-run: print what we would have inserted, return.
        log.info("DRY RUN — would insert %d variants:", len(surviving))
        for v in surviving:
            log.info("  treatment=%s hypothesis=%r",
                     json.dumps(v.treatment)[:200], v.hypothesis[:120])
        return True

    inserted_ids: list[int] = []
    for v in surviving:
        try:
            new_id = d1_client.insert_variant(
                slot=slot.name,
                page_match=json.dumps(page_match),
                treatment_json=json.dumps(v.treatment),
                hypothesis=v.hypothesis,
            )
            inserted_ids.append(new_id)
            _append_history({
                "kind": "variant_inserted",
                "variant_id": new_id,
                "slot": slot.name,
                "page_match": page_match,
                "hypothesis": v.hypothesis,
            })
            log.info("inserted variant id=%d", new_id)
        except d1_client.D1Error as e:
            log.error("insert failed: %s", e)

    if commit:
        _update_posteriors_for_slot(slot)
        _apply_kill_promote(slot)

    return bool(inserted_ids)


def _load_champion(slot: cfg.Slot, page_match: dict) -> dict | None:
    try:
        rows = d1_client.query(
            """SELECT treatment FROM seo_variants
               WHERE slot = ?1 AND page_match = ?2 AND status = 'champion'
               LIMIT 1""",
            [slot.name, json.dumps(page_match)],
        )
    except d1_client.D1Error as e:
        log.warning("champion load failed: %s", e)
        return None
    if not rows:
        return None
    try:
        return json.loads(rows[0]["treatment"])
    except json.JSONDecodeError:
        return None


def _update_posteriors_for_slot(slot: cfg.Slot) -> None:
    stats = allocator.load_variant_stats(slot.name)
    for s in stats:
        imps, reward = allocator.engagement_score_d1(s.variant_id, hours=24)
        if imps > 0:
            allocator.update_posterior(s.variant_id, imps, reward)


def _apply_kill_promote(slot: cfg.Slot) -> None:
    stats = allocator.load_variant_stats(slot.name)
    if not stats:
        return
    champion = next((s for s in stats if s.status == "champion"), None)
    if not champion:
        # No champion yet — first variant to cross the promote bar becomes one.
        for s in stats:
            if s.impressions >= cfg.PROMOTE_MIN_IMPRESSIONS:
                _promote(s.variant_id)
                log.info("promoted first champion variant_id=%d", s.variant_id)
                return
        return

    for s in stats:
        if s.variant_id == champion.variant_id or s.status != "active":
            continue
        p_beat = allocator.prob_beats_champion(s, champion)
        if s.impressions >= cfg.KILL_MIN_IMPRESSIONS and p_beat < cfg.KILL_BEATS_PROB:
            _set_status(s.variant_id, "killed")
            log.info("killed variant_id=%d p_beat=%.3f", s.variant_id, p_beat)
            continue
        age_days = _age_days(s.created_at)
        if (
            s.impressions >= cfg.PROMOTE_MIN_IMPRESSIONS
            and age_days >= cfg.PROMOTE_MIN_DAYS
            and p_beat > cfg.PROMOTE_BEATS_PROB
        ):
            _set_status(champion.variant_id, "paused")
            _promote(s.variant_id)
            log.info("promoted variant_id=%d p_beat=%.3f age=%.1fd",
                     s.variant_id, p_beat, age_days)


def _set_status(variant_id: int, status: str) -> None:
    d1_client.query(
        "UPDATE seo_variants SET status = ?1 WHERE id = ?2",
        [status, variant_id],
    )


def _promote(variant_id: int) -> None:
    _set_status(variant_id, "champion")
    _append_history({"kind": "promoted", "variant_id": variant_id})


def _age_days(iso_at: str) -> float:
    try:
        ts = datetime.fromisoformat(iso_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    now = datetime.now(timezone.utc)
    return (now - ts).total_seconds() / 86400.0


# --- Main loop ----------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Run one tick without D1 writes; stub the Claude call too.")
    parser.add_argument("--iterations", type=int, default=0,
                        help="Run this many ticks then exit (0 = run forever).")
    parser.add_argument("--slot", default=None, help="Restrict to this slot name.")
    args = parser.parse_args()

    if args.slot:
        keep = args.slot
        for s in cfg.SLOTS:
            s.enabled = s.name == keep
        log.info("restricted to slot %s (enabled=%s)", keep,
                 [s.name for s in cfg.enabled_slots()])

    commit = not args.dry_run

    if args.dry_run:
        log.info("DRY RUN: one tick, no D1 writes, stubbed Claude.")
        run_one_tick(commit=False)
        return 0

    iteration = 0
    while True:
        try:
            worked = run_one_tick(commit=commit)
        except KeyboardInterrupt:
            log.info("interrupted; exiting")
            return 0
        except Exception:
            log.exception("tick failed; backing off")
            worked = False

        iteration += 1
        if args.iterations and iteration >= args.iterations:
            return 0

        if not worked:
            log.info("idle; sleeping %ds", cfg.IDLE_COOLDOWN_S)
            time.sleep(cfg.IDLE_COOLDOWN_S)
        else:
            cooldown = cfg.NIGHT_COOLDOWN_S if _is_night() else cfg.MIN_COOLDOWN_S
            log.info("tick done; sleeping %ds", cooldown)
            time.sleep(cooldown)


if __name__ == "__main__":
    sys.exit(main())

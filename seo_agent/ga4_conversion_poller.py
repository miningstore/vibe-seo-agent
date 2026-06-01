"""GA4 conversion poller — nightly.

Queries GA4 Data API for goal events (configured per-slot via
`Slot.goal_event`) over the last N days, joins each event to a row in
`seo_assignments` via the `_arx` cookie value (passed to GA4 as a
custom event parameter), and writes one row per matched event into
`seo_outcomes`. The agent's allocator then reads those rows the same
way it reads on-page engagement events.

Why this exists (vs Worker-side capture):
- miningstore.com is a static Astro build behind a CF Worker. The
  Worker already swaps title/H1/meta tags but doesn't have a hot path
  to record per-visitor outcomes without injecting client JS.
- GA4 already captures custom events at gtag-call time and is fast
  enough for daily reconciliation.
- This poller is the agent's only way to know about conversions on
  static sites until a richer Worker-side event channel ships.

Required GA4 setup (one-time, see grant_ga4_access.py for the API
calls to register conversions + register the custom dimension):
1. Site fires `gtag('event', '<goal_event>', { arx: <cookie>, page })`
   at the moment of conversion intent.
2. GA4 → Admin → Custom Definitions → register `arx` as an
   event-scoped custom dimension (so the Data API surfaces it).
3. GA4 → Admin → Events → mark `<goal_event>` as a Conversion
   (for UI reporting; not required for this poller to work).

Run nightly via `systemd/seo-ga4-conversion-poller.timer` at 03:30
local (10 min after gsc_poller, so the poller sees today's freshly-
attributed assignments without contending for D1 connections).

CLI:
    python -m seo_agent.ga4_conversion_poller             # last 1 day
    python -m seo_agent.ga4_conversion_poller --days 7    # backfill
    python -m seo_agent.ga4_conversion_poller --dry-run   # no D1 writes
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from . import config as cfg, d1_client, ga4_client

log = logging.getLogger("seo_agent.ga4_conversion_poller")


def _goal_events_from_config() -> set[str]:
    """All unique goal_event names declared across the loaded SLOTS."""
    return {
        getattr(s, "goal_event", "") for s in cfg.SLOTS
        if getattr(s, "goal_event", "")
    }


def _arx_dimension_name() -> str:
    """GA4 custom dimension name for the _arx cookie. Must match what
    the site sends and what's registered in GA4 Admin → Custom
    Definitions. Override via env if your dim has a different
    registered name (e.g. 'arx_cookie')."""
    return os.environ.get("GA4_ARX_DIMENSION", "customEvent:arx")


def fetch_conversion_events(
    property_id: str,
    days: int,
    goal_events: set[str],
) -> list[dict]:
    """Run a GA4 Data API report for the configured goal events,
    returning a list of dicts:
        {event_name, arx, page_path, event_count, date}
    """
    if not goal_events:
        log.info("no goal_event values configured in any slot — nothing to query")
        return []

    from google.analytics.data_v1beta import BetaAnalyticsDataClient  # type: ignore
    from google.analytics.data_v1beta.types import (  # type: ignore
        DateRange, Dimension, Filter, FilterExpression, FilterExpressionList,
        Metric, RunReportRequest,
    )

    creds = ga4_client._load_creds()
    client = BetaAnalyticsDataClient(credentials=creds)

    end = date.today()
    start = end - timedelta(days=days)
    arx_dim = _arx_dimension_name()

    # Filter to ONLY our goal events; lets GA4 do the heavy filtering.
    event_filter = FilterExpression(
        filter=Filter(
            field_name="eventName",
            in_list_filter=Filter.InListFilter(values=sorted(goal_events)),
        )
    )

    req = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=start.isoformat(), end_date=end.isoformat())],
        dimensions=[
            Dimension(name="eventName"),
            Dimension(name=arx_dim),
            Dimension(name="pagePath"),
            Dimension(name="date"),
        ],
        metrics=[Metric(name="eventCount")],
        dimension_filter=event_filter,
        limit=100000,
    )
    resp = client.run_report(req)

    rows = []
    for row in resp.rows:
        vals = [dv.value for dv in row.dimension_values]
        event_name, arx, page_path, dt = vals[0], vals[1], vals[2], vals[3]
        count = int(row.metric_values[0].value or 0)
        # Skip rows with no arx — they can't be variant-attributed.
        # Skip the placeholder GA4 uses when the dim is unset, e.g. "(not set)".
        if not arx or arx == "(not set)":
            continue
        rows.append({
            "event_name": event_name,
            "arx": arx,
            "page_path": page_path,
            "date": dt,                # GA4 returns YYYYMMDD
            "count": count,
        })
    return rows


def attribute_and_write(rows: list[dict], dry_run: bool = False) -> int:
    """For each GA4 conversion row, credit every variant the session
    was ever assigned to on-or-before the conversion date, then insert
    one row per (variant, session, conversion-date) into seo_outcomes.

    Why session-level (not per-page): conversions almost always happen
    on a dedicated funnel page (e.g. /contact/) where no variant slots
    are configured. A strict (session_id, page_path) join therefore
    drops nearly every real conversion. The visitor's earlier exposure
    to variants on content pages is what caused the conversion, so the
    right credit is "every variant this session saw up to today".

    The `assigned_at <= conversion_date` clause prevents back-attributing
    to assignments minted after the conversion (e.g. a returning user
    re-rolled into new variants the next day).

    Returns the number of rows written."""
    if not rows:
        return 0

    written = 0
    skipped_no_assignment = 0
    skipped_already_recorded = 0

    for r in rows:
        # Bucket the GA4 date YYYYMMDD into an ISO timestamp at noon UTC.
        # (We don't know the exact event time; GA4's daily aggregation
        # buckets to a date. Noon UTC is a reasonable placeholder.)
        ts = f"{r['date'][:4]}-{r['date'][4:6]}-{r['date'][6:8]}T12:00:00Z"
        conv_date = ts[:10]  # YYYY-MM-DD

        # Credit every variant this session was assigned to anywhere on
        # the site on-or-before the conversion date. The on-page tracker
        # already records engagement events per-page; this poller adds
        # the conversion signal that GA4 owns (because /contact/ etc.
        # has no variant slot to fire a client-side event from).
        assigns = d1_client.query(
            """SELECT variant_id, slot, page_path
               FROM seo_assignments
               WHERE session_id = ?1
                 AND date(assigned_at) <= date(?2)""",
            [r["arx"], conv_date],
        )
        if not assigns:
            skipped_no_assignment += 1
            continue

        for a in assigns:
            # Idempotency: skip if we've already recorded a conversion
            # of this event for this (variant_id, session_id, date). The
            # GA4 nightly query can legitimately overlap with previous
            # runs (--days 7 etc), and we don't want duplicate writes.
            existing = d1_client.query(
                """SELECT 1 FROM seo_outcomes
                   WHERE variant_id = ?1
                     AND session_id = ?2
                     AND event = ?3
                     AND date(recorded_at) = date(?4)
                   LIMIT 1""",
                [a["variant_id"], r["arx"], r["event_name"], ts],
            )
            if existing:
                skipped_already_recorded += 1
                continue

            if dry_run:
                log.info(
                    "DRY: would write %s for variant_id=%s session=%s slot_page=%s (converted_on=%s)",
                    r["event_name"], a["variant_id"], r["arx"][:8],
                    a["page_path"], r["page_path"],
                )
                written += 1
                continue

            # page_path stored in seo_outcomes is the page where the
            # variant was SHOWN, not the page where the conversion
            # happened. This matches how on-page engagement outcomes
            # are recorded and keeps slot/page semantics consistent.
            d1_client.query(
                """INSERT INTO seo_outcomes
                   (session_id, variant_id, page_path, event, value, recorded_at, referrer_host)
                   VALUES (?1, ?2, ?3, ?4, 1.0, ?5, 'ga4')""",
                [r["arx"], a["variant_id"], a["page_path"], r["event_name"], ts],
            )
            written += 1

    log.info(
        "wrote=%d skipped_no_assignment=%d skipped_already_recorded=%d",
        written, skipped_no_assignment, skipped_already_recorded,
    )
    return written


def poll(days: int = 1, dry_run: bool = False) -> int:
    goal_events = _goal_events_from_config()
    log.info("goal_events to query: %s", sorted(goal_events) or "(none)")
    if not goal_events:
        return 0

    property_id = os.environ.get("GA4_PROPERTY_ID", "").strip()
    if not property_id:
        log.error("GA4_PROPERTY_ID not set")
        return 0

    from . import gsc_client as _gsc  # imported here so the module top doesn't pay the cost on dry-run
    try:
        rows = fetch_conversion_events(property_id, days=days, goal_events=goal_events)
    except _gsc.OAuthRevokedError as e:
        log.error("OAuth revoked/missing — skipping conversion poll.\n%s", e)
        return 0
    except Exception as e:
        log.error("GA4 conversion fetch failed: %s", e)
        return 0
    log.info("fetched %d GA4 conversion-event rows over %d days", len(rows), days)
    return attribute_and_write(rows, dry_run=dry_run)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1, help="lookback window (default 1)")
    ap.add_argument("--dry-run", action="store_true", help="don't write to D1")
    args = ap.parse_args()
    poll(days=args.days, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

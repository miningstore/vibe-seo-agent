"""GSC poller: nightly pull, attribute to variants, write seo_gsc_daily.

Uses the service-account client at `gsc_client.py` (no OAuth flow).
Queries `searchanalytics.query` for the last 7 days with
`dimensions=['date','page']`, and writes one row per (variant_id, date,
page_path) into D1's `seo_gsc_daily` table.

Variant attribution: for each (date, page) GSC row, find which variants
on that path are currently active or champion. We treat GSC clicks as
"the page got X clicks across all assigned visitors on that day", and
split clicks across the variants in proportion to each variant's
session share in `seo_assignments` for that day. This is an
approximation — GSC doesn't expose per-visitor session_ids — but it's
the best we can do without a UTM-tag-style per-variant URL, which we
explicitly avoid because Google may discount tagged URLs.

Run via:
    python -m seo_agent.gsc_poller             # last 7 days
    python -m seo_agent.gsc_poller --days 28   # backfill window
    python -m seo_agent.gsc_poller --list-sites  # diagnostic
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from . import config as cfg, d1_client, gsc_client

log = logging.getLogger("seo_agent.gsc_poller")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _site_url() -> str:
    """The GSC site_url must match the verified property exactly. Use the
    format shown in Search Console:
      • URL-prefix property: `https://yoursite.com/` (note trailing slash)
      • Domain property:     `sc-domain:yoursite.com` (no scheme, no path)
    Find yours via `python -m seo_agent.gsc_client --list-sites`.
    """
    import os

    url = os.environ.get("GSC_SITE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "GSC_SITE_URL not set. Add it to your .env, e.g.\n"
            "    GSC_SITE_URL=https://yoursite.com/\n"
            "or for a domain property:\n"
            "    GSC_SITE_URL=sc-domain:yoursite.com"
        )
    return url


def poll(days: int = 7, attribute: bool = True) -> int:
    """Pull GSC data and write per-variant daily rows. Returns rows written.

    When `attribute` is False, fetch GSC data and print a per-page
    summary, but skip the D1 attribution step. Useful for local smoke
    tests where CF_ACCOUNT_ID / CF_D1_DATABASE_ID aren't set.
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    log.info("polling GSC from %s to %s", start, end)

    try:
        rows_iter = gsc_client.fetch_page_daily(
            site_url=_site_url(),
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )
    except Exception as e:
        log.error("GSC fetch failed: %s", e)
        return 0

    # Group by (date, page_path). The API returns one row per (date,
    # page) so this dedup is a defensive merge, not a roll-up.
    daily: dict[tuple[str, str], dict] = {}
    for row in rows_iter:
        keys = row.get("keys") or []
        if len(keys) < 2:
            continue
        date_iso, page = keys[0], keys[1]
        path = _path_from_url(page)
        if path is None:
            continue
        daily[(date_iso, path)] = {
            "clicks": int(row.get("clicks", 0)),
            "impressions": int(row.get("impressions", 0)),
            "position": float(row.get("position", 0.0)),
        }

    if not attribute:
        # Smoke-test path: summarize what we got, no D1 writes.
        total_imp = sum(s["impressions"] for s in daily.values())
        total_clk = sum(s["clicks"] for s in daily.values())
        log.info(
            "no-attribute mode: %d (date,page) rows, %d impressions, %d clicks",
            len(daily), total_imp, total_clk,
        )
        # Top 10 pages by impressions, for sanity.
        by_page: dict[str, dict] = {}
        for (_, page_path), s in daily.items():
            cur = by_page.setdefault(page_path, {"impressions": 0, "clicks": 0})
            cur["impressions"] += s["impressions"]
            cur["clicks"] += s["clicks"]
        top = sorted(by_page.items(), key=lambda kv: -kv[1]["impressions"])[:10]
        for path, s in top:
            log.info("  %s impressions=%d clicks=%d", path, s["impressions"], s["clicks"])
        return len(daily)

    # Early-out: if no assignments exist at all, every per-row
    # attribution query would return empty. Skip the 5000+ wasted D1
    # round-trips and log a summary instead. Once variants launch and
    # seo_assignments starts populating, the full attribution path
    # automatically re-engages.
    has_assignments = d1_client.query(
        "SELECT EXISTS(SELECT 1 FROM seo_assignments LIMIT 1) AS x"
    )
    if not has_assignments or not has_assignments[0].get("x"):
        log.info(
            "no variants assigned yet — fetched %d (date,page) GSC rows, "
            "skipping per-variant attribution. These rows will be "
            "attributed on the next poll after variants launch.",
            len(daily),
        )
        return 0

    written = 0
    for (date_iso, page_path), stats in daily.items():
        attributions = _attribute_clicks(page_path, date_iso, stats)
        for variant_id, share in attributions.items():
            d1_client.query(
                """INSERT INTO seo_gsc_daily (variant_id, date, page_path, impressions, clicks, position)
                   VALUES (?1, ?2, ?3, ?4, ?5, ?6)
                   ON CONFLICT(variant_id, date, page_path) DO UPDATE SET
                     impressions = excluded.impressions,
                     clicks = excluded.clicks,
                     position = excluded.position""",
                [
                    variant_id,
                    date_iso,
                    page_path,
                    int(round(stats["impressions"] * share)),
                    int(round(stats["clicks"] * share)),
                    stats["position"],
                ],
            )
            written += 1
    log.info("wrote %d rows", written)
    return written


def _path_from_url(url: str) -> str | None:
    if not isinstance(url, str):
        return None
    if url.startswith("/"):
        return url
    if "://" in url:
        try:
            from urllib.parse import urlparse

            return urlparse(url).path or None
        except Exception:
            return None
    return None


def _attribute_clicks(page_path: str, date_iso: str, stats: dict) -> dict[int, float]:
    """Return {variant_id: share} where shares sum to ~1.0.

    Splits proportionally to how many distinct sessions saw each variant
    on this page on this day, as recorded in seo_assignments.
    """
    rows = d1_client.query(
        """SELECT variant_id, COUNT(DISTINCT session_id) AS sessions
           FROM seo_assignments
           WHERE page_path = ?1
             AND date(assigned_at) = ?2
           GROUP BY variant_id""",
        [page_path, date_iso],
    )
    if not rows:
        return {}
    total = sum(int(r["sessions"]) for r in rows) or 1
    return {int(r["variant_id"]): int(r["sessions"]) / total for r in rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--list-sites",
        action="store_true",
        help="Diagnostic: print every site this credential can read.",
    )
    parser.add_argument(
        "--no-attribute",
        action="store_true",
        help="Skip D1 variant attribution (no Cloudflare creds needed). "
             "Useful for local smoke-tests — prints a top-pages summary.",
    )
    args = parser.parse_args()
    if args.list_sites:
        for s in gsc_client.list_sites():
            print(s)
        return 0
    poll(days=args.days, attribute=not args.no_attribute)
    return 0


if __name__ == "__main__":
    sys.exit(main())

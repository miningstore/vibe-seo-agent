"""Google Analytics 4 Data API client.

Auth model: same OAuth Desktop client + cached refresh token as
`gsc_client.py`. The bootstrap there requests both `webmasters.readonly`
and `analytics.readonly` in one consent flow, so a single token file
unlocks both APIs.

Why OAuth user (and not the official analytics-mcp service account):
- Symmetric with GSC. One token file, one bootstrap, one identity to
  audit. Cuts ops surface area in half.
- Reusable across vibe-coding projects: every project that owns its own
  Google data already has an Editor user (the owner). OAuth as that
  user gives instant access to every GA4 property they're on, with no
  Admin-API grant dance per property.
- Google's official MCP is still fine for *interactive* Claude Code
  use, but for automation on the VPS the optimizer's loop calls this
  module directly — one less moving part, no `npx -y` package on the
  hot path.

Usage in the optimizer's eval step:

    from seo_agent import ga4_client
    rows = ga4_client.fetch_page_engagement(
        property_id="320487533",
        page_paths=["/city/austin-tx/", "/city/denver-co/trends/"],
        days=7,
    )
    # rows: { path: {screenPageViews, engagementRate, avgSessionDuration, conversions} }
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from typing import Iterable

log = logging.getLogger("seo_agent.ga4_client")

# Shared with gsc_client.py — the bootstrap requests both scopes.
GA4_SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
]
DEFAULT_TOKEN = "credentials/gsc-oauth-token.json"  # same cache as GSC


def _token_path() -> str:
    return os.environ.get("GSC_OAUTH_TOKEN_FILE", DEFAULT_TOKEN)


def _default_property() -> str:
    pid = os.environ.get("GA4_PROPERTY_ID", "").strip()
    if not pid:
        raise RuntimeError(
            "GA4_PROPERTY_ID not set. Find your property ID via:\n"
            "    python -m seo_agent.ga4_client --list-properties\n"
            "then add to your .env, e.g. GA4_PROPERTY_ID=529645777"
        )
    return pid


def _load_creds():
    """Load the shared cached OAuth creds. We don't run the interactive
    flow from here — that lives in gsc_client.py --bootstrap and is
    the single point where the user grants both scopes."""
    from google.oauth2.credentials import Credentials  # type: ignore
    from google.auth.transport.requests import Request  # type: ignore

    token_path = _token_path()
    if not os.path.exists(token_path):
        raise RuntimeError(
            f"OAuth token not cached at {token_path}. "
            "Run on a machine with a browser:\n"
            "  python -m seo_agent.gsc_client --bootstrap\n"
            "(the bootstrap grants both GSC and GA4 scopes in one consent)"
        )
    # Loading with GA4_SCOPES (not the full GSC_SCOPES set) is fine —
    # the loader uses the file's actual granted scopes, not the
    # parameter, for refresh.
    creds = Credentials.from_authorized_user_file(token_path, GA4_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    # Verify the GA4 scope is actually granted (e.g. token minted
    # before we added analytics.readonly to the bootstrap).
    granted = set(getattr(creds, "scopes", []) or [])
    if not any("analytics" in s for s in granted):
        raise RuntimeError(
            "OAuth token at "
            f"{token_path} does not include analytics.readonly. "
            "Re-run `python -m seo_agent.gsc_client --bootstrap` "
            "to grant the GA4 scope."
        )
    return creds


def list_account_summaries() -> list[dict]:
    """Diagnostic: list every GA4 account+property the credentials can
    see. Returns a list of {account_name, account_display_name, properties:
    [{property, display_name}]}.
    """
    from google.analytics.admin_v1beta import AnalyticsAdminServiceClient  # type: ignore

    creds = _load_creds()
    client = AnalyticsAdminServiceClient(credentials=creds)
    out = []
    for summary in client.list_account_summaries():
        props = [
            {"property": p.property, "display_name": p.display_name}
            for p in summary.property_summaries
        ]
        out.append({
            "account": summary.account,
            "display_name": summary.display_name,
            "properties": props,
        })
    return out


def fetch_page_engagement(
    page_paths: Iterable[str],
    days: int = 7,
    property_id: str | None = None,
) -> dict[str, dict]:
    """Run a single GA4 report aggregating engagement by pagePath.

    Returns: {path: {screenPageViews, engagementRate, averageSessionDuration,
                     conversions, sessions}}

    Paths not in the GA4 data over the window are absent from the result
    (treat as zero). The optimizer's eval step handles that — a brand-new
    variant with no GA4 traffic yet falls back to D1 engagement.
    """
    from google.analytics.data_v1beta import BetaAnalyticsDataClient  # type: ignore
    from google.analytics.data_v1beta.types import (  # type: ignore
        DateRange, Dimension, Filter, FilterExpression, FilterExpressionList,
        Metric, RunReportRequest,
    )

    paths_list = list(page_paths)
    if not paths_list:
        return {}

    creds = _load_creds()
    client = BetaAnalyticsDataClient(credentials=creds)
    end = date.today()
    start = end - timedelta(days=days)
    prop = property_id or _default_property()

    # Filter to only the paths we care about — avoids pulling the whole
    # site and post-filtering. GA4's IN_LIST is exactly what we want.
    page_filter = FilterExpression(
        filter=Filter(
            field_name="pagePath",
            in_list_filter=Filter.InListFilter(values=paths_list),
        )
    )

    req = RunReportRequest(
        property=f"properties/{prop}",
        date_ranges=[DateRange(start_date=start.isoformat(), end_date=end.isoformat())],
        dimensions=[Dimension(name="pagePath")],
        metrics=[
            Metric(name="screenPageViews"),
            Metric(name="engagementRate"),
            Metric(name="averageSessionDuration"),
            Metric(name="conversions"),
            Metric(name="sessions"),
        ],
        dimension_filter=page_filter,
        limit=10000,
    )
    resp = client.run_report(req)

    out: dict[str, dict] = {}
    for row in resp.rows:
        path = row.dimension_values[0].value
        vals = [mv.value for mv in row.metric_values]
        out[path] = {
            "screenPageViews": int(vals[0] or 0),
            "engagementRate": float(vals[1] or 0.0),
            "averageSessionDuration": float(vals[2] or 0.0),
            "conversions": float(vals[3] or 0.0),
            "sessions": int(vals[4] or 0),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="GA4 client utilities")
    ap.add_argument("--list-properties", action="store_true",
                    help="List every GA4 account+property the OAuth user can see.")
    ap.add_argument("--sample", action="store_true",
                    help="Fetch engagement for a few sample paths over 7 days.")
    ap.add_argument("--property", help="Override property ID (default: $GA4_PROPERTY_ID or 320487533)")
    args = ap.parse_args()

    if args.list_properties:
        for acct in list_account_summaries():
            print(f"\nAccount: {acct['display_name']} ({acct['account']})")
            for p in acct["properties"]:
                print(f"  {p['property']:35s} {p['display_name']}")
        return 0
    if args.sample:
        sample_paths = ["/city/austin-tx/", "/city/denver-co/trends/", "/", "/search/"]
        rows = fetch_page_engagement(sample_paths, days=7, property_id=args.property)
        if not rows:
            print("(no rows — either no traffic on these paths or filter missed)")
            return 0
        for path, m in sorted(rows.items(), key=lambda kv: -kv[1]["screenPageViews"]):
            print(
                f"{path:50s} "
                f"views={m['screenPageViews']:>5d} "
                f"sessions={m['sessions']:>4d} "
                f"engagement={m['engagementRate']:.2%} "
                f"dur={m['averageSessionDuration']:.1f}s "
                f"conv={m['conversions']:.0f}"
            )
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

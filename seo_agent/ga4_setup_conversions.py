"""One-time GA4 setup helper for the conversion poller.

Registers your goal events as Conversions in GA4 (so they show up in
the GA4 UI's Conversions report) and verifies the `arx` custom
dimension is registered. Both are required for the conversion poller
to attribute variant performance.

Run once per project, after you've defined `goal_event` values on
your slots and your site is firing the gtag events:

    python -m seo_agent.ga4_setup_conversions \\
        --property 295790490 \\
        --events book_call,quote_request

The script is idempotent — re-running on already-registered events
returns OK with a no-op message. The Admin API requires Editor on the
property; use the same OAuth flow as gsc_client (the shared token
file granted both webmasters.readonly + analytics.readonly +
analytics.edit if you registered all three scopes).
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--property", required=True, help="GA4 property ID, e.g. 295790490")
    ap.add_argument(
        "--events",
        required=True,
        help="Comma-separated goal event names to mark as Conversions "
             "(e.g. 'book_call,quote_request')",
    )
    ap.add_argument(
        "--arx-dim",
        default="arx",
        help="Event-scoped custom dimension name for the _arx cookie. "
             "Must match what the site sends. Default 'arx'.",
    )
    args = ap.parse_args()

    try:
        from google.analytics.admin_v1beta import AnalyticsAdminServiceClient
        from google.analytics.admin_v1beta.types import (
            ConversionEvent,
            CustomDimension,
        )
    except ImportError:
        print(
            "Missing dep. Install with:\n  pip install google-analytics-admin",
            file=sys.stderr,
        )
        return 1

    client = AnalyticsAdminServiceClient()
    parent = f"properties/{args.property}"

    # 1. Register custom dimension for `arx` if not already present.
    print(f"\n=== Checking custom dimension '{args.arx_dim}' ===")
    existing_dims = list(client.list_custom_dimensions(parent=parent))
    have_arx = any(d.parameter_name == args.arx_dim for d in existing_dims)
    if have_arx:
        print(f"  OK: dimension '{args.arx_dim}' already registered")
    else:
        try:
            d = client.create_custom_dimension(
                parent=parent,
                custom_dimension=CustomDimension(
                    parameter_name=args.arx_dim,
                    display_name="SEO Agent session (_arx)",
                    description="The _arx cookie value, joined to seo_assignments for variant attribution",
                    scope=CustomDimension.DimensionScope.EVENT,
                ),
            )
            print(f"  CREATED: {d.name}")
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            return 1

    # 2. Register each goal event as a Conversion.
    print(f"\n=== Marking events as Conversions ===")
    existing_conv = {c.event_name for c in client.list_conversion_events(parent=parent)}
    for event_name in (e.strip() for e in args.events.split(",") if e.strip()):
        if event_name in existing_conv:
            print(f"  OK (no-op): '{event_name}' already a Conversion")
            continue
        try:
            c = client.create_conversion_event(
                parent=parent,
                conversion_event=ConversionEvent(event_name=event_name),
            )
            print(f"  CREATED: {c.name}")
        except Exception as e:
            msg = str(e)
            if "ALREADY_EXISTS" in msg.upper():
                print(f"  OK (no-op): '{event_name}'")
            else:
                print(f"  FAILED ({event_name}): {e}", file=sys.stderr)
                return 1

    print("\nDONE. Next steps:")
    print("  1. Make sure your site fires gtag('event', '<event>', { arx: <cookie> })")
    print("  2. Wait 24-48h for GA4 to ingest events with the new dimension")
    print("  3. Run `python -m seo_agent.ga4_conversion_poller --days 1`")
    print("     and verify rows land in D1 seo_outcomes")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""One-shot: grant a service account VIEWER access on a GA4 property.

Why this exists: as of 2024, GA4's UI rejects service-account emails with
"This email doesn't match a Google Account." The fix is to call the
Analytics Admin API directly, which still accepts SA principals — Google
just blocked the UI path, not the API path.

Auth model:
- This script authenticates AS YOU (an OAuth user with GA4 Editor on the
  property), via `gcloud auth application-default login`. It then
  ADDS the service account to the property's access bindings.
- We can't have the SA add itself — only a property Editor can grant
  access bindings.

One-time setup:
    gcloud auth application-default login \\
        --scopes=https://www.googleapis.com/auth/analytics.edit,\\
                 https://www.googleapis.com/auth/cloud-platform

Run:
    python -m seo_agent.grant_ga4_access \\
        --property 320487533 \\
        --service-account gsc-reader@miningstore-seo-ops.iam.gserviceaccount.com \\
        --role viewer

Verify after:
    python -c "from google.analytics.admin_v1beta import AnalyticsAdminServiceClient; \\
               c = AnalyticsAdminServiceClient(); \\
               print([b.user for b in c.list_access_bindings(parent='properties/320487533')])"
"""
from __future__ import annotations

import argparse
import sys

ROLE_MAP = {
    "viewer": "predefinedRoles/viewer",
    "analyst": "predefinedRoles/analyst",
    "editor": "predefinedRoles/editor",
    "admin": "predefinedRoles/admin",
    # no-cost-data and no-revenue-data are exclusions, layered on top of a role
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--property", required=True, help="GA4 property ID, e.g. 320487533")
    ap.add_argument("--service-account", required=True, help="SA email to grant")
    ap.add_argument("--role", default="viewer", choices=ROLE_MAP.keys())
    args = ap.parse_args()

    try:
        from google.analytics.admin_v1beta import AnalyticsAdminServiceClient
        from google.analytics.admin_v1beta.types import AccessBinding
    except ImportError:
        print(
            "Missing dep. Install with:\n"
            "  pip install google-analytics-admin",
            file=sys.stderr,
        )
        return 1

    client = AnalyticsAdminServiceClient()
    parent = f"properties/{args.property}"
    binding = AccessBinding(
        user=args.service_account,
        roles=[ROLE_MAP[args.role]],
    )
    try:
        result = client.create_access_binding(parent=parent, access_binding=binding)
        print(f"OK: granted {args.role} on {parent} to {args.service_account}")
        print(f"     binding name: {result.name}")
        return 0
    except Exception as e:
        msg = str(e)
        if "ALREADY_EXISTS" in msg or "already has access" in msg.lower():
            print(f"OK (no-op): {args.service_account} already has access on {parent}")
            return 0
        print(f"FAILED: {e}", file=sys.stderr)
        if "PERMISSION_DENIED" in msg:
            print(
                "\nThis usually means your OAuth user doesn't have Editor on the "
                "property. Run `gcloud auth application-default login` as a user "
                "that has Editor permission on the GA4 property, then retry.",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())

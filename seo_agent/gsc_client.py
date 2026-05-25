"""Google Search Console client. Supports OAuth user creds (primary) and
service-account creds (fallback).

Why OAuth user is primary:
- Since 2024, Google blocks new service-account additions to GSC via the
  UI with "email not found." Group workaround is also blocked.
- The property owner (jp@miningstore.com) already has Owner access, so
  OAuth as that user requires zero GSC permission changes — ever.
- Reusable across vibe-coding projects: create one OAuth Desktop client
  per project (or reuse one), run the bootstrap flow once locally to
  mint a refresh token, copy that token to the VPS.

Auth mode is selected by env var:
    GSC_AUTH_MODE=oauth     (default; reads secrets + cached token)
    GSC_AUTH_MODE=service   (legacy; reads a grandfathered SA key)

Paths (env-overridable):
    GSC_OAUTH_SECRETS_FILE   default: credentials/gsc-oauth-secrets.json
    GSC_OAUTH_TOKEN_FILE     default: credentials/gsc-oauth-token.json
    GSC_SERVICE_ACCOUNT_PATH default: credentials/gsc_service_account.json

Bootstrap (one-time, on a machine with a browser):
    python -m seo_agent.gsc_client --bootstrap

That command runs InstalledAppFlow.run_local_server(), opens the
default browser to Google's consent screen, and writes the refresh
token to GSC_OAUTH_TOKEN_FILE. Subsequent calls (including on the VPS
after you scp the token file over) refresh silently without a browser.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Iterable

log = logging.getLogger("seo_agent.gsc_client")

# Scopes the bootstrap requests. We ask for both Search Console and GA4
# read access at once so a single OAuth flow + cached token covers
# everything the optimizer needs. Google's docs recommend bundling
# scopes in the consent prompt rather than re-prompting per API.
GSC_SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",   # Search Console
    "https://www.googleapis.com/auth/analytics.readonly",    # GA4 Data API (read)
    "https://www.googleapis.com/auth/analytics.edit",        # GA4 Admin API (register conversions + custom dimensions, see ga4_setup_conversions.py)
]
DEFAULT_SECRETS = "credentials/gsc-oauth-secrets.json"
DEFAULT_TOKEN = "credentials/gsc-oauth-token.json"
DEFAULT_SA_KEY = "credentials/gsc_service_account.json"


def _auth_mode() -> str:
    return os.environ.get("GSC_AUTH_MODE", "oauth").lower()


def _secrets_path() -> str:
    return os.environ.get("GSC_OAUTH_SECRETS_FILE", DEFAULT_SECRETS)


def _token_path() -> str:
    return os.environ.get("GSC_OAUTH_TOKEN_FILE", DEFAULT_TOKEN)


def _sa_key_path() -> str:
    return os.environ.get("GSC_SERVICE_ACCOUNT_PATH", DEFAULT_SA_KEY)


def _load_oauth_creds(interactive: bool):
    """Load cached OAuth creds, refresh if expired. If `interactive`,
    fall back to running the local-server consent flow when no cached
    token exists or the refresh-token has been revoked.
    """
    from google.oauth2.credentials import Credentials  # type: ignore
    from google.auth.transport.requests import Request  # type: ignore

    token_path = _token_path()
    creds = None

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, GSC_SCOPES)

    needs_consent = creds is None or (not creds.valid and not creds.refresh_token)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            log.warning("token refresh failed: %s", e)
            needs_consent = True

    if needs_consent:
        if not interactive:
            raise RuntimeError(
                f"No usable OAuth token at {token_path}. "
                "Run on a machine with a browser:\n"
                "  python -m seo_agent.gsc_client --bootstrap\n"
                f"Then copy {token_path} to this host."
            )
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore

        secrets_path = _secrets_path()
        if not os.path.exists(secrets_path):
            raise RuntimeError(
                f"OAuth Desktop client secrets not found at {secrets_path}. "
                "Download from GCP Console → APIs & Services → Credentials → "
                "your Desktop OAuth client → DOWNLOAD JSON."
            )
        flow = InstalledAppFlow.from_client_secrets_file(secrets_path, GSC_SCOPES)
        # port=0 picks a free local port for the loopback redirect.
        # access_type='offline' ensures we get a refresh_token.
        creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    # Persist (refreshed access_token + refresh_token if updated).
    os.makedirs(os.path.dirname(token_path) or ".", exist_ok=True)
    with open(token_path, "w") as f:
        f.write(creds.to_json())
    os.chmod(token_path, 0o600)
    return creds


def _load_sa_creds():
    from google.oauth2 import service_account  # type: ignore

    key = _sa_key_path()
    if not os.path.exists(key):
        raise RuntimeError(f"GSC service account key not found at {key}.")
    return service_account.Credentials.from_service_account_file(key, scopes=GSC_SCOPES)


def _build_service(interactive: bool = False):
    """Lazy import so the rest of the optimizer doesn't require these
    deps to be installed (dry-run on workstations skips this path).
    """
    from googleapiclient.discovery import build  # type: ignore

    mode = _auth_mode()
    if mode == "service":
        creds = _load_sa_creds()
    else:
        creds = _load_oauth_creds(interactive=interactive)
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def fetch_page_daily(
    site_url: str,
    start_date: str,
    end_date: str,
    row_limit: int = 25000,
) -> Iterable[dict]:
    """Yield rows from searchanalytics.query with dimensions=['date','page']."""
    service = _build_service()
    start_row = 0
    while True:
        body = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": ["date", "page"],
            "rowLimit": row_limit,
            "startRow": start_row,
        }
        resp = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        rows = resp.get("rows", [])
        if not rows:
            return
        for row in rows:
            yield row
        if len(rows) < row_limit:
            return
        start_row += len(rows)


def list_sites() -> list[str]:
    """Diagnostic: list every site this credential has access to. Useful
    after bootstrapping OAuth — should include https://average-rent.com/
    (the user's owned property)."""
    service = _build_service()
    resp = service.sites().list().execute()
    return [s["siteUrl"] for s in resp.get("siteEntry", [])]


def _bootstrap() -> int:
    """One-time browser-based consent flow. Run locally, then scp the
    token to the VPS."""
    print(f"OAuth mode: {_auth_mode()}")
    print(f"Secrets file: {_secrets_path()}")
    print(f"Token file: {_token_path()}")
    print()
    print("Building service (will open browser for consent)...")
    svc = _build_service(interactive=True)
    sites = svc.sites().list().execute().get("siteEntry", [])
    print(f"\nOAuth bootstrap successful. Visible sites ({len(sites)}):")
    for s in sites:
        print(f"  - {s['siteUrl']:60s} permission={s.get('permissionLevel','?')}")
    if not sites:
        print("  (none — make sure you signed in as a Google account that owns at")
        print("   least one GSC property)")
    print(f"\nToken cached at {_token_path()}. To run on the VPS:")
    print(f"  scp {_token_path()} vps:/home/ubuntu/apartment-pricer/{_token_path()}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="GSC client utilities")
    ap.add_argument("--bootstrap", action="store_true",
                    help="Run the OAuth consent flow (opens a browser). "
                         "Run this once locally before deploying to the VPS.")
    ap.add_argument("--list-sites", action="store_true",
                    help="List every GSC property the current credentials can read.")
    args = ap.parse_args()
    if args.bootstrap:
        return _bootstrap()
    if args.list_sites:
        for s in list_sites():
            print(s)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

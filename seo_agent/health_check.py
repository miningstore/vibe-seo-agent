"""End-to-end data-access dry run for the SEO agent.

Verifies that this host can reach every external dependency the agent
relies on at runtime, in a single command:

  1. GSC: list properties + sum impressions/clicks over the last 7 days.
  2. GA4: list properties + sample engagement for a few sample paths.
  3. D1 (optional): connect, list seo_* tables, count rows.
  4. Anthropic: ping (optional — only if ANTHROPIC_API_KEY is set).

Exits 0 if everything works; non-zero if any required check fails.
Designed to be the FIRST thing a new vibe-coding project runs after
provisioning, and the gate before enabling the agent's systemd timer.

Usage:
  python -m seo_agent.health_check
  python -m seo_agent.health_check --skip-d1   # for first-time setup before CF token is in place
  python -m seo_agent.health_check --json      # machine-readable for CI
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta
from typing import Any


def _check_gsc(days: int = 7) -> dict:
    from . import gsc_client

    started = time.time()
    sites = gsc_client.list_sites()
    end = date.today()
    start = end - timedelta(days=days)
    site_url = os.environ.get("GSC_SITE_URL", "").strip()
    if not site_url:
        return {
            "ok": False,
            "reason": "GSC_SITE_URL not set in env (add to .env)",
            "visible_sites": len(sites),
            "elapsed_ms": int((time.time() - started) * 1000),
        }
    if site_url not in sites and not any(s.endswith(site_url.replace("https://", "").rstrip("/")) for s in sites):
        return {
            "ok": False,
            "reason": f"site {site_url!r} not in visible sites {sites}",
            "elapsed_ms": int((time.time() - started) * 1000),
        }
    rows = list(gsc_client.fetch_page_daily(site_url, start.isoformat(), end.isoformat()))
    impressions = sum(int(r.get("impressions", 0)) for r in rows)
    clicks = sum(int(r.get("clicks", 0)) for r in rows)
    return {
        "ok": True,
        "site_url": site_url,
        "visible_sites": len(sites),
        "rows": len(rows),
        "impressions": impressions,
        "clicks": clicks,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _check_ga4(days: int = 7) -> dict:
    from . import ga4_client

    started = time.time()
    accounts = ga4_client.list_account_summaries()
    total_props = sum(len(a["properties"]) for a in accounts)
    # Sample paths for the health check. Defaults to ['/'] (homepage,
    # always exists). Override with GA4_HEALTHCHECK_PATHS env var
    # (comma-separated list) to probe paths specific to your site.
    sample_paths_env = os.environ.get("GA4_HEALTHCHECK_PATHS", "/").strip()
    sample_paths = [p.strip() for p in sample_paths_env.split(",") if p.strip()]
    engagement = ga4_client.fetch_page_engagement(sample_paths, days=days)
    return {
        "ok": True,
        "accounts": len(accounts),
        "total_properties_visible": total_props,
        "default_property": ga4_client._default_property(),
        "sample_paths_probed": len(sample_paths),
        "sample_paths_with_traffic": sum(1 for p in sample_paths if p in engagement),
        "sample_engagement": engagement,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _check_d1() -> dict:
    from . import d1_client, config as cfg

    started = time.time()
    if not cfg.CF_API_TOKEN or not cfg.CF_D1_DATABASE_ID or not cfg.CF_ACCOUNT_ID:
        return {
            "ok": False,
            "reason": "CLOUDFLARE_API_TOKEN / CLOUDFLARE_D1_API_TOKEN / DATABASE_ID / ACCOUNT_ID not in env",
            "elapsed_ms": int((time.time() - started) * 1000),
        }
    tables = d1_client.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'seo_%' ORDER BY name"
    )
    counts = {}
    for t in (r["name"] for r in tables):
        rows = d1_client.query(f"SELECT COUNT(*) AS n FROM {t}")
        counts[t] = int(rows[0]["n"])
    return {
        "ok": True,
        "tables": list(counts.keys()),
        "row_counts": counts,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _check_anthropic() -> dict:
    started = time.time()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {
            "ok": None,
            "reason": "ANTHROPIC_API_KEY not set (skipped)",
            "elapsed_ms": 0,
        }
    # Light ping: just verify the env var looks like a real key. We
    # don't burn an actual API call here.
    key = os.environ["ANTHROPIC_API_KEY"]
    looks_ok = key.startswith(("sk-ant-", "sk_ant_")) and len(key) > 30
    return {
        "ok": looks_ok,
        "key_format_valid": looks_ok,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def run(skip_d1: bool = False, days: int = 7) -> dict:
    results: dict[str, Any] = {}
    failures: list[str] = []

    try:
        results["gsc"] = _check_gsc(days=days)
        if not results["gsc"]["ok"]:
            failures.append(f"gsc: {results['gsc'].get('reason')}")
    except Exception as e:
        results["gsc"] = {"ok": False, "reason": f"exception: {e}"}
        failures.append(f"gsc: {e}")

    try:
        results["ga4"] = _check_ga4(days=days)
        if not results["ga4"]["ok"]:
            failures.append(f"ga4: {results['ga4'].get('reason')}")
    except Exception as e:
        results["ga4"] = {"ok": False, "reason": f"exception: {e}"}
        failures.append(f"ga4: {e}")

    if skip_d1:
        results["d1"] = {"ok": None, "reason": "skipped"}
    else:
        try:
            results["d1"] = _check_d1()
            if not results["d1"]["ok"]:
                failures.append(f"d1: {results['d1'].get('reason')}")
        except Exception as e:
            results["d1"] = {"ok": False, "reason": f"exception: {e}"}
            failures.append(f"d1: {e}")

    results["anthropic"] = _check_anthropic()  # advisory, never fails the run

    results["overall"] = "PASS" if not failures else "FAIL"
    results["failures"] = failures
    return results


def _print_human(r: dict) -> None:
    def status(check: dict) -> str:
        if check.get("ok") is True:
            return "OK"
        if check.get("ok") is False:
            return "FAIL"
        return "SKIP"

    g = r["gsc"]
    print(f"GSC    {status(g):4s}  ", end="")
    if g.get("ok"):
        print(
            f"{g['visible_sites']} properties visible, "
            f"{g['rows']} rows / {g['impressions']} impressions / {g['clicks']} clicks "
            f"for {g['site_url']} ({g['elapsed_ms']}ms)"
        )
    else:
        print(g.get("reason", "?"))

    a = r["ga4"]
    print(f"GA4    {status(a):4s}  ", end="")
    if a.get("ok"):
        probed = a.get("sample_paths_probed", 0)
        with_traffic = a.get("sample_paths_with_traffic", 0)
        print(
            f"{a['accounts']} accounts / {a['total_properties_visible']} properties, "
            f"{with_traffic}/{probed} sample paths have traffic on "
            f"property {a['default_property']} ({a['elapsed_ms']}ms)"
        )
        for path, m in a.get("sample_engagement", {}).items():
            print(
                f"               {path:30s} views={m['screenPageViews']} "
                f"engagement={m['engagementRate']:.2%} dur={m['averageSessionDuration']:.1f}s"
            )
    else:
        print(a.get("reason", "?"))

    d = r["d1"]
    print(f"D1     {status(d):4s}  ", end="")
    if d.get("ok"):
        for t, n in d.get("row_counts", {}).items():
            print(f"{t}={n}  ", end="")
        print(f"({d['elapsed_ms']}ms)")
    else:
        print(d.get("reason", "?"))

    an = r["anthropic"]
    print(f"ANTH.  {status(an):4s}  ", end="")
    print(an.get("reason", "key looks valid"))

    print()
    print(f"OVERALL: {r['overall']}")
    if r["failures"]:
        for f in r["failures"]:
            print(f"  • {f}")


def main() -> int:
    ap = argparse.ArgumentParser(description="SEO agent data-access health check")
    ap.add_argument("--skip-d1", action="store_true",
                    help="Skip D1 connectivity check (useful pre-CF-token setup).")
    ap.add_argument("--days", type=int, default=7, help="Days of GSC/GA4 data to sample.")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of human output.")
    args = ap.parse_args()

    results = run(skip_d1=args.skip_d1, days=args.days)
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        _print_human(results)
    return 0 if results["overall"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

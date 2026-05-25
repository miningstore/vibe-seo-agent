"""Cloudflare D1 HTTP API client.

The optimizer runs on a VPS, not in a Cloudflare Worker, so it cannot
use the in-runtime D1 binding the rest of the site uses. Instead it
talks to D1 via the public HTTP API:

    POST https://api.cloudflare.com/client/v4/accounts/{acct}/d1/database/{db}/query

with a `params` array for parameterized statements. We never interpolate
into SQL strings — same defense as the rest of the codebase.

The API token needs the `D1:Edit` permission scoped to the
average-rent-db database.
"""
from __future__ import annotations

import time
from typing import Any

import requests

from . import config as cfg


class D1Error(RuntimeError):
    pass


def _endpoint() -> str:
    if not cfg.CF_ACCOUNT_ID or not cfg.CF_D1_DATABASE_ID:
        raise D1Error("CF_ACCOUNT_ID / CF_D1_DATABASE_ID not set")
    return (
        f"https://api.cloudflare.com/client/v4/accounts/{cfg.CF_ACCOUNT_ID}"
        f"/d1/database/{cfg.CF_D1_DATABASE_ID}/query"
    )


def _headers() -> dict[str, str]:
    if not cfg.CF_API_TOKEN:
        raise D1Error("CF_API_TOKEN not set")
    return {
        "Authorization": f"Bearer {cfg.CF_API_TOKEN}",
        "Content-Type": "application/json",
    }


def query(sql: str, params: list[Any] | None = None) -> list[dict]:
    """Run a parameterized SELECT or DML. Returns rows for SELECTs,
    empty list for INSERT/UPDATE/DELETE.
    """
    body = {"sql": sql, "params": params or []}
    for attempt in range(3):
        resp = requests.post(_endpoint(), json=body, headers=_headers(), timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if not data.get("success"):
                errs = data.get("errors", [])
                raise D1Error(f"D1 query failed: {errs}")
            results = data.get("result", [])
            if not results:
                return []
            rows = results[0].get("results", [])
            return rows or []
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(2 ** attempt)
            continue
        raise D1Error(f"D1 HTTP {resp.status_code}: {resp.text[:500]}")
    raise D1Error("D1 query exhausted retries")


def insert_variant(
    slot: str,
    page_match: str,
    treatment_json: str,
    hypothesis: str,
    created_by: str = "seo_agent",
) -> int:
    """Insert a new variant row, return its rowid."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    rows = query(
        """INSERT INTO seo_variants
           (slot, page_match, treatment, status, created_at, created_by, hypothesis, alpha, beta)
           VALUES (?1, ?2, ?3, 'active', ?4, ?5, ?6, 1.0, 1.0)
           RETURNING id""",
        [slot, page_match, treatment_json, now, created_by, hypothesis],
    )
    if not rows:
        raise D1Error("INSERT ... RETURNING produced no row")
    return int(rows[0]["id"])


def count_active_variants(slot: str, page_match_json: str) -> int:
    rows = query(
        """SELECT COUNT(*) AS n FROM seo_variants
           WHERE slot = ?1 AND page_match = ?2 AND status = 'active'""",
        [slot, page_match_json],
    )
    return int(rows[0]["n"]) if rows else 0

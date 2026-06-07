"""Weekly SEO digest emailer.

Marketing-team-friendly dashboard: what's live, what changed since last
week, what to watch. Mirrors the layout of the autoresearch/seo_research
digests so recipients get a consistent visual language across the
agents we run.

Send via ForwardEmail.net REST API (same provider as autoresearch).
Config via env vars in .env:

    SEO_DIGEST_TO       = "marketing@miningstore.com"  (required)
    SEO_DIGEST_FROM     = "noreply@miningstore.com"    (required)
    SEO_DIGEST_SUBJECT_PREFIX = "[MiningStore SEO Agent]"  (optional)
    FORWARD_EMAIL_API_KEY = "..."  (required — same key autoresearch uses)
    GSC_SITE_URL        = used to label the digest

The agent's site_config.SLOTS drives the per-slot summary. We don't
hardcode any miningstore-specific knowledge — the same mailer drives
average-rent's digest, miningstore's digest, and any future project.

Invocation:
    python -m seo_agent.mailer              # sends if not already sent this week
    python -m seo_agent.mailer --force      # ignore sentinel, send anyway
    python -m seo_agent.mailer --dry-run    # print to stdout, don't send
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

from . import config as cfg

log = logging.getLogger("seo_agent.mailer")

FORWARD_EMAIL_API = "https://api.forwardemail.net/v1/emails"
DIGEST_SENTINEL = cfg.METRICS_DIR / "last_digest_iso_week.txt"


def _to() -> str:
    addr = os.environ.get("SEO_DIGEST_TO", "").strip()
    if not addr:
        raise RuntimeError("SEO_DIGEST_TO not set in .env")
    return addr


def _from() -> str:
    addr = os.environ.get("SEO_DIGEST_FROM", "").strip()
    if not addr:
        raise RuntimeError("SEO_DIGEST_FROM not set in .env")
    return addr


def _subject_prefix() -> str:
    return os.environ.get("SEO_DIGEST_SUBJECT_PREFIX", "[SEO Agent]")


def _site_label() -> str:
    """Friendly site name for the email header — strip scheme/trailing slash from GSC_SITE_URL."""
    site = os.environ.get("GSC_SITE_URL", "").strip()
    if site.startswith("sc-domain:"):
        return site.split(":", 1)[1]
    return site.replace("https://", "").replace("http://", "").rstrip("/") or "your site"


def _iso_week() -> str:
    y, w, _ = date.today().isocalendar()
    return f"{y}-W{w:02d}"


def _already_sent_this_week() -> bool:
    if not DIGEST_SENTINEL.exists():
        return False
    return DIGEST_SENTINEL.read_text().strip() == _iso_week()


def _mark_sent() -> None:
    DIGEST_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
    DIGEST_SENTINEL.write_text(_iso_week())


# === Data gathering ==========================================================


def _gather_metrics() -> dict:
    """Pull everything we need for the digest from D1 + GSC."""
    from . import d1_client

    metrics: dict = {"site": _site_label()}

    # --- This week vs last week GSC totals ---
    today = date.today()
    this_week_start = (today - timedelta(days=7)).isoformat()
    this_week_end = today.isoformat()
    last_week_start = (today - timedelta(days=14)).isoformat()
    last_week_end = (today - timedelta(days=7)).isoformat()

    def _gsc_total(start: str, end: str) -> dict:
        rows = d1_client.query(
            """SELECT COALESCE(SUM(impressions), 0) AS imp,
                      COALESCE(SUM(clicks), 0) AS clk,
                      COALESCE(AVG(position), 0) AS pos
               FROM seo_gsc_daily
               WHERE date >= ?1 AND date < ?2""",
            [start, end],
        )
        if not rows:
            return {"imp": 0, "clk": 0, "pos": 0.0}
        r = rows[0]
        return {"imp": int(r["imp"] or 0), "clk": int(r["clk"] or 0), "pos": float(r["pos"] or 0)}

    metrics["this_week"] = _gsc_total(this_week_start, this_week_end)
    metrics["last_week"] = _gsc_total(last_week_start, last_week_end)

    # --- Per-slot active variants ---
    slot_rows = d1_client.query(
        """SELECT slot, status, COUNT(*) AS n
           FROM seo_variants
           GROUP BY slot, status
           ORDER BY slot, status"""
    )
    slots: dict[str, dict] = {}
    for r in slot_rows:
        slot = r["slot"]
        slots.setdefault(slot, {"active": 0, "killed": 0, "paused": 0, "champion": 0})
        slots[slot][r["status"]] = int(r["n"])
    metrics["slots"] = slots

    # --- Variants killed / promoted in the last 7 days ---
    # (we approximate via created_at since we don't track status change time —
    # add a status_changed_at column in a future migration for exactness)
    recently_killed = d1_client.query(
        """SELECT id, slot, treatment, hypothesis
           FROM seo_variants WHERE status='killed'
           ORDER BY id DESC LIMIT 10"""
    )
    promoted = d1_client.query(
        """SELECT id, slot, treatment, hypothesis
           FROM seo_variants WHERE status='champion'
           ORDER BY id DESC LIMIT 10"""
    )
    metrics["killed"] = [_simplify_variant(r) for r in (recently_killed or [])]
    metrics["promoted"] = [_simplify_variant(r) for r in (promoted or [])]

    # --- Active variants currently sampling ---
    # Conversion count per variant: we look up each slot's goal_event
    # from the loaded config (cfg.SLOTS) and count seo_outcomes rows
    # matching that event. Variants whose slot has no goal_event get
    # conversions=0 (treated as engagement-only).
    slot_goals: dict[str, str] = {}
    for s in cfg.SLOTS:
        ge = getattr(s, "goal_event", "")
        if ge:
            slot_goals[s.name] = ge

    active = d1_client.query(
        """SELECT v.id, v.slot, v.page_match, v.treatment, v.hypothesis,
                  v.alpha, v.beta,
                  (SELECT COUNT(*) FROM seo_assignments a WHERE a.variant_id = v.id) AS sessions
           FROM seo_variants v WHERE v.status='active'
           ORDER BY v.slot, v.id"""
    )
    # 7-day window matches the headline Conversions tile and the
    # existing GSC impressions/clicks tiles, so users reading the
    # digest don't see one column showing "this week" while another
    # silently aggregates all-time.
    simplified_active = []
    for r in (active or []):
        s = _simplify_variant(r)
        ge = slot_goals.get(s["slot"], "")
        # Sessions in last 7 days (same window as conversions below).
        sess_rows = d1_client.query(
            """SELECT COUNT(*) AS n FROM seo_assignments
               WHERE variant_id = ?1
                 AND datetime(assigned_at) >= datetime('now', '-7 days')""",
            [s["id"]],
        )
        s["sessions_7d"] = int(sess_rows[0]["n"]) if sess_rows else 0
        if ge:
            crows = d1_client.query(
                """SELECT COUNT(*) AS n FROM seo_outcomes
                   WHERE variant_id = ?1
                     AND event = ?2
                     AND datetime(recorded_at) >= datetime('now', '-7 days')""",
                [s["id"], ge],
            )
            s["conversions"] = int(crows[0]["n"]) if crows else 0
            s["goal_event"] = ge
        else:
            s["conversions"] = 0
            s["goal_event"] = ""
        simplified_active.append(s)
    metrics["active"] = simplified_active

    # --- Total conversions (7d) across all slots with a goal_event ---
    total_conv_this = 0
    total_conv_last = 0
    if slot_goals:
        goal_events_in = "(" + ",".join(["?"] * len(slot_goals)) + ")"
        ge_values = list(set(slot_goals.values()))
        if ge_values:
            placeholders = ",".join(["?"] * len(ge_values))
            crows_this = d1_client.query(
                f"""SELECT COUNT(*) AS n FROM seo_outcomes
                    WHERE event IN ({placeholders})
                      AND datetime(recorded_at) >= datetime('now', '-7 days')""",
                ge_values,
            )
            crows_last = d1_client.query(
                f"""SELECT COUNT(*) AS n FROM seo_outcomes
                    WHERE event IN ({placeholders})
                      AND datetime(recorded_at) >= datetime('now', '-14 days')
                      AND datetime(recorded_at) <  datetime('now', '-7 days')""",
                ge_values,
            )
            total_conv_this = int(crows_this[0]["n"]) if crows_this else 0
            total_conv_last = int(crows_last[0]["n"]) if crows_last else 0
    metrics["conversions"] = {"this_week": total_conv_this, "last_week": total_conv_last}
    metrics["has_goal_slots"] = bool(slot_goals)

    # --- Striking-distance opportunities (positions 4-20 with high impressions) ---
    opps = d1_client.query(
        """SELECT page_path,
                  SUM(impressions) AS imp,
                  SUM(clicks) AS clk,
                  AVG(position) AS pos
           FROM seo_gsc_daily
           WHERE date >= ?1
             AND position >= 4 AND position <= 20
           GROUP BY page_path
           HAVING SUM(impressions) > 50
           ORDER BY SUM(impressions) DESC
           LIMIT 5""",
        [this_week_start],
    )
    metrics["opportunities"] = opps or []

    return metrics


def _simplify_variant(r: dict) -> dict:
    out = dict(r)
    try:
        out["treatment_text"] = json.loads(r["treatment"]).get("text", "")
    except Exception:
        out["treatment_text"] = ""
    try:
        out["page_match_path"] = json.loads(r.get("page_match", "{}") or "{}").get("path", "*")
    except Exception:
        out["page_match_path"] = "*"
    return out


# === HTML rendering ==========================================================


def _pct_change(this_w: int, last_w: int, plain: bool = False) -> str:
    """Format a week-over-week percent change.

    `plain=True` returns a value safe for a plaintext context (the email
    Subject header), which must NOT contain HTML entities — otherwise an
    inbox shows the literal text "+&infin;" instead of a rendered
    infinity glyph. The HTML body keeps using the entity form.
    """
    if last_w == 0:
        if this_w > 0:
            return "+∞%" if plain else "+&infin;"
        return "+0%"
    pct = (this_w - last_w) / last_w * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.0f}%"


def _delta_color(this_w: int, last_w: int) -> str:
    if this_w >= last_w:
        return "#16a34a"  # green
    return "#dc2626"      # red


def build_digest() -> tuple[str, str]:
    """Build subject + HTML body for the weekly digest."""
    m = _gather_metrics()
    site = m["site"]
    tw = m["this_week"]
    lw = m["last_week"]

    subject = (
        f"{_subject_prefix()} {site} — "
        f"{tw['imp']:,} impressions / {tw['clk']:,} clicks "
        f"({_pct_change(tw['imp'], lw['imp'], plain=True)} imp wow)"
    )

    # Slot summary rows
    slot_rows_html = ""
    if not m["slots"]:
        slot_rows_html = (
            '<tr><td colspan="5" style="padding:12px;color:#999;text-align:center;font-size:12px">'
            "No variants in any slot yet. The agent will populate slots over the next few ticks."
            "</td></tr>"
        )
    else:
        for slot_name, counts in sorted(m["slots"].items()):
            status_label = (
                f'<span style="color:#16a34a;font-weight:600">{counts["active"]} active</span>'
                if counts["active"] > 0
                else '<span style="color:#94a3b8">idle</span>'
            )
            slot_rows_html += (
                "<tr>"
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;font-weight:600;font-size:12px;vertical-align:top">{escape(slot_name)}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;vertical-align:top">{status_label}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;text-align:right;vertical-align:top;color:#dc2626">{counts["killed"]}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;text-align:right;vertical-align:top;color:#0f172a;font-weight:600">{counts["champion"]}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;text-align:right;vertical-align:top">{counts["paused"]}</td>'
                "</tr>"
            )

    # Active variants table — now with Conv / Conv% columns
    variant_rows_html = ""
    if not m["active"]:
        variant_rows_html = (
            '<tr><td colspan="4" style="padding:12px;color:#999;text-align:center;font-size:12px">'
            "No active variants yet."
            "</td></tr>"
        )
    else:
        for v in m["active"][:15]:
            # All-time sessions is informational; 7d is the bandit-
            # learning window and what the conversion rate is computed
            # against (matches the headline tile).
            sessions_total = int(v.get("sessions", 0) or 0)
            sessions_7d = int(v.get("sessions_7d", 0) or 0)
            convs = int(v.get("conversions", 0) or 0)
            cr = (convs / sessions_7d * 100) if sessions_7d else 0.0
            goal_label = v.get("goal_event") or "—"
            text = escape((v.get("treatment_text") or "")[:80])
            slot_short = escape(v["slot"])
            path = escape(v.get("page_match_path", "*"))
            hyp = escape((v.get("hypothesis") or "")[:90])
            sess_cell = (
                f'<strong>{sessions_7d}</strong>'
                f'<br><span style="color:#94a3b8;font-size:10px">{sessions_total} all-time</span>'
            )
            conv_cell = (
                f'<strong>{convs}</strong>'
                f'<br><span style="color:#94a3b8;font-size:10px">{cr:.2f}% · {escape(goal_label)}</span>'
                if v.get("goal_event")
                else '<span style="color:#cbd5e1">—</span>'
            )
            variant_rows_html += (
                "<tr>"
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:11px;font-weight:600;vertical-align:top">{slot_short}<br><span style="color:#94a3b8;font-weight:400">{path}</span></td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;vertical-align:top;font-family:monospace">{text}<br><span style="color:#94a3b8;font-family:Arial;font-size:11px">{hyp}</span></td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;text-align:right;vertical-align:top">{sess_cell}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;text-align:right;vertical-align:top">{conv_cell}</td>'
                "</tr>"
            )

    # Opportunities
    opp_rows_html = ""
    if not m["opportunities"]:
        opp_rows_html = (
            '<tr><td colspan="4" style="padding:12px;color:#999;text-align:center;font-size:12px">'
            "Not enough GSC data yet (first weekly poll runs after deploy)."
            "</td></tr>"
        )
    else:
        for o in m["opportunities"]:
            path = escape(o["page_path"])
            opp_rows_html += (
                "<tr>"
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:11px;font-family:monospace">{path}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;text-align:right">{int(o["imp"] or 0):,}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;text-align:right">{int(o["clk"] or 0)}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;text-align:right">{float(o["pos"] or 0):.1f}</td>'
                "</tr>"
            )

    # Promoted / killed callouts
    promoted_html = ""
    if m["promoted"]:
        promoted_html = '<div style="margin-top:12px;padding:12px;background:#f0fdf4;border-left:3px solid #16a34a;font-size:13px"><strong>&#9989; Promoted winners</strong> (now baked into the site):<ul style="margin:6px 0 0 0;padding-left:20px">'
        for v in m["promoted"][:5]:
            promoted_html += f'<li><code>{escape(v["slot"])}</code>: {escape((v.get("treatment_text") or "")[:80])}</li>'
        promoted_html += "</ul></div>"

    killed_html = ""
    if m["killed"]:
        killed_html = '<div style="margin-top:8px;padding:12px;background:#fef2f2;border-left:3px solid #dc2626;font-size:13px"><strong>&#10060; Killed (lost decisively)</strong>:<ul style="margin:6px 0 0 0;padding-left:20px">'
        for v in m["killed"][:5]:
            killed_html += f'<li><code>{escape(v["slot"])}</code>: {escape((v.get("treatment_text") or "")[:80])}</li>'
        killed_html += "</ul></div>"

    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:20px;background:#f8f8f8;font-family:Arial,Helvetica,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr><td align="center">
  <table width="720" cellpadding="0" cellspacing="0" border="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08)">

    <tr>
      <td style="background:#0f172a;padding:20px 24px">
        <div style="color:#ffffff;font-size:18px;font-weight:700">SEO Agent Weekly Digest</div>
        <div style="color:#94a3b8;font-size:13px;margin-top:4px">{escape(site)} &middot; {date.today().strftime('%A, %B %d, %Y')}</div>
      </td>
    </tr>

    <!-- Headline stats -->
    <tr>
      <td style="padding:0;border-bottom:1px solid #eee">
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr>
            <td width="25%" style="padding:20px 0 20px 24px;vertical-align:top">
              <div style="font-size:24px;font-weight:700;color:#0f172a;line-height:1">{tw['imp']:,}</div>
              <div style="font-size:11px;color:#94a3b8;margin-top:4px">Impressions (7d)</div>
              <div style="font-size:12px;color:{_delta_color(tw['imp'], lw['imp'])};margin-top:2px">{_pct_change(tw['imp'], lw['imp'])} vs prior 7d</div>
            </td>
            <td width="25%" style="padding:20px 0;vertical-align:top">
              <div style="font-size:24px;font-weight:700;color:#0f172a;line-height:1">{tw['clk']:,}</div>
              <div style="font-size:11px;color:#94a3b8;margin-top:4px">Clicks (7d)</div>
              <div style="font-size:12px;color:{_delta_color(tw['clk'], lw['clk'])};margin-top:2px">{_pct_change(tw['clk'], lw['clk'])} vs prior 7d</div>
            </td>
            <td width="25%" style="padding:20px 0;vertical-align:top">
              <div style="font-size:24px;font-weight:700;color:#0f172a;line-height:1">{(tw['clk']/tw['imp']*100 if tw['imp'] else 0):.2f}%</div>
              <div style="font-size:11px;color:#94a3b8;margin-top:4px">CTR (7d)</div>
              <div style="font-size:12px;color:#94a3b8;margin-top:2px">avg position {tw['pos']:.1f}</div>
            </td>
            <td width="25%" style="padding:20px 24px 20px 0;vertical-align:top">
              <div style="font-size:24px;font-weight:700;color:#0f172a;line-height:1">{m['conversions']['this_week']:,}</div>
              <div style="font-size:11px;color:#94a3b8;margin-top:4px">Conversions (7d)</div>
              <div style="font-size:12px;color:{_delta_color(m['conversions']['this_week'], m['conversions']['last_week'])};margin-top:2px">{_pct_change(m['conversions']['this_week'], m['conversions']['last_week'])} vs prior 7d</div>
            </td>
          </tr>
        </table>
      </td>
    </tr>{f'<tr><td style="padding:12px 24px;background:#fffbeb;border-bottom:1px solid #fde68a;font-size:12px;color:#92400e"><strong>Heads up:</strong> No slots have a <code>goal_event</code> configured yet, so conversions are 0. Set <code>goal_event</code> on at least one slot in <code>site_config.py</code> to start optimizing for business outcomes.</td></tr>' if not m.get("has_goal_slots") else ''}

    <!-- Promoted / killed callouts (only render if something happened) -->
    {f'<tr><td style="padding:0 24px">{promoted_html}{killed_html}</td></tr>' if (promoted_html or killed_html) else ''}

    <!-- Per-slot status -->
    <tr>
      <td style="padding:20px 24px;border-bottom:1px solid #eee;border-top:1px solid #eee">
        <div style="font-size:13px;color:#475569;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:12px">Slots</div>
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr style="background:#f8fafc">
            <td style="padding:8px 12px;font-size:11px;color:#94a3b8;font-weight:600">Slot</td>
            <td style="padding:8px 12px;font-size:11px;color:#94a3b8;font-weight:600">Status</td>
            <td style="padding:8px 12px;font-size:11px;color:#94a3b8;font-weight:600;text-align:right">Killed</td>
            <td style="padding:8px 12px;font-size:11px;color:#94a3b8;font-weight:600;text-align:right">Champion</td>
            <td style="padding:8px 12px;font-size:11px;color:#94a3b8;font-weight:600;text-align:right">Paused</td>
          </tr>
          {slot_rows_html}
        </table>
      </td>
    </tr>

    <!-- Active variants -->
    <tr>
      <td style="padding:20px 24px;border-bottom:1px solid #eee">
        <div style="font-size:13px;color:#475569;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:12px">Active variants currently sampling</div>
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr style="background:#f8fafc">
            <td style="padding:8px 12px;font-size:11px;color:#94a3b8;font-weight:600">Slot / Path</td>
            <td style="padding:8px 12px;font-size:11px;color:#94a3b8;font-weight:600">Variant text + hypothesis</td>
            <td style="padding:8px 12px;font-size:11px;color:#94a3b8;font-weight:600;text-align:right">Sessions (7d)</td>
            <td style="padding:8px 12px;font-size:11px;color:#94a3b8;font-weight:600;text-align:right">Conv (7d) / Goal</td>
          </tr>
          {variant_rows_html}
        </table>
      </td>
    </tr>

    <!-- Opportunities -->
    <tr>
      <td style="padding:20px 24px;border-bottom:1px solid #eee">
        <div style="font-size:13px;color:#475569;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Top opportunities &mdash; striking-distance pages</div>
        <div style="font-size:11px;color:#94a3b8;margin-bottom:12px">Pages ranking in position 4&ndash;20 with high impressions. These are the ones where small copy changes can unlock real traffic.</div>
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr style="background:#f8fafc">
            <td style="padding:8px 12px;font-size:11px;color:#94a3b8;font-weight:600">Page</td>
            <td style="padding:8px 12px;font-size:11px;color:#94a3b8;font-weight:600;text-align:right">Imp</td>
            <td style="padding:8px 12px;font-size:11px;color:#94a3b8;font-weight:600;text-align:right">Clk</td>
            <td style="padding:8px 12px;font-size:11px;color:#94a3b8;font-weight:600;text-align:right">Pos</td>
          </tr>
          {opp_rows_html}
        </table>
      </td>
    </tr>

    <!-- Footer -->
    <tr>
      <td style="padding:16px 24px;background:#f8fafc;font-size:12px;color:#64748b">
        Cost this week: <strong>$0</strong> (Claude Pro plan, no per-token charges) &middot;
        <a href="https://miningstore.atlassian.net/wiki/spaces/MarketingT/pages/4328652804" style="color:#2563eb;text-decoration:none">Confluence docs</a> &middot;
        <a href="https://github.com/miningstore/vibe-seo-agent" style="color:#2563eb;text-decoration:none">Repo</a>
      </td>
    </tr>

  </table>
  </td></tr>
</table>
</body>
</html>"""

    return subject, html


# === Send ====================================================================


def send_digest(force: bool = False, dry_run: bool = False) -> bool:
    """Send the weekly digest. Returns True if sent (or dry-run printed)."""
    if not force and _already_sent_this_week():
        log.info("digest already sent this ISO week; skipping (use --force to override)")
        return False

    subject, html = build_digest()

    if dry_run:
        print(f"=== DRY RUN ===\nSubject: {subject}\n\n{html[:2000]}\n...[truncated for stdout]")
        return True

    api_key = os.environ.get("FORWARD_EMAIL_API_KEY", "").strip()
    if not api_key:
        log.error("FORWARD_EMAIL_API_KEY not set in .env — cannot send")
        return False

    payload = json.dumps({
        "from": _from(),
        "to": _to(),
        "subject": subject,
        "html": html,
    }).encode()
    credentials = base64.b64encode(f"{api_key}:".encode()).decode()
    req = urllib.request.Request(
        FORWARD_EMAIL_API,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        log.info("digest sent to %s — subject: %s", _to(), subject)
        _mark_sent()
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        log.error("digest send failed HTTP %d: %s", e.code, body)
        return False
    except Exception as e:
        log.error("digest send failed: %s", e)
        return False


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Weekly SEO agent digest emailer")
    ap.add_argument("--force", action="store_true", help="Send even if already sent this ISO week")
    ap.add_argument("--dry-run", action="store_true", help="Print to stdout, don't send")
    args = ap.parse_args()
    ok = send_digest(force=args.force, dry_run=args.dry_run)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""vibe-seo-agent loop health alert — emails when the bandit loop is broken
(dead Claude auth, crashed/hung process, or repeated generation failures),
with exact re-authentication steps.

Drop-in for any seo-agent-style loop: every knob is an env var / flag, and it
needs only systemd + the `claude` CLI (no D1 credentials, no repo checkout).

Why this exists: the loop drives variant generation by shelling out to
`claude -p`. When the VPS Claude login expires the loop keeps running but
every generation tick fails — and CRUCIALLY, when all slots are *saturated*
the loop doesn't even attempt generation, so a dead login is fully invisible
in the journal. A "0 variants in 24h" trigger is therefore wrong: 0 variants
is perfectly healthy when slots are saturated. This watchdog keys on
*failure*, not silence.

Three signals, read on the VPS:

  (1) Liveness:  `systemctl is-active <service>` + whether the journal shows
      ANY tick activity in the window (a wedged process logs nothing).
  (2) Journal failures: the loop journal scanned for generation/auth failure
      signatures ("tick failed", "claude exit", "Not logged in", expired-login
      markers, ...) and, for context, attempts + variants inserted.
  (3) Proactive auth probe: a single cheap `claude -p "Reply AUTH_OK"`. This
      catches a dead login even when saturation means the loop never tried
      to generate — the failure mode silent journals miss.

Decision:
  - service not active                 -> "loop may be down" alert.
  - auth probe fails / journal auth-fail-> "re-authenticate" alert (+ steps).
  - other journal failures             -> "generation failing" alert.
  - active but zero journal activity   -> "loop may be hung" alert.
  - otherwise (generated OR idle-saturated, no failures, auth OK) -> healthy.

Sends via ForwardEmail.net (FORWARD_EMAIL_API_KEY). If SEO_HEALTH_EMAIL is
unset it runs log-only (still useful as a systemd-status / exit-code check).

Usage:
  python scripts/seo_health_alert.py             # check + email if broken
  python scripts/seo_health_alert.py --hours 24  # detection window
  python scripts/seo_health_alert.py --dry-run   # print, never email
  python scripts/seo_health_alert.py --force-email     # always email (test)
  python scripts/seo_health_alert.py --no-auth-probe   # skip the claude call

Env overrides (all optional): SEO_HEALTH_SERVICE, SEO_HEALTH_HOURS,
SEO_HEALTH_EMAIL, SEO_HEALTH_SENDER, SEO_HEALTH_ENV_FILE, SEO_HEALTH_AUTH_PROBE
(0/1), CLAUDE_BIN.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("seo_health_alert")

# Public-repo defaults are neutral; override via env / flags for your deploy.
DEFAULT_RECIPIENT = os.environ.get("SEO_HEALTH_EMAIL", "")  # empty -> log-only
DEFAULT_SENDER = "SEO Agent Ops <noreply@example.com>"
FORWARD_EMAIL_API = "https://api.forwardemail.net/v1/emails"
DEFAULT_SERVICE = "seo-agent.service"
DEFAULT_ENV_FILE = "/home/ubuntu/seo-agent/.env"

# Generation/auth failure signatures in the loop journal. The first group is
# specifically auth (drives the "re-authenticate" path); the rest are generic
# tick failures.
AUTH_FAIL_MARKERS = (
    "Not logged in",
    "Please run /login",
    "Failed to authenticate",
    "Invalid bearer token",
    "Invalid authentication credentials",
    "OAuth token has expired",
)
TICK_FAIL_MARKERS = ("tick failed", "claude exit", "claude CLI failed")
# Healthy-activity / progress markers (any one proves the loop is ticking).
ACTIVITY_MARKERS = (
    "picked target", "generation:", "inserted variant", "tick done",
    "idle; sleeping", "slots saturated", "posterior",
)
INSERTED_MARKER = "inserted variant"
ATTEMPT_MARKER = "generation:"

SENTINEL = Path.home() / ".cache" / "seo_health_alert" / "last_sent_date.txt"


# ---------------------------------------------------------------------------
# Signal 1 + 2: journal (liveness, attempts, failures)
# ---------------------------------------------------------------------------

def _read_journal(service: str, hours: int) -> str:
    """Return the service journal for the window. Tries an unprivileged read
    first (works when the unit grants SupplementaryGroups=systemd-journal),
    then falls back to passwordless sudo."""
    base = ["journalctl", "-u", service, f"--since={hours} hours ago",
            "--no-pager", "-o", "cat"]
    for cmd in (base, ["sudo", "-n", *base]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.warning("%s failed: %s", cmd[0], exc)
            continue
        text = out.stdout
        if text.strip() and "not seeing messages from other users" not in text:
            return text
    return ""


def _scan_journal(text: str) -> dict[str, int]:
    auth = tick_fail = attempts = inserted = activity = 0
    for line in text.splitlines():
        if any(m in line for m in AUTH_FAIL_MARKERS):
            auth += 1
        if any(m in line for m in TICK_FAIL_MARKERS):
            tick_fail += 1
        if ATTEMPT_MARKER in line:
            attempts += 1
        if INSERTED_MARKER in line:
            inserted += 1
        if any(m in line for m in ACTIVITY_MARKERS):
            activity += 1
    return {
        "auth_failures": auth, "tick_failures": tick_fail,
        "attempts": attempts, "inserted": inserted, "activity": activity,
    }


def _service_active(service: str) -> bool:
    try:
        out = subprocess.run(["systemctl", "is-active", service],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() == "active"
    except (subprocess.TimeoutExpired, OSError):
        return False


# ---------------------------------------------------------------------------
# Signal 3: proactive auth probe (catches dead login even when saturated)
# ---------------------------------------------------------------------------

def _auth_probe(timeout: int = 60) -> tuple[bool, str]:
    """Run one cheap `claude -p` and check it answers. Returns (ok, detail).

    ok=True  -> CLI authenticated and responding.
    ok=False -> not logged in / login rejected / timeout.
    Flat-rate: a single short completion on your plan. Skipped (ok=True) when
    the CLI isn't found, so a probe-less host doesn't false-alarm — the
    journal signals still apply."""
    claude = os.environ.get("CLAUDE_BIN") or shutil.which("claude") \
        or str(Path.home() / ".local" / "bin" / "claude")
    if not (Path(claude).exists() or shutil.which(claude)):
        return True, "claude CLI not found; skipped auth probe"
    try:
        out = subprocess.run(
            [claude, "-p", "Reply with exactly: AUTH_OK", "--output-format", "text"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"auth probe timed out after {timeout}s"
    except OSError as exc:
        return True, f"auth probe could not run ({exc}); skipped"
    blob = f"{out.stdout}\n{out.stderr}"
    if "AUTH_OK" in out.stdout:
        return True, "auth probe OK"
    for m in AUTH_FAIL_MARKERS:
        if m in blob:
            return False, f"auth probe rejected: {m!r}"
    if out.returncode != 0:
        return False, f"auth probe exit {out.returncode}: {(out.stderr or '').strip()[:200]}"
    return False, f"auth probe gave no AUTH_OK: {(out.stdout or '').strip()[:120]}"


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _reauth_steps(service: str, env_file: str) -> list[str]:
    return [
        "Re-authenticate the VPS Claude CLI (keeps the loop flat-rate on your plan).",
        "",
        "Durable (recommended — a long-lived token survives reboots/expiry):",
        "  1. On your Mac:   claude setup-token",
        "     Authorize in the browser; copy the printed token (sk-ant-oat01-...).",
        f"  2. On the VPS, put it in {env_file}:",
        f"       sed -i '/^CLAUDE_CODE_OAUTH_TOKEN=/d' {env_file}",
        f"       echo 'CLAUDE_CODE_OAUTH_TOKEN=<paste-token>' >> {env_file}",
        f"     and make sure the unit loads it: EnvironmentFile={env_file}",
        "",
        "Quick alternative (interactive login, can re-expire):",
        "       claude   # then /login to complete OAuth, writes ~/.claude/.credentials.json",
        "",
        f"Then restart + confirm:  sudo systemctl restart {service}",
        "                          claude -p 'Reply AUTH_OK'",
    ]


def _build_email(stats: dict, *, hours: int, active: bool, auth_ok: bool,
                 auth_detail: str, service: str, env_file: str
                 ) -> tuple[str | None, list[str]]:
    """Return (subject_or_None, body_lines). subject=None means healthy."""
    auth_j = stats["auth_failures"]
    tick_f = stats["tick_failures"]
    header = [
        "SEO agent loop health check",
        "",
        f"Service:            {service}",
        f"Window:             last {hours}h",
        f"Service active:     {'yes' if active else 'NO'}",
        f"Auth probe:         {'OK' if auth_ok else 'FAILED'}  ({auth_detail})",
        f"Variants inserted:  {stats['inserted']}",
        f"Generation ticks:   {stats['attempts']}",
        f"Tick failures:      {tick_f}",
        f"Journal auth fails: {auth_j}",
        f"Activity lines:     {stats['activity']}",
        "",
    ]

    if not active:
        return (f"⚠️ SEO agent: {service} is NOT active — loop down",
                header + [
                    "The loop service is not active, so no variants are being generated "
                    "or scored. Restart and inspect:",
                    "",
                    f"  ssh VPS -> sudo systemctl status {service}",
                    f"  sudo journalctl -u {service} -n 100 --no-pager",
                ])

    if not auth_ok or auth_j > 0:
        return (f"\U0001F6A8 SEO agent: Claude auth is broken — re-authenticate",
                header + [
                    "The Claude CLI is not authenticating "
                    f"(probe: {auth_detail}; {auth_j} auth failures in the loop journal). "
                    "Variant generation cannot run. Note: if all slots are saturated the "
                    "loop won't even attempt generation, so this can stay silent for weeks "
                    "without this probe. Fix:",
                    "",
                ] + _reauth_steps(service, env_file))

    if tick_f > 0:
        return (f"⚠️ SEO agent: {tick_f} generation tick failures in {hours}h",
                header + [
                    f"The loop attempted generation but {tick_f} tick(s) failed in the last "
                    f"{hours}h (auth looks OK, so this is likely a code/validation/D1 error). "
                    "Inspect:",
                    "",
                    f"  sudo journalctl -u {service} --since '{hours} hours ago' --no-pager | grep -A20 'tick failed'",
                ])

    if stats["activity"] == 0:
        return (f"⚠️ SEO agent: no tick activity in {hours}h — loop may be hung",
                header + [
                    "The service is active and auth is OK, but the journal shows no tick "
                    f"activity at all in the last {hours}h. The loop process may be wedged. "
                    "Restart and inspect:",
                    "",
                    f"  sudo systemctl restart {service}",
                    f"  sudo journalctl -u {service} -n 100 --no-pager",
                ])

    return None, header  # healthy


def _send_email(recipient: str, sender: str, subject: str, body: str) -> bool:
    if not recipient:
        log.warning("SEO_HEALTH_EMAIL not set; not emailing. Would have sent: %s", subject)
        return False
    api_key = os.environ.get("FORWARD_EMAIL_API_KEY", "").strip()
    if not api_key:
        log.error("FORWARD_EMAIL_API_KEY not set; cannot send alert")
        return False
    html = (
        "<pre style=\"font-family: ui-monospace, monospace; font-size: 13px; "
        "line-height: 1.5;\">" + body.replace("&", "&amp;").replace("<", "&lt;")
        + "</pre>"
    )
    payload = json.dumps({
        "from": sender, "to": recipient,
        "subject": subject, "text": body, "html": html,
    }).encode()
    creds = base64.b64encode(f"{api_key}:".encode()).decode()
    req = urllib.request.Request(
        FORWARD_EMAIL_API, data=payload, method="POST",
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        log.info("alert sent to %s: %s", recipient, subject)
        return True
    except urllib.error.HTTPError as exc:
        log.error("alert send HTTP %s: %s", exc.code,
                  exc.read().decode("utf-8", "replace")[:300])
        return False
    except Exception as exc:
        log.error("alert send failed: %s", exc)
        return False


def _already_sent_today() -> bool:
    try:
        return SENTINEL.read_text().strip() == date.today().isoformat()
    except OSError:
        return False


def _mark_sent_today() -> None:
    try:
        SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        SENTINEL.write_text(date.today().isoformat())
    except OSError as exc:
        log.warning("could not write sentinel %s: %s", SENTINEL, exc)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=int,
                    default=int(os.environ.get("SEO_HEALTH_HOURS", "24")),
                    help="detection window in hours (default 24)")
    ap.add_argument("--service",
                    default=os.environ.get("SEO_HEALTH_SERVICE", DEFAULT_SERVICE))
    ap.add_argument("--recipient", default=DEFAULT_RECIPIENT)
    ap.add_argument("--sender",
                    default=os.environ.get("SEO_HEALTH_SENDER", DEFAULT_SENDER))
    ap.add_argument("--env-file",
                    default=os.environ.get("SEO_HEALTH_ENV_FILE", DEFAULT_ENV_FILE),
                    help="path shown in the re-auth steps for the OAuth token")
    ap.add_argument("--no-auth-probe", action="store_true",
                    default=os.environ.get("SEO_HEALTH_AUTH_PROBE", "1") == "0",
                    help="skip the proactive `claude -p` auth probe")
    ap.add_argument("--dry-run", action="store_true", help="print, never email")
    ap.add_argument("--force-email", action="store_true",
                    help="email even when healthy / already sent today (test)")
    args = ap.parse_args()

    journal = _read_journal(args.service, args.hours)
    stats = _scan_journal(journal)
    active = _service_active(args.service)
    if args.no_auth_probe:
        auth_ok, auth_detail = True, "skipped (--no-auth-probe)"
    else:
        auth_ok, auth_detail = _auth_probe()

    print(f"Service:          {args.service}  (active={active})")
    print(f"Window:           last {args.hours}h")
    print(f"Auth probe:       {'OK' if auth_ok else 'FAILED'} — {auth_detail}")
    print(f"Variants inserted:{stats['inserted']}")
    print(f"Generation ticks: {stats['attempts']}")
    print(f"Tick failures:    {stats['tick_failures']}")
    print(f"Journal auth fail:{stats['auth_failures']}")
    print(f"Activity lines:   {stats['activity']}")
    if not journal.strip():
        print("WARN: journal was empty/unreadable; liveness signal degraded")

    subject, body = _build_email(
        stats, hours=args.hours, active=active, auth_ok=auth_ok,
        auth_detail=auth_detail, service=args.service, env_file=args.env_file,
    )
    healthy = subject is None
    print(f"Healthy:          {'YES' if healthy else 'no — ' + subject}")

    if args.dry_run:
        if healthy and not args.force_email:
            print("\n[dry-run] would NOT send (healthy)")
        else:
            sub = subject or "✅ SEO agent: healthy (forced test email)"
            print(f"\n[dry-run] would send: {sub}\n\n--- email body ---\n" + "\n".join(body))
        return 0

    if healthy and not args.force_email:
        return 0

    if _already_sent_today() and not args.force_email:
        log.info("alert already sent today; skipping (use --force-email to override)")
        return 0

    sub = subject or "✅ SEO agent: healthy (forced test email)"
    if not args.force_email:
        _mark_sent_today()  # mark before send to avoid dup on slow/racing runs
    sent = _send_email(args.recipient, args.sender, sub, "\n".join(body))
    return 0 if sent else 2


if __name__ == "__main__":
    sys.exit(main())

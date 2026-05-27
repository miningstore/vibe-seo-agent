"""Per-project configuration for the SEO agent.

EDIT THIS FILE for your site. The other modules in seo_agent/ are
generic — they read whatever you define here. Three things you'll
likely change:

  1. SLOTS — what copy surfaces (title, meta, H1, FAQ, schema…) the
     agent is allowed to vary. Each slot is the atomic unit of
     experimentation.
  2. HOUSE_STYLE_BANNED — words/phrases the agent must NEVER emit in
     proposed variants. Vendor names you compete with, internal jargon,
     etc.
  3. ACCEPTED_BRANCH — where promoted winners get cherry-picked when a
     variant has decisively won.

Runtime knobs (cooldowns, caps, kill/promote thresholds) below the
slot list have safe defaults — tune later from telemetry.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parent.parent

# Branch where accepted (post-promotion) static-template changes land
# as PRs. Pick something that won't collide with other automated loops
# in the same repo.
ACCEPTED_BRANCH = os.environ.get("SEO_ACCEPTED_BRANCH", "seo-agent/accepted")

# Rate / spend controls. Conservative on purpose — SEO experimentation
# is breadth, not speed; we want lots of variants alive at once, not
# many proposals per hour.
MIN_COOLDOWN_S = 600          # 10 min between ticks during the day
NIGHT_COOLDOWN_S = 1800       # 30 min at night
IDLE_COOLDOWN_S = 3600        # 1 hour when nothing to do
NIGHT_START_HOUR = 2          # operator-local
NIGHT_END_HOUR = 6
DAILY_SPEND_CAP_USD = float(os.environ.get("SEO_DAILY_SPEND_CAP_USD", "15.0"))
MAX_VARIANTS_GENERATED_PER_DAY = int(os.environ.get("SEO_MAX_VARIANTS_PER_DAY", "60"))
# Bandit's active-arm count per (slot, page_match). Higher = wider
# exploration but slower convergence at fixed traffic. Override via
# SEO_MAX_ACTIVE_PER_SLOT env var when scaling up.
MAX_ACTIVE_VARIANTS_PER_SLOT = int(os.environ.get("SEO_MAX_ACTIVE_PER_SLOT", "8"))

# Eval rules — when do we kill or promote?
# KILL_MIN_IMPRESSIONS dropped from 2000 → 200 to match real-world
# traffic on the launch sites. At ~5-15 sessions/variant/day, 2000
# imps takes 4+ months per arm — by which time the data is stale.
# 200 is enough for Beta(α,β) to differentiate decisively (a variant
# with 0 reward over 200 imps has P(beats champion) < 1%) without
# killing arms prematurely.
KILL_MIN_IMPRESSIONS = int(os.environ.get("SEO_KILL_MIN_IMPRESSIONS", "200"))
KILL_BEATS_PROB = 0.05        # < 5% chance of beating champion → kill
PROMOTE_MIN_IMPRESSIONS = int(os.environ.get("SEO_PROMOTE_MIN_IMPRESSIONS", "1000"))
PROMOTE_MIN_DAYS = 14
PROMOTE_BEATS_PROB = 0.95     # > 95% chance of beating → promote

# Stale-variant cleanup. A variant generated 30+ days ago that has
# fewer than 100 impressions is effectively dead — bandit-starved or
# targeting a page with no traffic. Kill it so the active pool stays
# clean and MAX_ACTIVE_VARIANTS_PER_SLOT actually reflects live arms.
STALE_KILL_MIN_AGE_DAYS = int(os.environ.get("SEO_STALE_KILL_MIN_AGE_DAYS", "30"))
STALE_KILL_MAX_IMPRESSIONS = int(os.environ.get("SEO_STALE_KILL_MAX_IMPRESSIONS", "100"))


SlotKind = Literal["text", "faq", "schema"]


@dataclass
class Slot:
    name: str                              # e.g. 'home.title'
    pattern: str                           # e.g. 'home' — groups slots by page-pattern
    kind: SlotKind
    enabled: bool                          # gate variant proposals
    description: str
    min_len: int = 0
    max_len: int = 0
    banned_tokens: tuple[str, ...] = field(default_factory=tuple)
    # Whitelisted template placeholders the variant may contain. The
    # page template substitutes these at render time. Required for slots
    # whose `page_match` is empty `{}` (i.e. variant applies to all
    # pages under a pattern, so per-page values must be templated).
    # Each entry is the bare name without braces, e.g. ("city",) means
    # `{city}` is allowed.
    template_vars: tuple[str, ...] = field(default_factory=tuple)
    # Per-slot conversion goal. When set, the allocator weights this
    # event_name by goal_weight in the reward function (default 5x
    # over lead_click), and the variant generator prompt tells Claude
    # to optimize for this specific conversion. When empty, the slot
    # falls back to the generic engagement-only reward (view / scroll /
    # dwell / cta_click / lead_click). Common goal_event values:
    #   pro_checkout_complete   - paid subscription signup
    #   book_call               - Calendly / consultation booked
    #   quote_request           - contact form / quote requested
    #   newsletter_signup       - email list opt-in
    goal_event: str = ""
    goal_weight: float = 5.0


# === EDIT BELOW: tokens that may never appear in your variants ===
# Common bans worth keeping: em/en dashes (look LLM-y in copy);
# competitor brand names; phrases that imply data source mechanics
# (e.g. "scraped"); legal/regulatory red flags.
#
# OVERRIDE: if you create `seo_agent/site_config.py` with HOUSE_STYLE_BANNED
# and/or SLOTS module-level constants, those take precedence over the
# defaults below. This lets you customize per-project without diverging
# the upstream template. See docs/03-CUSTOMIZING.md.
HOUSE_STYLE_BANNED = (
    "—", "–",          # em dash, en dash
    # Add competitor names, internal jargon, banned phrases here:
    # "competitor_x", "competitor_y",
)


# === EDIT BELOW: the slots your agent may vary ===
# Recommended starting set for any site:
#  - <title>          (high-leverage, capped at 60 chars by Google)
#  - meta description (drives SERP CTR; 70-160 char window)
#  - H1               (above-the-fold UX + on-page SEO)
# Add per page-pattern (e.g. one set for the homepage, one for a
# category page, one for a detail page). Start with ENABLED=False on
# all slots and flip one to True at a time as you gain confidence.
SLOTS: list[Slot] = [
    Slot(
        name="home.title", pattern="home", kind="text",
        enabled=False,
        description="HTML <title> on the homepage.",
        min_len=30, max_len=60,
        banned_tokens=HOUSE_STYLE_BANNED,
    ),
    Slot(
        name="home.meta_description", pattern="home", kind="text",
        enabled=False,
        description="<meta name='description'> on the homepage.",
        min_len=70, max_len=160,
        banned_tokens=HOUSE_STYLE_BANNED,
    ),
    Slot(
        name="home.h1", pattern="home", kind="text",
        enabled=True,   # safe launch surface — below SERP fold, low schema risk
        description="H1 on the homepage.",
        min_len=15, max_len=80,
        banned_tokens=HOUSE_STYLE_BANNED,
    ),
    # Add more page-patterns here, e.g.:
    # Slot(name="category.title", pattern="category", kind="text", enabled=False,
    #      description="<title> on /category/{slug}/", min_len=30, max_len=60,
    #      banned_tokens=HOUSE_STYLE_BANNED),
]


# Per-project overlay. If `seo_agent/site_config.py` exists with module-
# level `SLOTS` and/or `HOUSE_STYLE_BANNED` and/or `PAGE_MATCH`, those
# take precedence. This lets you customize without diverging the
# upstream template — pull template updates without merging your slots.
try:
    from . import site_config as _site  # type: ignore
    if hasattr(_site, "SLOTS"):
        SLOTS = _site.SLOTS  # noqa: F811
    if hasattr(_site, "HOUSE_STYLE_BANNED"):
        HOUSE_STYLE_BANNED = _site.HOUSE_STYLE_BANNED  # noqa: F811
    PAGE_MATCH: dict[str, dict] = getattr(_site, "PAGE_MATCH", {})
except ImportError:
    PAGE_MATCH = {}


def enabled_slots() -> list[Slot]:
    return [s for s in SLOTS if s.enabled]


def get_slot(name: str) -> Slot | None:
    for s in SLOTS:
        if s.name == name:
            return s
    return None


def page_match_for(slot_name: str) -> dict:
    """Look up the per-slot page_match predicate from site_config.PAGE_MATCH.
    Defaults to {} (match every page under the slot's pattern)."""
    return PAGE_MATCH.get(slot_name, {})


# === Runtime env vars (all set in .env, loaded by systemd) ===

# Anthropic / Claude CLI
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", os.path.expanduser("~/.local/bin/claude"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
CLAUDE_TURN_CAP = int(os.environ.get("CLAUDE_TURN_CAP", "30"))
CLAUDE_TIMEOUT_S = int(os.environ.get("CLAUDE_TIMEOUT_S", "600"))

# Cloudflare D1 HTTP API. We accept either our short names (CF_*) or
# Cloudflare's canonical names (CLOUDFLARE_*) so we don't conflict with
# wrangler / other tools on the same host.
CF_ACCOUNT_ID = (
    os.environ.get("CF_ACCOUNT_ID")
    or os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
)
CF_D1_DATABASE_ID = (
    os.environ.get("CF_D1_DATABASE_ID")
    or os.environ.get("CLOUDFLARE_D1_DATABASE_ID", "")
)
CF_API_TOKEN = (
    os.environ.get("CF_API_TOKEN")
    or os.environ.get("CLOUDFLARE_D1_API_TOKEN")     # apartment-pricer convention
    or os.environ.get("CLOUDFLARE_API_TOKEN", "")    # CF / wrangler canonical
)

METRICS_DIR = REPO_ROOT / "seo_agent" / "metrics"
SCRATCH_DIR = REPO_ROOT / "seo_agent" / "scratch"
PROMPTS_DIR = REPO_ROOT / "seo_agent" / "prompts"
MCP_CONFIG = REPO_ROOT / "seo_agent" / ".mcp.json"

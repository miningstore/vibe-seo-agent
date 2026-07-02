"""Per-project config overlay.

How to use:

    cp seo_agent/site_config.example.py seo_agent/site_config.py
    $EDITOR seo_agent/site_config.py

The actual `site_config.py` is gitignored — your edits stay local to
your project clone and template updates from upstream
(github.com/miningstore/vibe-seo-agent) won't overwrite them.

What you can override:

  * SLOTS              — the experimentation surfaces for your site
  * HOUSE_STYLE_BANNED — tokens / phrases your variants must never use
  * PAGE_MATCH         — per-slot {"path": "..."} or other predicate

The template's `seo_agent/config.py` imports from this module and uses
whatever you define here in place of the defaults.
"""
# IMPORTANT: import Slot from the template — never vendor a copy of the
# dataclass here. A vendored copy silently drops every field the template
# adds later (one deployment broke with "unexpected keyword argument
# 'serp_visible'" exactly this way). The import is safe even though
# config.py imports this module at its bottom: Slot is defined well
# before that import executes.
from .config import Slot, SlotKind  # noqa: F401


# Tokens that may never appear in your variants. Common bans:
# - Em/en dashes (LLM tell)
# - Competitor names you don't want to rank for
# - Regulatory red flags ("guaranteed returns", "risk-free", etc.)
# - Internal jargon a customer wouldn't recognize
HOUSE_STYLE_BANNED = (
    "—", "–",
    # "competitor_brand",
)


# Slots. Each represents one copy surface on one page-pattern. Start
# with TITLES enabled (biggest CTR lever), then META DESCRIPTIONS, then H1s.
SLOTS: list[Slot] = [
    Slot(
        name="home.title",
        pattern="page",
        kind="text",
        enabled=False,
        description="HTML <title> on the homepage.",
        min_len=30, max_len=60,
        banned_tokens=HOUSE_STYLE_BANNED,
    ),
    Slot(
        name="home.meta_description",
        pattern="page",
        kind="text",
        enabled=False,
        description="<meta name='description'> on the homepage.",
        min_len=70, max_len=160,
        banned_tokens=HOUSE_STYLE_BANNED,
    ),
    Slot(
        name="home.h1",
        pattern="page",
        kind="text",
        enabled=True,           # safest launch surface (below SERP fold)
        description="H1 on the homepage.",
        min_len=15, max_len=80,
        banned_tokens=HOUSE_STYLE_BANNED,
    ),
]


# Per-slot page_match predicates. Empty {} means "match all pages
# under the slot's pattern". For static sites where each slot targets
# ONE specific URL, set the path here:
#
#   PAGE_MATCH = {
#       "home.title":            {"path": "/"},
#       "pricing.title":         {"path": "/pricing/"},
#       "blog_post.title":       {"path_prefix": "/blog/"},   # custom matcher
#   }
PAGE_MATCH: dict[str, dict] = {
    "home.title":            {"path": "/"},
    "home.meta_description": {"path": "/"},
    "home.h1":               {"path": "/"},
}


# Page-family path regexes for SERP-visible slots, keyed by Slot.pattern.
# REQUIRED for every pattern that has serp_visible=True slots: the
# sequential SERP evaluator (serp_evaluator.py) aggregates GSC clicks /
# impressions / position over the matching pages to score each champion
# tenure. Patterns without an entry are skipped with a warning.
PATTERN_PATH_REGEX: dict[str, str] = {
    "home": r"^/$",
    # "category": r"^/c/[^/]+/$",
    # "product":  r"^/p/[^/]+/$",
}

# vibe-seo-agent

A drop-in SEO experimentation agent for any site you own. Reads
Google Search Console + Google Analytics 4, runs thousands of parallel
on-page experiments via edge A/B, and a Claude-CLI loop generates new
variants and promotes winners.

The Google connection layer **bypasses Google's 2024 service-account
block** by using OAuth user flow under your owner identity — one
bootstrap, every property auto-accessible, no per-site permission
grants ever.

## What you get

```
your-vps
├── nightly: GSC poll → D1 (per-page rank, CTR, impressions)
├── continuous: claude-cli loop
│   └── propose variants → validate → write to D1 → bandit allocates
└── edge: Astro/Cloudflare middleware reads D1, assigns visitors to variants
```

Reusable for any site you own. Per-project setup is ~10 minutes.

## Quickstart (10 min)

```bash
# 1. Clone and install
git clone git@github.com:miningstore/vibe-seo-agent.git
cd vibe-seo-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r seo_agent/requirements.txt

# 2. In GCP Console (one-time per Google account):
#    - Enable: Search Console API, Analytics Data API, Analytics Admin API
#    - Credentials → OAuth client → Desktop app → download JSON
#    - Save as credentials/gsc-oauth-secrets.json
# Detailed steps: docs/01-GCP_SETUP.md

# 3. Bootstrap OAuth (opens browser once, caches refresh token)
python -m seo_agent.gsc_client --bootstrap

# 4. Discover your IDs
python -m seo_agent.gsc_client --list-sites       # copy your site_url
python -m seo_agent.ga4_client --list-properties  # copy your property ID

# 5. Configure your site in .env
cp .env.example .env
$EDITOR .env   # set GSC_SITE_URL and GA4_PROPERTY_ID

# 6. Health check (runs your dry run)
set -a; source .env; set +a
python -m seo_agent.health_check --skip-d1

# 7. Edit your slots and bans
$EDITOR seo_agent/config.py   # SLOTS = [...], HOUSE_STYLE_BANNED = (...)
```

That's it for local. Optional: deploy to a VPS — see `docs/02-VPS_DEPLOY.md`.

## How the parts fit

| File | Purpose |
|---|---|
| `seo_agent/gsc_client.py` | Search Console — OAuth user flow, runs your bootstrap |
| `seo_agent/ga4_client.py` | GA4 Admin + Data — same OAuth token, second API |
| `seo_agent/gsc_poller.py` | Nightly job: pulls per-page GSC data, writes to D1 |
| `seo_agent/health_check.py` | One-shot dry run: GSC + GA4 + D1 + Anthropic |
| `seo_agent/config.py` | YOUR site's slots, bans, branch — edit this |
| `seo_agent/allocator.py` | Thompson sampling for variant traffic allocation |
| `seo_agent/variant_generator.py` | Builds the Claude CLI prompt for proposing variants |
| `seo_agent/loop.py` | The agent — generate → validate → store → eval → kill/promote |
| `seo_agent/d1_client.py` | Cloudflare D1 HTTP API client |
| `examples/astro-cloudflare/` | Reference middleware + migration for Astro on CF Pages |
| `docs/01-GCP_SETUP.md` | Exact GCP Console click-paths |
| `docs/02-VPS_DEPLOY.md` | SSH, systemd, env file, timer |
| `docs/03-CUSTOMIZING.md` | Adapt for non-Cloudflare, non-Astro, etc. |

## The 2024 SA blockade — what this repo solves

Since 2024, Google has blocked service-account emails from being added
to GSC ("email not found") and GA4 ("doesn't match a Google Account")
via UI. The workarounds you'll see online (groups → also blocked, GA4
Admin API → works but per-property dance) all get tedious fast.

This repo skips the whole problem by authenticating as the **property
owner via OAuth Desktop client**. You sign in once, get a refresh
token, copy it to the VPS, and it auto-refreshes forever. Same token
covers GSC + GA4 for every property you own. Zero per-property
permission grants. Zero service accounts.

For projects that DO need SAs (e.g. you want to grant a contractor's
SA read-only access to GA4 without sharing your token), see
`seo_agent/grant_ga4_access.py` — it uses the Admin API path that still
accepts SAs.

## Status

v1 ships the Google connection layer + the agent loop scaffold +
a reference Astro/Cloudflare integration. Tested against
average-rent.com. miningstore.com is the next target.

## License

MIT. Use freely on any site you own.

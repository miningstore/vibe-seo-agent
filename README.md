# vibe-seo-agent

A drop-in SEO experimentation agent for any site you own. Reads Google
Search Console + Google Analytics 4, runs thousands of parallel
on-page experiments via edge A/B, and a Claude-CLI loop generates new
variants and promotes winners.

> **Proven on average-rent.com.** Live agent, real GSC data flowing,
> 4 active H1 variants on `apartments_listing.h1`, visitors sampled
> via Thompson allocation, Googlebot served the static fallback to
> avoid cloaking. miningstore.com is the next deploy.

The Google connection layer **bypasses Google's 2024 service-account
block** by using OAuth user flow under your owner identity — one
bootstrap, every property auto-accessible, no per-site permission
grants ever.

## Architecture in one picture

```
your-vps
├── ~/seo-agent/                          # git worktree pinned to origin/main
│   ├── seo_agent/                        # the Python package
│   ├── credentials/                       # OAuth secrets + token + CF env
│   └── seo_agent/.venv/                   # Python deps
│
├── systemd: seo-gsc-poller.timer         # nightly 03:00 — pulls GSC into D1
└── systemd: seo-agent-loop.service       # continuous — claude-cli proposes
                                            variants, validates, inserts to D1
your-site (Cloudflare Pages, Astro, etc.)
├── middleware.ts                         # _arx cookie + variant sampling
└── pages/*.astro                         # pickSlotText(slot, fallback, vars)
```

## What makes this work where most attempts fail

| Problem | Common (broken) approach | What this repo does |
|---|---|---|
| Google blocks new service accounts on GSC | Try harder (use groups, write API workarounds — also blocked) | OAuth user flow as the property owner — zero permission grants |
| `claude` CLI on a VPS — needs an API key? | `--bare` flag, `ANTHROPIC_API_KEY=...` | Use your Claude Pro/Max plan via `~/.claude/.credentials.json` — the loop invokes `claude -p` (no `--bare`!) and inherits OAuth |
| Untracked code on the VPS gets wiped by sibling git ops | Commit everything | Git **worktree** at `~/seo-agent` pinned to `origin/main` — survives `git reset --hard` on the main checkout |
| Bandit gives bots different H1s across crawls → cloaking flag | Just give bots a "stable" variant | Bots see the **literal fallback** until a champion is named (deterministic across deploys) |
| Per-page values in cross-city variants | Generate one variant per city (10× cost) | `template_vars=("city","unit_count",...)` whitelist; template substitutes at render |

## Quickstart (15 min, end to end)

```bash
# 1. Clone (or fork) this repo
git clone git@github.com:miningstore/vibe-seo-agent.git
cd vibe-seo-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r seo_agent/requirements.txt

# 2. GCP Console (one-time): enable APIs + create OAuth Desktop client.
#    Full click-paths in docs/01-GCP_SETUP.md. Save the downloaded
#    JSON as credentials/gsc-oauth-secrets.json.

# 3. Bootstrap (opens browser, you sign in once, cached forever)
python -m seo_agent.gsc_client --bootstrap

# 4. Discover your IDs
python -m seo_agent.gsc_client --list-sites       # copy your site_url
python -m seo_agent.ga4_client --list-properties  # copy your GA4 property

# 5. Configure .env
cp .env.example .env
$EDITOR .env    # GSC_SITE_URL + GA4_PROPERTY_ID required

# 6. Dry run — your verification gate
set -a; source .env; set +a
python -m seo_agent.health_check --skip-d1
# Expect: GSC OK, GA4 OK, OVERALL PASS

# 7. Edit slots + bans for YOUR site
$EDITOR seo_agent/config.py
$EDITOR seo_agent/prompts/variant_generator.md   # set {SITE_NAME}, {SITE_URL}

# 8. Deploy to VPS (see docs/02-VPS_DEPLOY.md)
bash scripts/install_vps.sh
```

That's it. Loop ticks every 10 min, proposes 2–4 variants per slot,
validates, inserts to D1. Nightly poll attributes GSC clicks/impressions
to specific variants. After 14 days + 5k impressions a winner gets
promoted into the static template via PR.

## How the parts fit

### Python package — `seo_agent/`

| File | Purpose |
|---|---|
| `gsc_client.py` | OAuth user flow, Search Console reads, `--bootstrap` + `--list-sites` CLI |
| `ga4_client.py` | Same OAuth token → GA4 Admin (`--list-properties`) + Data API (engagement) |
| `gsc_poller.py` | Nightly job: pulls per-page GSC into `seo_gsc_daily`. Early-outs when no variants assigned yet (no wasted D1 round-trips). |
| `d1_client.py` | Cloudflare D1 HTTP API client. Reads `CLOUDFLARE_D1_API_TOKEN` (or `CF_API_TOKEN` or `CLOUDFLARE_API_TOKEN`). |
| `config.py` | **YOUR site's slots + bans + branch.** This is the one file you'll edit per project. |
| `allocator.py` | Thompson sampling math (Beta(α,β) posteriors). |
| `variant_generator.py` | Builds the Claude CLI prompt; spawns `claude -p` subprocess (plan auth); validates output. |
| `loop.py` | The agent — generate → validate → store → eval → kill/promote. Run as a long-running systemd service. |
| `health_check.py` | One-shot dry run: GSC + GA4 + D1 + Anthropic. Your launch gate. |
| `grant_ga4_access.py` | One-shot — grant a service account viewer access on GA4 via the Admin API (still works post-2024 UI block). |

### Edge integration — `examples/astro-cloudflare/`

| File | Purpose |
|---|---|
| `middleware.ts` | Reads/mints `_arx` cookie. Passes `isBot` flag. Looks up variants. Cache `caches.default` for 60s. |
| `lib-seo-variants.ts` | `pickSlotText(slot, fallback, field='text', vars={})` — reads variant + substitutes placeholders. |
| `migrations/0001_seo_experiments.sql` | The 4 D1 tables: `seo_variants`, `seo_assignments`, `seo_outcomes`, `seo_gsc_daily`. |

For non-Astro / non-Cloudflare stacks, see `docs/03-CUSTOMIZING.md` —
the data shape is portable, the middleware pattern translates to Next.js,
Remix, etc.

### Health watchdog — `scripts/seo_health_alert.py`

The loop fails silently in one nasty way: when the VPS Claude login expires,
generation 401s — and when **all slots are saturated** the loop doesn't even
attempt generation, so a dead login is invisible in the journal. A naive "0
variants in 24h" check is wrong (0 variants is healthy when saturated), so
this watchdog keys on *failure*: `systemctl` liveness, journal failure/auth
markers, and a proactive `claude -p "AUTH_OK"` probe that catches a dead login
even while saturated. It emails exact re-auth steps via ForwardEmail.

`install_vps.sh` installs it as `seo-agent-health-alert.timer` (daily). Set
`SEO_HEALTH_EMAIL` in the unit (or `.env`) to receive alerts, else it runs
log-only. All knobs are env-overridable (`SEO_HEALTH_SERVICE`, `_HOURS`,
`_SENDER`, `_ENV_FILE`, `_AUTH_PROBE`). Manual check / test:

```bash
python scripts/seo_health_alert.py --dry-run     # print verdict, never email
python scripts/seo_health_alert.py --force-email # send a test alert now
```

### Docs

| Doc | Purpose |
|---|---|
| `docs/01-GCP_SETUP.md` | Cloud Console click-paths (APIs, OAuth client) |
| `docs/02-VPS_DEPLOY.md` | SSH, git worktree, systemd, claude CLI auth |
| `docs/03-CUSTOMIZING.md` | Per-site config: slots, banned tokens, prompts, middleware |
| `docs/04-OPERATIONS.md` | What to watch, how to interpret D1 rows, how to debug |
| `scripts/seo_health_alert.py` | Daily watchdog: emails on dead auth / hung / failing generation |

## The 2024 SA blockade — the thing this repo solves

Since 2024, Google has blocked service-account emails from being added
via UI to both Search Console ("email not found") and GA4 ("doesn't
match a Google Account"). The widely-suggested Group workaround
**also doesn't work for GSC anymore**.

This repo skips the whole problem:

- **GSC**: authenticate as the **property owner** via OAuth Desktop
  client. You sign in once on your laptop, the refresh token caches,
  scp it to the VPS, it refreshes silently from then on. Same token
  reads every property you own — no per-site grants.
- **GA4**: same OAuth token. The Admin API still accepts SA grants if
  you ever need delegation (`grant_ga4_access.py`), but for your own
  reads, OAuth is the simpler path.

## Critical gotcha: `claude -p` vs `claude -p --bare`

This was the #1 thing that broke our deploy. The Claude Code CLI's
`--bare` flag explicitly disables OAuth/keychain auth, forcing
`ANTHROPIC_API_KEY`-only. From `claude --help`:

> --bare: ... Anthropic auth is strictly ANTHROPIC_API_KEY or apiKeyHelper
> via --settings (OAuth and keychain are never read).

**If you want to use your Claude Pro/Max plan (no API key), do NOT
pass `--bare`.** Use `claude -p PROMPT --output-format text --model
sonnet ...`. The loop in this repo does the right thing — just don't
re-introduce `--bare` if you customize.

## Reusability — adding a new site

For miningstore.com or any new vibe-coding project:

1. **In the new site's GCP project**: enable Search Console API, GA4
   Data API, GA4 Admin API. Create one Desktop OAuth client. Download
   JSON.
2. **On your laptop**: clone this repo, drop the JSON into
   `credentials/`, run `python -m seo_agent.gsc_client --bootstrap`.
   You'll see every property your Google account owns — pick the one
   for this site.
3. **Edit `seo_agent/config.py`** for the new site's slots / banned
   tokens. Edit `seo_agent/prompts/variant_generator.md` to set the
   site's name + URL + audience.
4. **scp credentials + .env to a VPS**, run `scripts/install_vps.sh`.
   Same VPS can host multiple agents — one git worktree + one systemd
   unit set per site.

Per-site setup time: ~10 min if you've done it once.

## License

MIT.

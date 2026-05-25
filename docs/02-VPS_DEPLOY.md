# 02 - VPS deploy

Run the SEO agent on your own VPS. Ubuntu 22.04+ / Python 3.11+.
You don't need a beefy box — `t3.small` / equivalent is fine.

## The architectural decision: git worktree

If you're running multiple agents on the same VPS (e.g. an
autoresearch loop that does `git fetch` / `git reset --hard` on its
own working tree), **untracked code in that working tree gets wiped**.
Don't put the SEO agent in a directory that another loop manages.

Recommended layout:

```
~/your-site/         ← whatever the other loops use (may be on a weird branch)
~/seo-agent/         ← git worktree pinned to origin/main, owned by THIS agent
```

A worktree shares `.git/` (cheap, no double clone) but has its own
working tree. Sibling loops can do whatever they want to their
working tree; the seo-agent worktree is independent.

```bash
# On the VPS, set up the worktree
cd ~/your-site-repo
git fetch origin main
git worktree add ~/seo-agent origin/main
```

If you have no sibling repo on the VPS, just `git clone` directly to
`~/seo-agent` and skip the worktree dance.

## 1. Bootstrap on your laptop first

Do the OAuth dance on a machine with a browser. This mints a refresh
token you'll copy to the VPS — the VPS never needs a browser.

```bash
# On your laptop, in this repo
python3 -m venv .venv && source .venv/bin/activate
pip install -r seo_agent/requirements.txt

# Put your OAuth Desktop client secrets here, then bootstrap
# (see docs/01-GCP_SETUP.md for the Cloud Console steps)
python -m seo_agent.gsc_client --bootstrap
```

This writes:
- `credentials/gsc-oauth-secrets.json` (you saved this from GCP earlier)
- `credentials/gsc-oauth-token.json` (bootstrap wrote it just now)

Both `chmod 600`, both gitignored.

## 2. Provision the VPS

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip git
```

Set up the worktree (or clone, if no sibling repo):

```bash
# Option A: worktree (recommended if you have a sibling repo)
cd ~/your-site-repo
git worktree add ~/seo-agent origin/main

# Option B: fresh clone
git clone git@github.com:miningstore/vibe-seo-agent.git ~/seo-agent
```

```bash
cd ~/seo-agent
python3 -m venv seo_agent/.venv
source seo_agent/.venv/bin/activate
pip install -r seo_agent/requirements.txt
```

## 3. Copy credentials and env from laptop to VPS

From your laptop:

```bash
scp credentials/gsc-oauth-secrets.json vps:~/seo-agent/credentials/
scp credentials/gsc-oauth-token.json   vps:~/seo-agent/credentials/
scp .env                                vps:~/seo-agent/

ssh vps "chmod 600 ~/seo-agent/credentials/gsc-oauth-*.json ~/seo-agent/.env"
```

## 4. Claude CLI — use your plan, NOT an API key

The agent loop invokes `claude -p PROMPT --output-format text ...` as
a subprocess. This uses whatever auth your Claude Code CLI is already
configured for — typically your Claude Pro/Max plan via OAuth, with
credentials at `~/.claude/.credentials.json`.

**Set up Claude Code on the VPS once** (same as you would on a laptop):

```bash
# On the VPS
curl -fsSL https://claude.ai/install.sh | bash
# Or: download the appropriate Claude Code binary for your OS

# Authenticate — this opens a URL you copy/paste to your laptop browser
# to complete OAuth. The cached credentials at ~/.claude/.credentials.json
# work headless from then on.
claude
# (Inside Claude, run /login or whatever the current Claude Code does)
```

Confirm:

```bash
ls -la ~/.claude/.credentials.json
~/.local/bin/claude --version
# Should print something like "2.x.y (Claude Code)"
```

**Do not set ANTHROPIC_API_KEY in your .env unless you specifically
want to bypass plan auth and pay per-token directly.** If you set
`ANTHROPIC_API_KEY`, the CLI may prefer it over the plan and you'll
see surprise charges.

**Do not pass `--bare` to the claude CLI.** The variant generator in
this repo deliberately omits it. From `claude --help`:

> `--bare`: ... Anthropic auth is strictly ANTHROPIC_API_KEY or
> apiKeyHelper via --settings (OAuth and keychain are never read).

If you fork and customize and accidentally add `--bare`, plan auth
will break.

## 5. Sanity check on the VPS

```bash
ssh vps
cd ~/seo-agent
source seo_agent/.venv/bin/activate
set -a; source .env; set +a
python -m seo_agent.health_check
```

Expected:

```
GSC    OK    N properties visible, R rows / I imp / C clicks ...
GA4    OK    M accounts / P properties, K/N sample paths have traffic
D1     OK    seo_assignments=0 seo_gsc_daily=0 ... (or rows if running already)
ANTH.  SKIP  ANTHROPIC_API_KEY not set (skipped)   ← this is FINE, plan auth doesn't need it

OVERALL: PASS
```

## 6. Install the systemd timer for nightly GSC polls

Edit the unit files to match your username + path. The provided files
assume `User=ubuntu` and `WorkingDirectory=/home/ubuntu/seo-agent`.

```bash
sudo cp systemd/seo-gsc-poller.service /etc/systemd/system/
sudo cp systemd/seo-gsc-poller.timer   /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now seo-gsc-poller.timer
sudo systemctl list-timers | grep seo
```

The timer runs daily at 03:00 server-local (+ random 15min jitter).
Check the log via `sudo journalctl -u seo-gsc-poller.service -f`.

## 7. Install the agent loop service

Only do this once you've enabled at least one slot in `config.py` and
verified the variant text renders correctly on a city page. The loop
calls Claude (your plan) — make sure `~/.claude/.credentials.json`
exists and is owned by the same user the service runs as.

```bash
sudo cp systemd/seo-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now seo-agent.service
sudo journalctl -u seo-agent.service -f
```

You should see:

```
INFO seo_agent: picked target: slot=<your_slot> page_match={}
INFO seo_agent: generation: proposed=4 surviving=4 rejected=0
INFO seo_agent: inserted variant id=1
INFO seo_agent: inserted variant id=2
...
INFO seo_agent: tick done; sleeping 600s
```

## Critical systemd-specific things that break agents

### `PYTHONPATH` must be set

When systemd launches `python -m seo_agent.gsc_poller` from the
worktree's venv, the CWD-on-sys.path trick that works interactively
sometimes doesn't apply to non-interactive python (depends on Python
version + venv config). Always set `Environment=PYTHONPATH=/home/ubuntu/seo-agent`
explicitly in the unit. The provided unit files already do this.

### `PATH` must include `~/.local/bin`

For the agent loop to find the `claude` binary. Provided unit files
set `Environment=PATH=/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin`.

### `EnvironmentFile=` for credentials

Don't bake secrets into the unit file. Use `EnvironmentFile=/home/ubuntu/seo-agent/.env`
and keep the `.env` chmod 600.

## Rotating credentials

- **OAuth refresh token expired** (rare — happens on revoke / 6 mo
  inactivity / scope change): re-run `python -m seo_agent.gsc_client
  --bootstrap` locally, scp the new `gsc-oauth-token.json` to the VPS,
  restart the services.
- **Cloudflare D1 API token rotated**: edit `.env` on the VPS, restart
  `seo-agent.service` and `seo-gsc-poller.timer`.
- **Claude Code re-authenticated**: nothing to do — `~/.claude/.credentials.json`
  is read at every invocation.

## Why no service accounts

See README. Service accounts on GSC have been blocked at the UI since
2024. We sidestep with OAuth user auth as the property Owner. One
token, every property auto-accessible.

If you specifically want to grant a contractor / agent / sub-account
read-only access to your GA4 (not GSC), use
`python -m seo_agent.grant_ga4_access ...` — that path goes through
the Admin API which still accepts SAs.

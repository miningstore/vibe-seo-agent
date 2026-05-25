# 02 - VPS deploy

Run the SEO agent on your own VPS. Assumes Ubuntu 22.04+ and Python
3.11+. You don't need a beefy box — t3.small / equivalent is fine.

## 1. Bootstrap on your laptop first

Do the OAuth dance on a machine with a browser (your laptop). This
mints a refresh token you'll copy to the VPS — the VPS never needs a
browser.

```bash
# On your laptop, in this repo
python3 -m venv .venv && source .venv/bin/activate
pip install -r seo_agent/requirements.txt

# Put your OAuth Desktop client secrets here, then bootstrap
# (see docs/01-GCP_SETUP.md for the Cloud Console steps)
python -m seo_agent.gsc_client --bootstrap
```

This writes:
- `credentials/gsc-oauth-secrets.json` (you saved this earlier from GCP)
- `credentials/gsc-oauth-token.json` (bootstrap wrote it just now)

Both `chmod 600`, both gitignored.

## 2. Provision the VPS

Any Ubuntu host with SSH access works. On the VPS:

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip git
mkdir -p ~/vibe-seo-agent
```

Clone the repo onto the VPS (use your fork if you've customized
config.py):

```bash
git clone git@github.com:miningstore/vibe-seo-agent.git ~/vibe-seo-agent
cd ~/vibe-seo-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r seo_agent/requirements.txt
```

## 3. Copy credentials and env from your laptop to the VPS

From your laptop:

```bash
scp credentials/gsc-oauth-secrets.json vps:~/vibe-seo-agent/credentials/
scp credentials/gsc-oauth-token.json   vps:~/vibe-seo-agent/credentials/
scp .env                                vps:~/vibe-seo-agent/

ssh vps "chmod 600 ~/vibe-seo-agent/credentials/gsc-oauth-*.json ~/vibe-seo-agent/.env"
```

## 4. Sanity check on the VPS

```bash
ssh vps
cd ~/vibe-seo-agent
source .venv/bin/activate
set -a; source .env; set +a
python -m seo_agent.health_check
```

Expected `OVERALL: PASS`.

## 5. Install the systemd timer for nightly GSC polls

```bash
# Edit the unit files for your username + paths if they differ
sudo cp systemd/seo-gsc-poller.service /etc/systemd/system/
sudo cp systemd/seo-gsc-poller.timer   /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now seo-gsc-poller.timer
sudo systemctl list-timers | grep seo
```

The timer runs daily at 03:00 server-local. Check the log via
`journalctl -u seo-gsc-poller.service -f`.

## 6. (Optional) Install the agent loop service

Only do this once you've enabled at least one slot in `config.py` and
verified variants render correctly on your site (see `03-CUSTOMIZING.md`).
The loop calls Anthropic — make sure `ANTHROPIC_API_KEY` is in `.env`
and you've set a `SEO_DAILY_SPEND_CAP_USD`.

```bash
sudo cp systemd/seo-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now seo-agent.service
sudo journalctl -u seo-agent.service -f
```

## Rotating credentials

- **OAuth refresh token expired** (rare — only on revoke / 6 months of
  inactivity / scope change): re-run `python -m seo_agent.gsc_client
  --bootstrap` locally, scp the new token to the VPS.
- **Cloudflare D1 token rotated**: edit `.env` on the VPS, restart
  `seo-agent.service`.

## Why no service accounts

See README. Service accounts on GSC have been blocked at the UI since
2024. We sidestep by using OAuth user auth as the property Owner. One
token, every property auto-accessible.

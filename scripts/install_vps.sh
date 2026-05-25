#!/usr/bin/env bash
# Install vibe-seo-agent on a fresh Ubuntu VPS.
#
# Pre-reqs (one-time, on your laptop):
#   1. Created OAuth Desktop client in GCP (see docs/01-GCP_SETUP.md)
#   2. Ran `python -m seo_agent.gsc_client --bootstrap` locally
#   3. scp'd credentials/gsc-oauth-*.json + .env to this VPS
#
# Then run this script on the VPS:
#   bash scripts/install_vps.sh

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/vibe-seo-agent}"
cd "$PROJECT_DIR"

# --- 1. System deps ---------------------------------------------------------
if ! command -v python3 >/dev/null; then
  sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
fi

# --- 2. Python venv + deps --------------------------------------------------
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r seo_agent/requirements.txt
echo "deps installed:"
pip list 2>/dev/null | grep -iE "google|anthropic|requests" | head -10

# --- 3. Sanity: required files present --------------------------------------
for f in credentials/gsc-oauth-secrets.json credentials/gsc-oauth-token.json .env; do
  if [ ! -f "$f" ]; then
    echo "ERROR: $f missing. scp it from your laptop:"
    echo "  scp $f vps:$PROJECT_DIR/$f"
    exit 1
  fi
done
chmod 600 credentials/gsc-oauth-secrets.json credentials/gsc-oauth-token.json .env

# --- 4. Health check --------------------------------------------------------
set -a; source .env; set +a
echo ""
echo "=== Running health check (this is your dry run) ==="
python -m seo_agent.health_check || {
  echo "Health check FAILED. Fix above before installing systemd units."
  exit 1
}

# --- 5. Install systemd units ----------------------------------------------
SYSTEMD_USER="${SUDO_USER:-$USER}"
echo ""
echo "Install systemd units?"
echo "  This will copy systemd/*.service and *.timer to /etc/systemd/system/,"
echo "  enable seo-gsc-poller.timer (nightly), and prepare seo-agent.service"
echo "  (which you start manually once you've enabled slots in config.py)."
read -rp "Proceed? [y/N] " yn
if [[ "$yn" != "y" && "$yn" != "Y" ]]; then
  echo "Skipping systemd install. Re-run when ready."
  exit 0
fi

sudo cp systemd/seo-gsc-poller.service systemd/seo-gsc-poller.timer systemd/seo-agent.service \
  /etc/systemd/system/
# Patch the WorkingDirectory + paths if the user is not 'ubuntu' or
# the project dir is not /home/ubuntu/vibe-seo-agent
sudo sed -i "s|/home/ubuntu/vibe-seo-agent|$PROJECT_DIR|g; s|User=ubuntu|User=$SYSTEMD_USER|g" \
  /etc/systemd/system/seo-gsc-poller.service \
  /etc/systemd/system/seo-agent.service

sudo systemctl daemon-reload
sudo systemctl enable --now seo-gsc-poller.timer
echo ""
echo "Enabled seo-gsc-poller.timer (runs nightly at 03:00 server-local)"
sudo systemctl list-timers | grep seo

echo ""
echo "DONE. Next:"
echo "  1. Watch the first nightly run:  journalctl -u seo-gsc-poller.service -f"
echo "  2. When ready, enable the agent: sudo systemctl enable --now seo-agent.service"
echo "  3. See docs/03-CUSTOMIZING.md to plug into your site."

#!/usr/bin/env bash
# One-shot setup for an always-on Ubuntu host (VPS) that runs the edge-finder
# backend 24/7 -- the scheduler polls Kalshi/ESPN, settles bets, and fires Discord
# alerts on its own timers, so NO frontend and NO public exposure are needed for
# alerts to work. Run this ONCE, from the repo root, on a fresh Ubuntu 24.04 box:
#
#   git clone <your-private-repo-url> ~/nfl-edge-app
#   cd ~/nfl-edge-app
#   bash deploy/setup.sh
#
# Idempotent: safe to re-run. To deploy new code later, use deploy/deploy.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$REPO_ROOT/backend"
DATA_DIR="$HOME/nfl-edge-data"      # persistent SQLite -- OUTSIDE the repo so git never touches it
SERVICE_USER="$(whoami)"
PORT="${NFL_EDGE_PORT:-8756}"

echo "==> Installing system packages (python 3.12, venv, git)"
sudo apt-get update -y
sudo apt-get install -y python3.12 python3.12-venv git curl

echo "==> Creating venv + installing backend deps"
python3.12 -m venv "$BACKEND/.venv"
"$BACKEND/.venv/bin/pip" install --upgrade pip
# pywebview is desktop-GUI only and the server never imports it -- if it fails to
# build on a headless host, install everything else and carry on.
if ! "$BACKEND/.venv/bin/pip" install -r "$BACKEND/requirements.txt"; then
  echo "   (full install failed; retrying without the desktop-only pywebview)"
  grep -v '^pywebview' "$BACKEND/requirements.txt" | "$BACKEND/.venv/bin/pip" install -r /dev/stdin
fi

mkdir -p "$DATA_DIR"

echo "==> Writing systemd service (auto-restart + start on boot)"
sudo tee /etc/systemd/system/nfl-edge.service >/dev/null <<UNIT
[Unit]
Description=Edge Finder backend (pollers + settlement + Discord alerts)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$BACKEND
Environment=NFL_EDGE_DATA_DIR=$DATA_DIR
Environment=NFL_EDGE_HOST=127.0.0.1
Environment=NFL_EDGE_PORT=$PORT
ExecStart=$BACKEND/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $PORT --app-dir $BACKEND
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

echo "==> Enabling + starting the service"
sudo systemctl daemon-reload
sudo systemctl enable nfl-edge.service
sudo systemctl restart nfl-edge.service
sleep 4

echo "==> Health check"
curl -fsS "http://127.0.0.1:$PORT/health" && echo || echo "(not up yet -- check: sudo journalctl -u nfl-edge -n 50)"

cat <<DONE

==> Done. The backend now runs 24/7 (polls, settles, alerts) and restarts on crash/reboot.

Next steps:
  1. Set your Discord webhook so alerts fire (run ON this host):
       curl -X PUT http://127.0.0.1:$PORT/settings/alerts \\
         -H 'Content-Type: application/json' \\
         -d '{"webhook_url":"https://discord.com/api/webhooks/XXX/YYY","min_edge_pp":0.05}'
     (Or carry your local settings+bets over: scp your portable_state.json here, then
      cd backend && .venv/bin/python scripts/portable_data.py import path/to/portable_state.json)

  2. Watch logs:      sudo journalctl -u nfl-edge -f
  3. Deploy new code: cd $REPO_ROOT && bash deploy/deploy.sh
DONE

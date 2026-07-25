#!/usr/bin/env bash
# Deploy new code to the always-on host: pull the latest from git, refresh Python
# deps if requirements changed, and restart the service. Run from the repo root
# on the host after you've `git push`ed from your dev machine:
#
#   cd ~/nfl-edge-app && bash deploy/deploy.sh
#
# The SQLite DB lives OUTSIDE the repo (in ~/nfl-edge-data), so pulling code never
# touches your bet/CLV history.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$REPO_ROOT/backend"

echo "==> git pull"
git -C "$REPO_ROOT" pull --ff-only

echo "==> refreshing backend deps (no-op if unchanged)"
"$BACKEND/.venv/bin/pip" install -q -r "$BACKEND/requirements.txt"

echo "==> restarting service"
sudo systemctl restart nfl-edge.service
sleep 4

PORT="${NFL_EDGE_PORT:-8756}"
curl -fsS "http://127.0.0.1:$PORT/health" && echo " <- healthy" || echo "(check: sudo journalctl -u nfl-edge -n 50)"

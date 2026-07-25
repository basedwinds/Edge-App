# Always-on deployment (keep alerts running while you're away)

Your home PC does **not** need to stay on. This runs the backend on a small cloud
host 24/7: the scheduler polls the markets, settles bets, and fires **Discord
alerts** on its own timers — all with just the backend process alive. No frontend
and no public exposure are needed for alerts to work.

You keep developing on your laptop exactly as now: **push to git → run one deploy
command on the host**. The host keeps its own database (your real bet/CLV
history lives there); your laptop's DB stays separate for testing.

---

## What you'll need
- A **GitHub repo** with this code pushed (see `REMOTE_SETUP.md` for the one-time push).
- A small **always-on Linux host**. Recommended: an **Ubuntu 24.04** VPS — e.g.
  Hetzner (CX22, ~€4/mo) or DigitalOcean ($6/mo). The cheapest tier is plenty
  (this is a light Python process + SQLite). A cloud host means **no fire hazard
  and no dependence on your home network.**

## Step 1 — create the VPS
Create an Ubuntu 24.04 droplet/server in your provider's console, and note its IP.
Add your SSH key during creation so you can log in:
```bash
ssh root@YOUR_SERVER_IP
```
(Optional but nice: create a non-root user and use that instead of root.)

## Step 2 — clone + run setup (on the VPS)
```bash
sudo apt-get update -y && sudo apt-get install -y git
git clone https://github.com/<your-username>/nfl-edge-app.git ~/nfl-edge-app
cd ~/nfl-edge-app
bash deploy/setup.sh
```
`setup.sh` installs Python, builds the venv, and registers a **systemd service**
(`nfl-edge`) that runs the backend, **auto-restarts on crash, and starts on
reboot**. When it finishes it prints a `/health` check.

## Step 3 — turn on Discord alerts
Create a Discord channel webhook (Server Settings → Integrations → Webhooks → New
Webhook → Copy URL), then, **on the VPS**:
```bash
curl -X PUT http://127.0.0.1:8756/settings/alerts \
  -H 'Content-Type: application/json' \
  -d '{"webhook_url":"https://discord.com/api/webhooks/XXX/YYY","min_edge_pp":0.05}'
```
That's it — you'll get a batched Discord message whenever a genuinely new bet
clears your edge floor. (Prefer to carry over your existing settings + placed
bets instead? On your laptop run `python scripts/portable_data.py export`, `scp`
the `portable_state.json` to the VPS, then `cd backend && .venv/bin/python
scripts/portable_data.py import ~/portable_state.json`.)

---

## Everyday workflow (develop + deploy)
On your **laptop** — edit + push as usual:
```bash
git add -A && git commit -m "…" && git push
```
On the **VPS** — pull + restart:
```bash
cd ~/nfl-edge-app && bash deploy/deploy.sh
```
The DB is outside the repo (`~/nfl-edge-data`), so deploying code never touches
your history.

## Handy commands (on the VPS)
```bash
sudo systemctl status nfl-edge      # is it running?
sudo journalctl -u nfl-edge -f      # live logs (watch alerts/polls fire)
sudo systemctl restart nfl-edge     # manual restart
```

---

## Optional — check the tracker from your phone
Alerts don't need this, but if you also want to open the app's UI remotely, the
frontend currently hard-codes the API at `127.0.0.1:8756`, so it needs (a) the
API base made configurable + the built frontend served, and (b) either a private
tunnel (Tailscale — free, recommended) or an exposed port with auth. That's a
small follow-up build — ask and I'll wire it up. For a hands-off vacation,
Discord alerts alone usually cover it.

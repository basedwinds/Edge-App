# Running this project on another computer (e.g. while travelling)

You do **not** need to leave your home PC on. The whole project is code + a local
database; move the code via GitHub and the small bit of data that matters via a
2 MB export. Then run Claude Code against the clone on any machine.

## One-time: put the code on GitHub (do this before you leave)

The repo is already initialized and committed locally (no secrets are stored —
the app uses only public APIs, and the database is git-ignored). To push it:

1. On https://github.com create a **new, private** repository (e.g. `nfl-edge-app`). Don't add a README/gitignore — the repo already has them.
2. In the project folder (`C:\Users\awaws\Downloads\nfl-edge-app`), run:
   ```
   git remote add origin https://github.com/<your-username>/nfl-edge-app.git
   git branch -M main
   git push -u origin main
   ```
   You'll be asked to sign in to GitHub — that part is yours (I can't enter your credentials).

## Carry your data (bets + settings), not the 973 MB DB

The market-snapshot history rebuilds itself once the pollers run, so don't copy
the whole DB. Export the part that can't be re-scraped — your placed bets and
settings — into a 2 MB file:

```
cd backend
.venv/Scripts/python.exe scripts/portable_data.py export
```

That writes `portable_state.json`. Email it to yourself / drop it in cloud
storage / put it on a USB stick. (Or, if you'd rather have the full history,
just copy the whole DB file from `%LOCALAPPDATA%\nfl-edge-app\app.db` — it's big
but works too.)

## On the other computer

1. Install the prerequisites: **Git**, **Python 3.12**, **Node 18+**, and **Claude Code**.
2. Clone and set up:
   ```
   git clone https://github.com/<your-username>/nfl-edge-app.git
   cd nfl-edge-app/backend
   python -m venv .venv
   .venv/Scripts/activate        # Windows;  source .venv/bin/activate on Mac/Linux
   pip install -r requirements.txt
   python scripts/portable_data.py import portable_state.json   # restores your bets+settings
   cd ../frontend
   npm install
   ```
3. Open Claude Code in the `nfl-edge-app` folder and keep working exactly as at home.
   Start the servers the same way (backend on :8756, frontend on :5173).

## What keeps running vs pauses while you're away

- **Code work** (via Claude Code on the laptop): fully portable — this setup is all you need.
- **The live data pipeline + paper-logging/CLV**: only runs while a copy of the
  app is actually running. On the laptop it resumes the moment you start the
  backend; it re-scrapes current markets and picks CLV back up going forward.
  (If you want it running 24/7 with no laptop, that's the "deploy to a cloud
  server" option — ask and I'll write up those steps.)

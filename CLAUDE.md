# Edge Finder — project context for Claude

This file travels with the repo (via git), so **any machine that clones it gives Claude this context** — unlike Claude Code's per-machine memory, which does NOT sync between the home PC and the laptop. If you're a fresh Claude on a new machine: read this top to bottom, then `git log --oneline -40` for the detailed change history (commit messages are written to be a running narrative).

The user is **non-technical** and relies entirely on Claude to make code changes, run commands, and walk them through anything on the machine. Explain steps simply; never assume they can code.

## What this is
A multi-sport prediction-market **edge finder**: it prices Kalshi/Polymarket markets against in-house Elo/Monte-Carlo models across NFL, NBA, WNBA, MLB, MMA, Tennis, Soccer, CS2, Valorant, LoL, and racing (F1/NASCAR/IndyCar), and surfaces where the model disagrees with the market ("edge").

- **Backend**: FastAPI + SQLAlchemy + SQLite, Python, runs on port **8756**.
- **Frontend**: React + TypeScript + Vite, runs on port **5173**, hash-routed (`#/mlb`, `#/all`, etc.).
- **Frontend hardcodes the API at `127.0.0.1:8756`** (`frontend/src/api/client.ts`), so the UI only ever talks to a backend on the same machine.

## Hard constraints (do not violate)
- **No paid APIs** — public/free data sources only.
- **No LLM in pricing or bet reasoning** — models are rule-based; reasoning is seeded/templated, not generated.
- Every model ships **`model_validated: false`**. The honest thesis: *no model reliably beats the market on average; the only proof of a real edge is beating the CLOSING line, forward (CLV).* Don't oversell edges.
- **Verify against real data before shipping.** The user values correctness over speed and dislikes rushed/speculative builds.
- **The DB is git-ignored** and machine-local. No secrets in the repo (public APIs only; the Discord webhook is set at runtime, never committed).
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. The user pushes to GitHub themselves (Claude cannot enter their credentials).

## How to run it locally (Windows)
From the repo root: `scripts/dev.ps1` launches backend + frontend in two windows. Then open `http://localhost:5173`.
- Backend only: `scripts/run_backend.ps1` (uses `backend/.venv`). **Defaults to STABLE — no `--reload`.** Pass `-Reload` to watch files while editing backend code.
- **`--reload` gotcha**: every backend file save triggers a full cache-warm reboot, during which requests just queue. Measured 2026-08-12 mid-reload after a five-file edit: `/health` 45.9s, then 1.3s once settled; `/cfb/markets` 0.90s then 0.29s. This is what "the app loads extremely slowly sometimes" and "cfb takes a while to warm up" were — a reboot, not a hang and not a slow model. Hence the flipped default: the person USING the app gains nothing from `--reload` and is only stalled by it. **In stable mode a code change needs a manual backend restart to take effect.**
- First-time setup on a new machine: create the venv (`cd backend && python -m venv .venv && .venv/Scripts/pip install -r requirements.txt`), and `cd frontend && npm install`. (On macOS/Linux the `.ps1` scripts won't run — use the equivalent shell commands; ask the user which OS the laptop is.)
- The SQLite DB lives at `%LOCALAPPDATA%\nfl-edge-app\app.db` (Windows) or `<cwd>/data/app.db` otherwise — NOT in the repo.

## Deployment (the always-on part)
The user won't leave the home PC on. A **Vultr Ubuntu VPS ("edge-app")** runs the **backend only, 24/7**, and is the thing that sends **Discord alerts**. It has its **own separate database** (fresh; the PC's bet history is not on it). No frontend runs on the server.
- One-time server setup: `deploy/setup.sh` (systemd service `nfl-edge`, binds 127.0.0.1:8756).
- **Deploy new code**: user pushes to GitHub, then on the Vultr console: `cd ~/nfl-edge-app && bash deploy/deploy.sh` (git pull + restart). A "couldn't connect :8756" at the end is a known slow-boot false alarm.
- Discord webhook is stored in the server's DB (set once via `curl -X PUT .../settings/alerts`), alert floor `min_edge_pp` = 0.03.
- **Alerts** are produced by `backend/app/models/paper_logger.py` on a 30-min schedule (`app/scheduler.py`). It self-HTTPs the `/markets` endpoints, logs every edge-qualified market as a paper bet (for forward CLV), and pings Discord for **newly-qualified STAKED bets**, capped at 6 per sport, with a readiness gate (see below). `_BASE` is hardcoded `http://127.0.0.1:8756` — the app self-HTTPs itself, so it must run on 8756.

## Moving data between machines
Bet history + settings (NOT the market-snapshot history) move via `backend/scripts/portable_data.py`:
- Export (on the machine that has the data): `cd backend && .venv/Scripts/python scripts/portable_data.py export` → `portable_state.json`.
- Import (on the new machine, after venv exists): `.venv/Scripts/python scripts/portable_data.py import path/to/portable_state.json` (idempotent upsert).

## Key architecture notes
- **Recommended bets** are assembled in the **frontend** (`frontend/src/api/markets.ts` `buildRecommendedBets` + leaner per-sport variants; futures shortlist in `pages/Combined.tsx` `loadCombinedFutures`). Passes: ladder-collapse → cross-platform dedup (`crossPlatformKey`) → per-player cap → `capToOneRowPerGame` → per-pool bankroll-budget cap. The Discord alert re-derives its own set in the backend and is NOT yet a byte-for-byte match (see pending tasks).
- **Readiness gate** (shared rule, alerts + UI): far-future games (kickoff > 14d) and season-sport futures whose season isn't active/near (within ~21d, preseason excluded via `game_type != 'PRE'`) are hidden/not-pinged. Backend: `paper_logger._sport_season_active` + `GET /markets/readiness`. Frontend: `markets.ts` `isRowNotReady`/`isFuturesSportNotReady`, wired into `RecommendedBetsTable`, `FuturesTable`, and `CrossSportFuturesTable`. Event-based sports (tennis/mma/esports/racing) are not season-gated.
- **Copycat guard**: `create_placed_bet` (backend) refuses a duplicate real pending bet for the same cross-platform key (so the same bet can't be recorded twice from Kalshi + Polymarket).
- Settlement, CLV, and paper-logging span all sports; Kalshi market-resolution is the authoritative settlement path.

## Current state (2026-07-25)
Everything below is deployed to the Vultr server and live: 30-min alert cadence, staked-only + per-sport-cap-6 alerts, cross-platform dedup, readiness gate across alerts + the whole UI, per-sport futures fairness cap. Alerts are CLOSE to the Recommended tab but not identical yet.

## Pending tasks (agreed for the laptop, in priority order)
1. **Laptop setup**: install Claude Code + Python + Node + git; clone this repo; create the backend venv + `npm install`; import the user's `portable_state.json`.
2. **Make Discord alerts EXACTLY match the Recommended-bets tab.** Currently close but not identical because the tab's full cap pipeline (esp. the bankroll-budget cap) lives only in the frontend. Right fix: build ONE shared backend "recommended set" that both the app and `paper_logger` read, VERIFIED by comparing backend output to the live tab sport-by-sport before trusting it. This is a careful multi-hour port — don't rush it into the alert system.
3. **Cross-platform PLACED de-dup in the Recommended view.** Symptom: user marks a bet placed on one book, the same bet reappears (other book, different `market_id`) and they place it twice. Cause: the "already placed" check is keyed by `market_id`, but the deduped row flips which book it shows. Fix: key the placed-set by `crossPlatformKey`, not `market_id`.
4. **Same-GAME line churn** (open design question — confirm intent with user first). After placing e.g. Over 4.5, an Over 5.5 for the same game appears and they're unsure whether to place it. Decide: (a) treat the whole game as covered, (b) dedup within a ladder, or (c) leave it. Leaning per-game placed awareness (extend the placed-set key to game-id for game-tied markets).

The richest decision-history lives in Claude's memory on the home PC (DESKTOP-T6RV5CH); this file + `git log` are the portable substitute.

# NFL Edge Finder

Desktop app that estimates NFL win/spread/total probabilities from historical stats
(with a bounded news/research adjustment), cross-references live Kalshi/Polymarket
prices, and surfaces mispriced markets.

## Dev setup

Backend (first time only):
```
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Frontend (first time only):
```
cd frontend
npm install
```

Run both dev servers:
```
scripts\dev.ps1
```
- Backend: http://127.0.0.1:8756/health
- Frontend: http://localhost:5173

Run as a desktop window (points at the Vite dev server):
```
$env:NFL_EDGE_DEV=1
backend\.venv\Scripts\python.exe desktop\launcher.py
```

## Status

Phase 0 (scaffolding) complete — see `docs/` and the build plan for the full phase
sequence (live data ingestion → baseline model → dashboard → spread/totals →
news adjustment → packaging → backtest validation).

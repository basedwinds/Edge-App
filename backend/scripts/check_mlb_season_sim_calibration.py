"""Is the MLB season simulator calibrated on the FUTURES it actually prices?

WHY THIS EXISTS. season_sim_mlb.py's own docstring calls it a "free reference
estimate, not validated to beat the market". 726 active markets are priced from
it -- win_total 201, ws_matchup 225, playoff_qualifier 60, division_winner 60,
pennant 60, World Series 60, best/worst record 60 -- while every MLB backtest in
this repo (backtest_moneyline_mlb / _spread_mlb / _totals_mlb) tests GAME
markets. Nothing has ever checked the season outputs.

That is the same shape as the racing top_n bug: a model fitted and validated on
one question, then asked several others that nobody checked. Racing turned out
to be 20-30pp wrong in a consistent direction. Unlike the CFB and MLS brackets
(1-2 completed events, uncalibratable, correctly badged "approximate"), MLB has
11 seasons -- so this one can actually be answered.

WHAT IS MEASURED. Preseason playoff-qualification probability, per team per
season, against whether that team actually qualified (data/mlb_playoff_teams.json
is the real answer key). Playoff odds are chosen because they are a REAL priced
market (playoff_qualifier, 60 rows) and because a binary outcome with a known
answer key gives clean calibration buckets. Win totals share the same simulation,
so a bias here implicates them too.

NO LEAKAGE. Elo entering each season is built ONLY from earlier seasons, and the
simulation is run from an empty slate (every game unplayed), which is exactly how
a preseason future is priced. The season being scored contributes nothing to the
ratings that price it.

READING IT. The rows that matter are the extremes. Racing's failure was
overconfidence -- favourites too high, longshots too low, error growing with
distance from the head of the distribution. If MLB shares it, expect teams
projected above ~70% to qualify less often than claimed, and teams below ~20% to
qualify more often.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.season_sim_mlb import run_simulation

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
from app.models.baseline import elo_mlb

# PRODUCTION parameters. The first pass used hand-picked K=4/HFA=24 and applied
# NO season regression, which let ratings carry forward too strongly and would
# inflate confidence on its own -- the same "benchmark against the shipped model,
# not a stand-in" lesson the MMA style work hit earlier the same day.
BASE = elo_mlb.BASE_RATING
K = elo_mlb.K                              # 5.0
HFA = elo_mlb.HOME_FIELD_ADV               # 22.0
SEASON_REGRESSION = elo_mlb.SEASON_REGRESSION   # 0.25 toward the mean each year
N_TRIALS = 2000
WARMUP_SEASONS = 1

BUCKETS = [(0.0, 0.10), (0.10, 0.25), (0.25, 0.40), (0.40, 0.60),
           (0.60, 0.75), (0.75, 0.90), (0.90, 1.01)]


def load_games():
    d = json.loads((DATA_DIR / "mlb_schedule_cache.json").read_text(encoding="utf-8"))
    rows = d if isinstance(d, list) else list(d.values())
    if rows and isinstance(rows[0], list):
        rows = [x for sub in rows for x in sub]
    return [r for r in rows if r.get("game_type") == "R"]


def bucket_of(p):
    for lo, hi in BUCKETS:
        if lo <= p < hi:
            return (lo, hi)
    return None


def main() -> None:
    games = load_games()
    playoff = json.loads((DATA_DIR / "mlb_playoff_teams.json").read_text(encoding="utf-8"))
    by_season = defaultdict(list)
    for g in games:
        by_season[int(g["season"])].append(g)
    seasons = sorted(by_season)
    print(f"seasons={seasons[0]}-{seasons[-1]}  games={len(games)}")

    elo = defaultdict(lambda: BASE)
    agg = defaultdict(lambda: [0.0, 0, 0])   # bucket -> [sum_p, hits, n]
    scored_seasons = []

    for si, season in enumerate(seasons):
        rows = sorted(by_season[season], key=lambda r: r["gameday"])
        actual = set(playoff.get(str(season)) or [])

        # Production regresses every rating toward the mean between seasons.
        # Omitting this was the first pass's biggest divergence from shipped
        # behaviour and pushes directly on the quantity under test.
        if si > 0:
            for t in list(elo):
                elo[t] = BASE + (1 - SEASON_REGRESSION) * (elo[t] - BASE)

        # Score this season ONLY if we have both warmed ratings and an answer key.
        if si >= WARMUP_SEASONS and actual:
            ratings = {t: elo[t] for t in {r["home_team"] for r in rows} | {r["away_team"] for r in rows}}
            # Preseason: every game unplayed, which is how a future is priced.
            sim_games = [{"home_team": r["home_team"], "away_team": r["away_team"],
                          "home_score": None, "away_score": None, "location": None} for r in rows]
            res = run_simulation(ratings, sim_games, n_trials=N_TRIALS, seed=season)
            n_hit = 0
            for team, out in res.items():
                if team.startswith("_"):
                    continue
                p = out.get("playoff_pct")
                if p is None:
                    continue
                p = p / 100.0 if p > 1.0 else float(p)
                b = bucket_of(p)
                if b is None:
                    continue
                made = int(team in actual)
                n_hit += made
                a = agg[b]
                a[0] += p; a[1] += made; a[2] += 1
            scored_seasons.append((season, len(actual), n_hit))

        # ---- update Elo with this season's REAL results (walk-forward) -------
        for r in rows:
            hs, as_ = r.get("home_score"), r.get("away_score")
            if hs is None or as_ is None:
                continue
            h, a = r["home_team"], r["away_team"]
            eh, ea = elo[h] + HFA, elo[a]
            ph = 1.0 / (1.0 + 10 ** ((ea - eh) / 400.0))
            sh = 1.0 if hs > as_ else (0.0 if hs < as_ else 0.5)
            elo[h] += K * (sh - ph)
            elo[a] += K * ((1 - sh) - (1 - ph))

    print(f"scored {len(scored_seasons)} seasons: " +
          ", ".join(f"{s}({m} qualifiers, {h} matched a bucket)" for s, m, h in scored_seasons))

    print(f"\n{'model says':>14s} {'teams':>6s} {'avg model':>10s} {'actual':>8s} {'diff':>8s}")
    for b in BUCKETS:
        psum, hits, n = agg[b]
        if n < 8:
            continue
        avg, act = psum / n, hits / n
        flag = "  <-- OVER" if avg - act > 0.07 else ("  <-- under" if act - avg > 0.07 else "")
        print(f"{f'{b[0]:.0%}-{b[1]:.0%}':>14s} {n:6d} {avg:10.3f} {act:8.3f} {avg-act:+8.3f}{flag}")

    num = den = 0.0
    for b, (psum, hits, n) in agg.items():
        if n < 8:
            continue
        num += n * abs(psum / n - hits / n); den += n
    print(f"\nweighted mean calibration error: {num/den:.4f}" if den else "\nno scorable buckets")


if __name__ == "__main__":
    main()

"""Scores the MLB season sim's PENNANT and WORLD SERIES legs -- the two it was
never validated on.

WHY THIS EXISTS. check_mlb_season_sim_calibration.py validated the sim's
`playoff_pct` and that is what "MLB season sim VALIDATED" has meant since. But
the sim also emits `pennant_pct` and `championship_pct`, and those feed real
priced inventory: 60 `conference_champion` (pennant) rows, 60 `championship`
rows, and the 225 `ws_matchup` legs that are built from the JOINT pennant
counts. Roughly a third of the MLB futures book rode on two legs nobody had
ever scored.

WHY NOT BUCKETED CALIBRATION. A pennant happens twice a season and a title
once, so across the scorable seasons there are only ~8 title outcomes against
~240 team-seasons. Calibration buckets at that density measure noise. Two
things are measurable instead:

  1. MEAN PROBABILITY ASSIGNED TO THE TEAM THAT ACTUALLY WON, against the naive
     baseline (1/30 for the title, 1/15 for a pennant). A model that knows
     nothing scores the baseline; a model with real information beats it. This
     is the same "always compare against the market/baseline, never against a
     coin flip" discipline used everywhere else in this app.
  2. WHETHER THE PROBABILITIES SUM TO THE RIGHT TOTAL. Every simulated season
     produces exactly one champion and exactly two pennant winners, so summing
     each leg over all teams must give 1.0 and 2.0. A sum that drifts is an
     accounting bug in the sim, and it would be invisible in any per-team check.

Truth comes from data/mlb_postseason_cache.json (real postseason games, 2016-
2025) -- the pennant winner is whoever won the most games in that season's
AL/NL Championship Series, the champion whoever won the most World Series
games. Deriving it from game rows rather than a hand-typed list means it cannot
silently disagree with the schedule cache the sim is fed.

model_validated stays whatever the outcome of this says it should be; this
script only measures.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.season_sim_mlb import run_simulation

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
from app.models.baseline import elo_mlb

BASE = elo_mlb.BASE_RATING
K = elo_mlb.K
HFA = elo_mlb.HOME_FIELD_ADV
SEASON_REGRESSION = elo_mlb.SEASON_REGRESSION
N_TRIALS = 2000
WARMUP_SEASONS = 1


def load_games():
    d = json.loads((DATA_DIR / "mlb_schedule_cache.json").read_text(encoding="utf-8"))
    return d if isinstance(d, list) else (d.get("games") or [])


def postseason_truth():
    """(champion_by_season, pennant_winners_by_season) from real postseason games."""
    rows = json.loads((DATA_DIR / "mlb_postseason_cache.json").read_text(encoding="utf-8"))
    ws: dict[int, Counter] = defaultdict(Counter)
    pen: dict[int, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for e in rows:
        s, series, w = int(e["season"]), e["series"], e["winner"]
        if not w:
            continue
        if "World Series" in series:
            ws[s][w] += 1
        elif "Championship Series" in series:
            pen[s][series[:2]][w] += 1
    champ = {s: c.most_common(1)[0][0] for s, c in ws.items() if c}
    penn = {s: {lg: c.most_common(1)[0][0] for lg, c in d.items() if c} for s, d in pen.items()}
    return champ, penn


def _pct(v):
    if v is None:
        return None
    v = float(v)
    return v / 100.0 if v > 1.0 else v


def main() -> None:
    games = load_games()
    champ_by_season, pennant_by_season = postseason_truth()
    by_season = defaultdict(list)
    for g in games:
        by_season[int(g["season"])].append(g)
    seasons = sorted(by_season)
    print(f"schedule seasons {seasons[0]}-{seasons[-1]}; postseason truth for "
          f"{min(champ_by_season)}-{max(champ_by_season)}")

    elo = defaultdict(lambda: BASE)
    title_p, pennant_p = [], []
    sum_title, sum_pennant = [], []
    rows_out = []

    for si, season in enumerate(seasons):
        rows = sorted(by_season[season], key=lambda r: r["gameday"])
        if si > 0:
            for t in list(elo):
                elo[t] = BASE + (1 - SEASON_REGRESSION) * (elo[t] - BASE)

        champ = champ_by_season.get(season)
        penns = set((pennant_by_season.get(season) or {}).values())
        if si >= WARMUP_SEASONS and champ:
            teams = {r["home_team"] for r in rows} | {r["away_team"] for r in rows}
            ratings = {t: elo[t] for t in teams}
            sim_games = [{"home_team": r["home_team"], "away_team": r["away_team"],
                          "home_score": None, "away_score": None, "location": None} for r in rows]
            res = run_simulation(ratings, sim_games, n_trials=N_TRIALS, seed=season)
            tot_t = tot_p = 0.0
            for team, out in res.items():
                if team.startswith("_"):
                    continue
                ct, pp = _pct(out.get("championship_pct")), _pct(out.get("pennant_pct"))
                if ct is not None:
                    tot_t += ct
                    if team == champ:
                        title_p.append(ct)
                if pp is not None:
                    tot_p += pp
                    if team in penns:
                        pennant_p.append(pp)
            sum_title.append(tot_t)
            sum_pennant.append(tot_p)
            rows_out.append((season, champ, title_p[-1] if title_p else None,
                             sorted(penns), tot_t, tot_p))

        # Walk the season forward so the NEXT season starts from warmed ratings.
        for r in rows:
            hs, as_ = r.get("home_score"), r.get("away_score")
            if hs is None or as_ is None:
                continue
            h, a = r["home_team"], r["away_team"]
            exp_h = 1.0 / (1.0 + 10 ** (-((elo[h] + HFA) - elo[a]) / 400.0))
            res_h = 1.0 if hs > as_ else 0.0
            elo[h] += K * (res_h - exp_h)
            elo[a] += K * (exp_h - res_h)

    print()
    print(f"{'season':7s} {'champion':9s} {'P(champ)':>9s}  pennant winners      sum(title) sum(pennant)")
    for s, c, p, pw, tt, tp in rows_out:
        print(f"{s:<7d} {c:9s} {('%.4f' % p) if p is not None else '   n/a':>9s}  "
              f"{','.join(pw):20s} {tt:9.3f} {tp:11.3f}")

    n = len(title_p)
    print()
    print(f"scored seasons: {n}")
    if n:
        mt, mp = sum(title_p) / n, sum(pennant_p) / max(1, len(pennant_p))
        print(f"  mean P assigned to the ACTUAL champion : {mt:.4f}   baseline 1/30 = {1/30:.4f}   "
              f"{'BEATS' if mt > 1/30 else 'FAILS'} baseline ({mt/(1/30):.2f}x)")
        print(f"  mean P assigned to ACTUAL pennant winner: {mp:.4f}   baseline 1/15 = {1/15:.4f}   "
              f"{'BEATS' if mp > 1/15 else 'FAILS'} baseline ({mp/(1/15):.2f}x)")
        print()
        st, sp = sum(sum_title) / n, sum(sum_pennant) / n
        print(f"  ACCOUNTING: mean sum(championship_pct) = {st:.4f}  (must be 1.000)")
        print(f"              mean sum(pennant_pct)      = {sp:.4f}  (must be 2.000)")
        bad = abs(st - 1.0) > 0.02 or abs(sp - 2.0) > 0.04
        print(f"              -> {'FAIL: the legs do not add up' if bad else 'OK'}")


if __name__ == "__main__":
    main()

"""Re-fits NFL TEAM_STRENGTH_SIGMA on ELEVEN seasons instead of five.

WHY THIS EXISTS. season_sim.py's sigma comment is unusually candid: the value
was fitted over 2021-2025, leave-one-season-out improved only 3 of 5 folds
(2021 and 2023 got worse), the fitted sigma ranged 100-150, and 100 was taken
as the LOW end deliberately -- "the DIRECTION is not in doubt ... the magnitude
is the uncertain part, so it is deliberately under-applied".

That ambiguity was a SAMPLE problem, and the sample was never the constraint.
The schedule cache holds eleven complete seasons (2015-2025); the fit used the
last five. Six more were sitting there unused.

WHAT IS NOT AVAILABLE, checked before writing this: a newer season. 2026 has 1
of 321 games played -- the season starts in September -- so "re-fit now that
2026 is done" is not a thing that can be done. The gain here comes from going
BACKWARD, not forward.

THE 16-VS-17 GAME CHANGE IS FINE, and worth stating because it looks like it
should not be. 2015-2020 are 16-game seasons, 2021+ are 17. Sigma is pre-season
uncertainty about a team's TRUE STRENGTH in Elo points -- it is not expressed in
wins -- and each season is simulated against its own real schedule, so a 16-game
year produces 16-game win totals naturally. What would break is fitting a
threshold in win units across the boundary; that is not what this does.

WHAT IS MEASURED. For each scorable season, project it from prior-years-only
Elo with zero games played (exactly how a preseason future is priced), then
score every team-vs-threshold prediction the win histogram supports against
what actually happened. Two numbers per candidate sigma:

  * Brier over all team-threshold predictions -- the headline.
  * Mean absolute calibration gap, bucketed. sigma=0 is famously overconfident
    (the shipped comment records 88.5% predicted -> 76.3% happened), so the gap
    is what sigma is actually for.

Leave-one-season-out is reported per fold rather than summarised, because a
single full-sample optimum is exactly what the original fit could not trust.

This script only MEASURES. It prints a recommendation; it does not edit the
constant.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import season_sim
from app.models.baseline import elo as elo_mod

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
BASE = elo_mod.BASE_RATING
K = elo_mod.K
HFA = elo_mod.HOME_FIELD_ADV
SEASON_REGRESSION = elo_mod.SEASON_REGRESSION

N_TRIALS = 1500
WARMUP_SEASONS = 2          # two prior seasons before any rating is trusted
SIGMAS = [0.0, 100.0, 150.0, 200.0, 250.0, 300.0, 400.0, 500.0]
BUCKETS = [(0.0, 0.10), (0.10, 0.25), (0.25, 0.40), (0.40, 0.60),
           (0.60, 0.75), (0.75, 0.90), (0.90, 1.01)]


def _bucket(p: float):
    for lo, hi in BUCKETS:
        if lo <= p < hi:
            return (lo, hi)
    return None


def load_games() -> list[dict]:
    from app.db.database import SessionLocal
    from app.db.models import NflGame

    s = SessionLocal()
    try:
        rows = s.query(NflGame).all()
        return [
            {"season": g.season, "week": getattr(g, "week", None),
             "home_team": g.home_team, "away_team": g.away_team,
             "home_score": g.home_score, "away_score": g.away_score,
             "location": getattr(g, "location", None)}
            for g in rows if g.season is not None
        ]
    finally:
        s.close()


def score_season(ratings, rows, actual_wins, sigma) -> tuple[list, list]:
    """(brier_terms, (bucket, p, hit) rows) for one season at one sigma."""
    sim_games = [{"home_team": r["home_team"], "away_team": r["away_team"],
                  "home_score": None, "away_score": None,
                  "location": r.get("location")} for r in rows]
    old = season_sim.TEAM_STRENGTH_SIGMA
    season_sim.TEAM_STRENGTH_SIGMA = sigma
    try:
        res = season_sim.run_simulation(ratings, sim_games, n_trials=N_TRIALS, seed=1234)
    finally:
        season_sim.TEAM_STRENGTH_SIGMA = old

    brier, cal = [], []
    for team, out in res.items():
        if team.startswith("_") or not isinstance(out, dict):
            continue
        hist = out.get("win_count_pct")
        if not hist or team not in actual_wins:
            continue
        total = sum(hist) or 1.0
        cum = 0.0
        # P(wins >= t) for every threshold the histogram supports.
        for t in range(1, len(hist)):
            cum += hist[t - 1] / total
            p = max(0.0, min(1.0, 1.0 - cum))
            if p <= 0.001 or p >= 0.999:
                continue          # degenerate thresholds carry no information
            hit = 1 if actual_wins[team] >= t else 0
            brier.append((p - hit) ** 2)
            b = _bucket(p)
            if b:
                cal.append((b, p, hit))
    return brier, cal


def main() -> None:
    games = load_games()
    by_season = defaultdict(list)
    for g in games:
        by_season[int(g["season"])].append(g)
    complete = sorted(s for s, rs in by_season.items()
                      if len(rs) > 250 and sum(1 for r in rs if r["home_score"] is not None) >= len(rs) * 0.95)
    print(f"complete NFL seasons available: {complete[0]}-{complete[-1]} ({len(complete)})")
    print(f"the shipped sigma was fitted on 2021-2025 (5). This uses {len(complete)}.\n")

    # Walk Elo forward once; snapshot the preseason ratings for each season.
    elo = defaultdict(lambda: BASE)
    preseason: dict[int, dict] = {}
    actual: dict[int, dict] = {}
    for i, season in enumerate(complete):
        rows = by_season[season]
        if i > 0:
            for t in list(elo):
                elo[t] = BASE + (1 - SEASON_REGRESSION) * (elo[t] - BASE)
        teams = {r["home_team"] for r in rows} | {r["away_team"] for r in rows}
        preseason[season] = {t: elo[t] for t in teams}
        wins = defaultdict(int)
        for r in rows:
            hs, as_ = r["home_score"], r["away_score"]
            if hs is None or as_ is None:
                continue
            h, a = r["home_team"], r["away_team"]
            if hs > as_:
                wins[h] += 1
            elif as_ > hs:
                wins[a] += 1
            exp_h = 1.0 / (1.0 + 10 ** (-((elo[h] + HFA) - elo[a]) / 400.0))
            res_h = 1.0 if hs > as_ else (0.0 if as_ > hs else 0.5)
            elo[h] += K * (res_h - exp_h)
            elo[a] += K * (exp_h - res_h)
        actual[season] = dict(wins)

    scorable = complete[WARMUP_SEASONS:]
    print(f"scoring {len(scorable)} seasons: {scorable[0]}-{scorable[-1]}\n")

    per_season: dict[float, dict[int, float]] = {s: {} for s in SIGMAS}
    overall = {}
    for sigma in SIGMAS:
        all_brier, all_cal = [], []
        for season in scorable:
            b, c = score_season(preseason[season], by_season[season], actual[season], sigma)
            all_brier += b
            all_cal += c
            per_season[sigma][season] = sum(b) / len(b) if b else float("nan")
        agg = defaultdict(lambda: [0.0, 0, 0])
        for bkt, p, hit in all_cal:
            a = agg[bkt]
            a[0] += p; a[1] += hit; a[2] += 1
        gaps = [abs(a[0] / a[2] - a[1] / a[2]) for a in agg.values() if a[2] >= 30]
        overall[sigma] = (sum(all_brier) / len(all_brier) if all_brier else float("nan"),
                          sum(gaps) / len(gaps) if gaps else float("nan"),
                          len(all_brier))
        print(f"  sigma={sigma:5.0f}  brier={overall[sigma][0]:.5f}  "
              f"mean|cal gap|={overall[sigma][1]:.4f}  n={overall[sigma][2]}")

    best_brier = min(SIGMAS, key=lambda s: overall[s][0])
    best_gap = min(SIGMAS, key=lambda s: overall[s][1])
    print(f"\nfull-sample best by Brier: sigma={best_brier:.0f}")
    print(f"full-sample best by calibration gap: sigma={best_gap:.0f}")

    print("\nLEAVE-ONE-SEASON-OUT (best sigma on the other seasons, by Brier):")
    picks = []
    for held in scorable:
        others = [s for s in scorable if s != held]
        pick = min(SIGMAS, key=lambda sg: sum(per_season[sg][s] for s in others) / len(others))
        picks.append(pick)
        shipped = per_season[100.0][held]
        print(f"   hold out {held}: picks sigma={pick:5.0f}   "
              f"brier on {held}: pick={per_season[pick][held]:.5f} vs shipped100={shipped:.5f}"
              f"   {'better' if per_season[pick][held] < shipped else 'worse/equal'}")
    from collections import Counter
    tally = Counter(picks)
    print(f"\n   fold picks: {dict(tally)}   modal={tally.most_common(1)[0][0]:.0f}")
    print(f"   shipped value is 100. Change it only if the folds AGREE -- the "
          f"original fit did not, which is why 100 was chosen conservatively.")


if __name__ == "__main__":
    main()

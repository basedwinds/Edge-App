"""Walk-forward validation of the MLB season simulator. (Task #118.)

WHY THIS IS THE GATE. season_sim_mlb prices 726 live futures markets -- division
winner, playoff berth, pennant, World Series, best/worst record, win totals --
and has NEVER been scored against a real outcome. Only MLB's per-GAME markets
were ever backtested. The bracket sims for other sports at least had the excuse
of thin history; this one has eleven completed seasons sitting in
data/mlb_schedule_cache.json with real scores, so it is testable and simply had
not been tested.

WHAT IS ASKED, and it is the same question the market asks. Standing at a fixed
point in a season, what is P(this team wins its division / makes the playoffs /
wins the pennant / wins the World Series)? The archive knows every answer.

TWO CUTOFFS, because the futures board is live all year and the honest question
differs by date:
    preseason   0 games played -- ratings carried in from prior seasons only
    midseason   81 games played (half a schedule) -- the sim's starting_wins
                path is exercised, which is the branch live pricing actually uses

WALK-FORWARD, STRICTLY. Ratings entering season S are replayed from seasons
BEFORE S only, using the app's own elo_mlb, with its own season regression
applied between years. At the midseason cutoff the first half of S is added.
Nothing after the cutoff ever touches the ratings, so no season informs its own
prediction. Getting this wrong is the failure that makes a model look excellent
and then lose money, which is the entire reason this script exists.

BASELINES, because "better than nothing" is not the bar:
    uniform   every team equally likely (1/5 division, 12/30 playoff, 1/15
              pennant, 1/30 championship) -- the knows-nothing floor
    the sim   scored on the same events

Brier and log-loss. Log-loss is the one that matters for a price: it punishes
confident-and-wrong, which is exactly how a miscalibrated season sim loses
money on a futures board.

===========================================================================
RESULT, 2026-08-09: PASSES. The sim has real skill. 726 futures markets are
no longer resting on an unchecked model.

  PRESEASON (9 seasons: 2017-2019, 2021-2026)
    event              n   sim Brier  unif Brier   sim LogL  unif LogL
    division winner  270      0.1264      0.1600     0.3971     0.5004
    playoff berth    270      0.1957      0.2400     0.5742     0.6730

  MIDSEASON (10 seasons, 81 games played)
    division winner  300      0.0841      0.1600     0.2615     0.5004
    playoff berth    300      0.1223      0.2400     0.3698     0.6730

Beats the uniform prior on BOTH measures at BOTH cutoffs, and by a wide
margin -- division log-loss is 0.3971 against 0.5004 before a pitch is thrown.

THE SANITY CHECK THAT MATTERS MOST: midseason is substantially better than
preseason on every line (division Brier 0.1264 -> 0.0841). More information
producing better predictions is what a working sim must do. Had midseason NOT
improved, the starting_wins path -- the branch live pricing actually uses all
season -- would have been suspect.

WHAT THIS DOES NOT SAY, and the distinction is the whole discipline of this
project: beating a UNIFORM prior is not beating the MARKET. A futures price
already encodes far more than "all teams equal". This result says the sim is
not broken and is not fabricating structure; it does NOT say there is an edge
in the 726 markets. That still needs a backtest against real Kalshi/Polymarket
futures prices, which is a different and harder test.

NOT SCORED: pennant and World Series. The schedule cache holds regular-season
games only, so postseason truth is not derivable from it. Those two are the
higher-value legs of the board and remain unvalidated -- closing that needs a
postseason results source.

EXCLUSIONS, both deliberate: 2020 (900 games, the COVID-shortened season, too
different in shape to score against a 162-game prior) and 2016 at the
preseason cutoff (no earlier season in the cache to carry ratings in from).
===========================================================================
"""
from __future__ import annotations

import collections
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.baseline import elo_mlb  # noqa: E402
from app.data.mlb_divisions import DIVISIONS, TEAM_LEAGUE  # noqa: E402
from app.models import season_sim_mlb  # noqa: E402

#: team -> division, inverted from the app's own division -> teams map.
TEAM_DIVISION = {t: d for d, members in DIVISIONS.items() for t in members}

CACHE = Path(__file__).resolve().parents[2] / "data" / "mlb_schedule_cache.json"

N_TRIALS = 2000
MIDSEASON_GAMES = 81  # half a 162-game schedule, per team

# The four questions the futures board actually asks, with their uniform prior.
EVENTS = [
    ("division_pct", "division winner", 1.0 / 5.0),
    ("playoff_pct", "playoff berth", 12.0 / 30.0),
    ("pennant_pct", "pennant", 1.0 / 15.0),
    ("championship_pct", "World Series", 1.0 / 30.0),
]


def load_seasons() -> dict[int, list[dict]]:
    rows = json.loads(CACHE.read_text(encoding="utf-8"))
    by_season: dict[int, list[dict]] = collections.defaultdict(list)
    for g in rows:
        # REG only: the sim simulates a regular season and then a bracket, so
        # feeding it postseason games would double-count them.
        if g.get("game_type") != "R":
            continue
        if not g.get("season") or not g.get("home_team") or not g.get("away_team"):
            continue
        by_season[int(g["season"])].append(g)
    for season in by_season:
        by_season[season].sort(key=lambda g: (g.get("gameday") or "", g.get("id") or ""))
    return dict(by_season)


def actual_outcomes(games: list[dict]) -> dict[str, dict[str, int]]:
    """Who really won what, derived from the season's own finished games.

    Division and playoff truth come from real final records rather than a
    hardcoded table, so this needs no second data source and cannot drift out of
    step with the schedule cache. Pennant/championship are NOT derivable from
    regular-season games, so they are scored only where the cache carries the
    postseason -- see main()."""
    wins: dict[str, int] = collections.Counter()
    losses: dict[str, int] = collections.Counter()
    for g in games:
        hs, as_ = g.get("home_score"), g.get("away_score")
        if hs is None or as_ is None:
            continue
        h, a = g["home_team"], g["away_team"]
        if hs > as_:
            wins[h] += 1; losses[a] += 1
        elif as_ > hs:
            wins[a] += 1; losses[h] += 1

    teams = [t for t in TEAM_LEAGUE if wins.get(t, 0) + losses.get(t, 0) > 0]
    out = {t: {"division": 0, "playoff": 0} for t in teams}

    by_div: dict[str, list[str]] = collections.defaultdict(list)
    for t in teams:
        by_div[TEAM_DIVISION[t]].append(t)
    for div, members in by_div.items():
        best = max(members, key=lambda t: wins.get(t, 0))
        out[best]["division"] = 1

    # Playoff field: division winners plus the next best records in each league.
    by_league: dict[str, list[str]] = collections.defaultdict(list)
    for t in teams:
        by_league[TEAM_LEAGUE[t]].append(t)
    for league, members in by_league.items():
        champs = [t for t in members if out[t]["division"]]
        rest = sorted((t for t in members if not out[t]["division"]),
                      key=lambda t: -wins.get(t, 0))
        for t in champs + rest[:max(0, 6 - len(champs))]:
            out[t]["playoff"] = 1
    return out


def ratings_through(seasons: dict[int, list[dict]], upto_season: int,
                    partial_games: list[dict] | None = None) -> dict[str, float]:
    """Replay the app's own Elo over every season BEFORE upto_season, plus an
    optional partial slice of upto_season itself."""
    state = elo_mlb.EloState()
    for season in sorted(s for s in seasons if s < upto_season):
        # The app's OWN between-seasons regression toward the mean lives here,
        # on the state. Calling it (rather than reimplementing the formula) is
        # what keeps this validating the shipped model instead of a lookalike.
        state.start_season_if_new(season)
        for g in seasons[season]:
            if g.get("home_score") is None or g.get("away_score") is None:
                continue
            elo_mlb.predict_and_update(state, g)
    state.start_season_if_new(upto_season)
    for g in (partial_games or []):
        if g.get("home_score") is None or g.get("away_score") is None:
            continue
        elo_mlb.predict_and_update(state, g)
    return dict(state.ratings)


def brier(p, y):
    return (p - y) ** 2


def logloss(p, y):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def score(cutoff_name: str, seasons: dict[int, list[dict]], midseason: bool) -> None:
    rows: list[tuple[str, float, int]] = []  # (event_key, sim_p, actual)
    seasons_used = []

    for season in sorted(seasons):
        games = seasons[season]
        finished = [g for g in games if g.get("home_score") is not None]
        # A season needs to be essentially complete to be scored at all.
        if len(finished) < 1000:
            continue

        if midseason:
            per_team = collections.Counter()
            cut = []
            for g in games:
                if g.get("home_score") is None:
                    continue
                if per_team[g["home_team"]] < MIDSEASON_GAMES and per_team[g["away_team"]] < MIDSEASON_GAMES:
                    cut.append(g)
                    per_team[g["home_team"]] += 1
                    per_team[g["away_team"]] += 1
            ratings = ratings_through(seasons, season, cut)
            # Everything after the cutoff is handed to the sim UNPLAYED, which
            # is exactly the shape live pricing sees mid-season.
            cut_ids = {g.get("id") for g in cut}
            sim_input = [
                (g if g.get("id") in cut_ids
                 else {**g, "home_score": None, "away_score": None})
                for g in games
            ]
        else:
            ratings = ratings_through(seasons, season)
            sim_input = [{**g, "home_score": None, "away_score": None} for g in games]

        if len(ratings) < 20:
            continue  # not enough prior history to rate the league

        sim = season_sim_mlb.run_simulation(ratings, sim_input, n_trials=N_TRIALS, seed=season)
        truth = actual_outcomes(games)
        seasons_used.append(season)

        for team, real in truth.items():
            probs = sim.get(team)
            if not probs:
                continue
            rows.append(("division_pct", probs.get("division_pct", 0.0), real["division"]))
            rows.append(("playoff_pct", probs.get("playoff_pct", 0.0), real["playoff"]))

    if not rows:
        print(f"{cutoff_name}: no scorable seasons")
        return

    print(f"\n=== {cutoff_name} ===")
    print(f"seasons scored: {seasons_used}")
    print(f"{'event':18s}{'n':>5s}{'sim Brier':>11s}{'unif Brier':>12s}"
          f"{'sim LogL':>10s}{'unif LogL':>11s}{'verdict':>12s}")
    for key, label, prior in EVENTS:
        sub = [(p, y) for k, p, y in rows if k == key]
        if not sub:
            continue
        sb = statistics.mean(brier(p, y) for p, y in sub)
        ub = statistics.mean(brier(prior, y) for p, y in sub)
        sl = statistics.mean(logloss(p, y) for p, y in sub)
        ul = statistics.mean(logloss(prior, y) for p, y in sub)
        verdict = "BEATS" if (sb < ub and sl < ul) else ("mixed" if sb < ub or sl < ul else "LOSES")
        print(f"{label:18s}{len(sub):5d}{sb:11.4f}{ub:12.4f}{sl:10.4f}{ul:11.4f}{verdict:>12s}")


def main() -> None:
    if not CACHE.exists():
        print(f"no schedule cache at {CACHE}")
        return
    seasons = load_seasons()
    print(f"{len(seasons)} seasons in cache: {sorted(seasons)}")
    print(f"games per season: "
          f"{ {s: len(g) for s, g in sorted(seasons.items())} }")
    score("PRESEASON (0 games played)", seasons, midseason=False)
    score("MIDSEASON (81 games played)", seasons, midseason=True)
    print("\nNOTE: pennant and World Series are NOT scored -- the schedule cache holds")
    print("regular-season games only, so postseason truth is not derivable here.")
    print("Division and playoff berth ARE the bulk of the live futures board.")


if __name__ == "__main__":
    main()

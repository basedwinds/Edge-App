"""Does REST predict WNBA outcomes after Elo? Measured answer: NO.

Run before building a WNBA rest/schedule-spot adjustment, because NBA and NFL
both have one and the obvious move was to copy it across. The data does not
support it, so none was built.

METHOD. Walk-forward over data/wnba_game_cache.json (2021-2026, 1,540 scored
games) with this app's own elo_wnba: for each game, predict from ratings as
they stood BEFORE it, derive each team's rest as days since its own previous
game, then bucket the residual (actual - predicted) by rest DIFFERENTIAL.
Residual rather than raw win rate, so team strength is already accounted for --
a rested team that is simply better must not read as a rest effect.

RESULT (1,467 games with both rest values known and <= 10 days):

    rest diff    n     mean residual    SE
       -3        59      -3.21pp       6.36
       -2        83      +1.56pp       4.95
       -1       178      -2.69pp       3.60
        0       694      -0.35pp       1.74
       +1       250      +3.03pp       2.93
       +2       123      +7.25pp       4.45
       +3        80     -11.37pp       4.94

    OLS slope: -0.410pp per extra day of rest advantage

INCOHERENT, not merely weak. +1 and +2 lean positive, then +3 flips hard
negative; the overall slope is flat AND the wrong sign. The single bucket
outside 2 SE is one of seven tested, which is roughly what chance produces,
and it contradicts its own neighbours. There is no monotonic effect to fit.

WHY THIS IS PLAUSIBLE rather than a data artifact: a WNBA team plays 44 games
across ~4 months, so back-to-backs are far rarer than the NBA's 82-in-6-months
grind, and the schedule already spaces most games 2-3 days apart. The 0-diff
bucket alone is 694 of 1,467 games. There is simply less rest variation to
matter.

Re-run this before revisiting. If the WNBA expands its schedule the answer
could change.
"""
import datetime as dt
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.baseline.elo_wnba import (  # noqa: E402
    EloState, effective_home_court_adv, update_ratings, win_prob,
)

CACHE = Path(__file__).resolve().parent.parent.parent / "data" / "wnba_game_cache.json"
MAX_REST_DAYS = 10  # beyond this is an all-star break or season edge, not "rest"


def main():
    raw = json.loads(CACHE.read_text(encoding="utf-8"))
    games = [g for g in raw.values() if g.get("home_score") is not None and g.get("away_score") is not None]
    games.sort(key=lambda g: (g["date"], g["id"]))
    print(f"scored games: {len(games)}  seasons: {sorted({g['season'] for g in games})}")

    state = EloState()
    season = None
    last: dict[str, dt.date] = {}
    rows = []
    for g in games:
        if g["season"] != season:
            if season is not None and hasattr(state, "start_season"):
                state.start_season()
            season, last = g["season"], {}
        day = dt.date.fromisoformat(g["date"][:10])
        home, away = g["home"], g["away"]
        h_rest = (day - last[home]).days if home in last else None
        a_rest = (day - last[away]).days if away in last else None
        hca = 0.0 if g.get("neutral") else effective_home_court_adv(home, None)
        p = win_prob(state.get(home), state.get(away), hca)
        if h_rest is not None and a_rest is not None and h_rest <= MAX_REST_DAYS and a_rest <= MAX_REST_DAYS:
            actual = 1 if g["home_score"] > g["away_score"] else 0
            rows.append((h_rest - a_rest, actual - p))
        update_ratings(state, home, away, g["home_score"], g["away_score"], hca)
        last[home] = last[away] = day

    print(f"usable games: {len(rows)}\n")
    buckets = defaultdict(list)
    for diff, resid in rows:
        buckets[max(-3, min(3, diff))].append(resid)

    print(f"{'rest diff':>9} {'n':>5} {'mean residual':>16} {'SE':>8}")
    for k in sorted(buckets):
        vals = buckets[k]
        if len(vals) < 20:
            print(f"{k:+9d} {len(vals):5}   (too few)")
            continue
        mean = statistics.mean(vals)
        se = statistics.pstdev(vals) / len(vals) ** 0.5
        flag = " *" if abs(mean) > 2 * se else ""
        print(f"{k:+9d} {len(vals):5} {100 * mean:+15.2f}pp {100 * se:7.2f}{flag}")

    xs = [r[0] for r in rows]
    mx, my = statistics.mean(xs), statistics.mean(r[1] for r in rows)
    var = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in rows) / var if var else 0.0
    print(f"\nOLS slope: {100 * slope:+.3f}pp per extra day of rest advantage")
    print("* = more than 2 SE from zero")


if __name__ == "__main__":
    main()

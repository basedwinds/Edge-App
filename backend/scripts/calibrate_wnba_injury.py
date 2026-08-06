"""Calibrate the WNBA injury magnitude from real box scores.

Sibling of the NBA calibration that produced injury_rules_nba's POSITION_
WEIGHT_PP / MAX_TOTAL_PP. The point is to MEASURE the number rather than copy
NBA's: a WNBA roster carries ~11-12 active players against the NBA's 12-15, so
one starter is a larger share of team value and the effect should be bigger.

METHOD (deliberately the same as NBA's so the two are comparable):
  1. From data/wnba_boxscore_probe.json, rank each team's players by TOTAL
     minutes across the season -- the season-long ranking, so "star" is not
     defined by the game being measured.
  2. For each game, a top-N player is ABSENT if they do not appear in that
     game's box score at all, or appear with 0 minutes.
  3. Walk-forward elo_wnba over data/wnba_game_cache.json gives each game a
     pre-game win probability, so the residual nets out team strength.
  4. Report the residual for a team with k of its top-N out while the OPPONENT
     is at full strength. Requiring the opponent to be whole is what makes the
     effect attributable -- otherwise two injured teams cancel and the estimate
     is diluted toward zero.

A player who is merely RESTED shows up identically to an injured one here.
That is intentional: the adjustment is about availability, and the market
prices unavailability the same way whatever its cause.
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

DATA = Path(__file__).resolve().parent.parent.parent / "data"
GAMES = DATA / "wnba_game_cache.json"
PROBE = DATA / "wnba_boxscore_probe.json"
TOP_N = 3          # same "top-3 by minutes" definition the NBA calibration used
MIN_BUCKET = 15    # below this a bucket is reported but not treated as evidence


def main():
    probe = {k: v for k, v in json.loads(PROBE.read_text(encoding="utf-8")).items() if v}
    raw = json.loads(GAMES.read_text(encoding="utf-8"))
    games = [g for g in raw.values() if g.get("home_score") is not None]
    games.sort(key=lambda g: (g["date"], g["id"]))

    # Season-long minutes per (season, team, player).
    totals: dict[tuple, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for v in probe.values():
        season = int(v["date"][:4])
        for team, mins in v["minutes"].items():
            for player, m in mins.items():
                totals[(season, team)][player] += m
    top: dict[tuple, set[str]] = {
        key: {p for p, _ in sorted(mins.items(), key=lambda x: -x[1])[:TOP_N]}
        for key, mins in totals.items()
    }
    print(f"box scores: {len(probe)}   team-seasons ranked: {len(top)}")

    by_game = {(v["date"], v["home"], v["away"]): v for v in probe.values()}

    state = EloState()
    season = None
    buckets: dict[int, list[float]] = defaultdict(list)
    matched = 0
    for g in games:
        if g["season"] != season:
            if season is not None and hasattr(state, "start_season"):
                state.start_season()
            season = g["season"]
        home, away = g["home"], g["away"]
        hca = 0.0 if g.get("neutral") else effective_home_court_adv(home, None)
        p_home = win_prob(state.get(home), state.get(away), hca)
        box = by_game.get((g["date"][:10], home, away))
        if box:
            matched += 1
            outs = {}
            for team in (home, away):
                key = (int(g["date"][:4]), team)
                stars = top.get(key) or set()
                mins = box["minutes"].get(team) or {}
                outs[team] = sum(1 for s in stars if mins.get(s, 0) == 0)
            actual_home = 1 if g["home_score"] > g["away_score"] else 0
            # One row per SIDE, from that side's own perspective, but only when
            # the opponent is whole -- see the module docstring.
            if outs[away] == 0:
                buckets[outs[home]].append(actual_home - p_home)
            if outs[home] == 0:
                buckets[outs[away]].append((1 - actual_home) - (1 - p_home))
        update_ratings(state, home, away, g["home_score"], g["away_score"], hca)

    print(f"games joined to a box score: {matched}\n")
    print(f"{'top-3 out':>10} {'n':>5} {'mean residual':>16} {'SE':>8}")
    base = None
    for k in sorted(buckets):
        vals = buckets[k]
        mean = statistics.mean(vals)
        se = statistics.pstdev(vals) / len(vals) ** 0.5 if len(vals) > 1 else float("nan")
        if k == 0:
            base = mean
        note = "" if len(vals) >= MIN_BUCKET else "   (thin)"
        print(f"{k:>10} {len(vals):5} {100 * mean:+15.2f}pp {100 * se:7.2f}{note}")

    print()
    if base is not None:
        for k in sorted(buckets):
            if k == 0 or len(buckets[k]) < MIN_BUCKET:
                continue
            eff = statistics.mean(buckets[k]) - base
            print(f"  {k} of top-{TOP_N} out, opponent full: {100 * eff:+.2f}pp vs the 0-out baseline")
    print("\n(compare NBA: -4.2pp for one out (n=179), -9.6pp for 2+ (n=80))")


if __name__ == "__main__":
    main()

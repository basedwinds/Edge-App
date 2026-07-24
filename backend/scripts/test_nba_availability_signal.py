"""Feasibility gate (task #34): does a KEY player being out predict a team
underperforming its Elo win expectation? If not, an NBA player-impact model is
dead on arrival and we've spent one scrape instead of a full build.

Method:
  1. Walk-forward the SHIPPED NBA Elo (elo_nba, incl. season regression +
     rest/home-court) over all history; record each season-2025 game's
     PRE-game home win prob and actual result.
  2. From ESPN box scores (scripts/build_nba_boxscore_probe.py), identify each
     team's 3 biggest per-game contributors (top avg minutes when they play,
     >= 10 appearances) as its "key players".
  3. Flag, per game, whether each side had a key player OUT (0 min / absent).
  4. Compare actual outcome vs Elo prediction, conditioned on availability. If
     Elo is calibrated (residual ~0) even when a team is missing a star, there
     is no availability signal Elo doesn't already capture -> reject.

Mild, deliberately-accepted lookahead: "key player" is defined from the full
season, not trailing games -- fine for testing whether the EFFECT exists (not
building a leak-free predictor).
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.database import SessionLocal  # noqa: E402
from app.db.models import NbaGame  # noqa: E402
from app.models.baseline.elo_nba import (  # noqa: E402
    EloState, effective_home_court_adv, update_ratings, win_prob,
)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
BOX_PATH = DATA_DIR / "nba_boxscore_probe.json"
SEASON = 2025
N_KEY = 3
MIN_APPEARANCES = 10


def elo_pregame_probs():
    s = SessionLocal()
    try:
        games = [{
            "id": g.id, "season": g.season, "game_type": g.game_type, "gameday": g.gameday,
            "home_team": g.home_team, "away_team": g.away_team,
            "home_score": g.home_score, "away_score": g.away_score,
            "location": g.location, "home_rest": g.home_rest, "away_rest": g.away_rest,
        } for g in s.query(NbaGame).filter(NbaGame.game_type.in_(("REG", "POST", "PLAYIN"))).all()]
    finally:
        s.close()
    games.sort(key=lambda g: (g["season"], g["gameday"], g["id"]))
    state = EloState()
    out = {}
    for g in games:
        state.start_season_if_new(g["season"])
        if g["home_score"] is None or g["away_score"] is None:
            continue
        adv = effective_home_court_adv(g["home_team"], g["location"], g["home_rest"], g["away_rest"])
        if g["season"] == SEASON:
            p = win_prob(state.get(g["home_team"]), state.get(g["away_team"]), adv)
            out[(g["gameday"], g["home_team"], g["away_team"])] = (p, 1.0 if g["home_score"] > g["away_score"] else 0.0)
        update_ratings(state, g["home_team"], g["away_team"], g["home_score"], g["away_score"], adv)
    return out


TRAIL_W = 8          # look at each team's last this-many games
TRAIL_MIN_APP = 5    # a current key player appeared in >= this many of them
TRAIL_MIN_MIN = 26.0 # ...averaging >= this many minutes when they did


def team_game_lists(box):
    """team -> chronological [(date, played_set, minutes_dict)]. Trailing --
    correctly handles mid-season TRADES (a traded-away player drops out of the
    recent window; a new arrival accumulates in), unlike a season-global key
    set which flagged e.g. pre-trade Luka as 'out' for half a season."""
    per = defaultdict(list)
    for v in box.values():
        if not v or not v.get("minutes"):
            continue
        for team, mins in v["minutes"].items():
            played = {p for p, m in mins.items() if m > 0}
            per[team].append((v["date"], played, mins))
    for t in per:
        per[t].sort(key=lambda x: x[0])
    return per


def current_key(team_games, upto_idx):
    """Players who've been heavy-minute regulars over the team's TRAILING_W
    games strictly before index `upto_idx`."""
    window = team_games[max(0, upto_idx - TRAIL_W):upto_idx]
    if len(window) < TRAIL_MIN_APP:
        return None  # not enough recent history to judge
    app = defaultdict(int)
    tot = defaultdict(float)
    for _, _, mins in window:
        for p, m in mins.items():
            if m > 0:
                app[p] += 1
                tot[p] += m
    return {p for p in app if app[p] >= TRAIL_MIN_APP and tot[p] / app[p] >= TRAIL_MIN_MIN}


def main():
    if not BOX_PATH.exists():
        print("box-score scrape not present yet"); return
    box = {k: v for k, v in json.loads(BOX_PATH.read_text(encoding="utf-8")).items()}
    n_box = sum(1 for v in box.values() if v)
    print(f"{n_box} games with box scores")
    if n_box < 400:
        print("(scrape still in progress -- need most of the season for a read)"); return

    probs = elo_pregame_probs()
    per = team_game_lists(box)
    # index each (team, date) -> position in that team's own chronological list
    pos = {}
    for t, lst in per.items():
        for i, (d, _, _) in enumerate(lst):
            pos[(t, d)] = i

    def team_key_out(team, date, played):
        idx = pos.get((team, date))
        if idx is None:
            return None
        ck = current_key(per[team], idx)
        if ck is None:
            return None
        return bool(ck - played)

    # residual buckets
    both_full, home_short, away_short = [], [], []
    for v in box.values():
        if not v or not v.get("minutes"):
            continue
        k = (v["date"], v["home"], v["away"])
        if k not in probs:
            continue
        p, actual = probs[k]
        played = {t: {pl for pl, m in mn.items() if m > 0} for t, mn in v["minutes"].items()}
        h_out = team_key_out(v["home"], v["date"], played.get(v["home"], set()))
        a_out = team_key_out(v["away"], v["date"], played.get(v["away"], set()))
        if h_out is None or a_out is None:
            continue  # not enough trailing history to judge one side
        resid = actual - p  # home actual minus home predicted
        if not h_out and not a_out:
            both_full.append(resid)
        elif h_out and not a_out:
            home_short.append(resid)
        elif a_out and not h_out:
            away_short.append(resid)

    def rpt(name, arr):
        if not arr:
            print(f"  {name:26} n=0"); return
        m = sum(arr) / len(arr)
        print(f"  {name:26} n={len(arr):4}  mean(actual-elo home resid) = {m:+.4f}")

    print("\nHome-win residual (actual - Elo predicted), by availability:")
    rpt("both teams full", both_full)
    rpt("HOME missing a key player", home_short)   # expect NEGATIVE (home underperforms)
    rpt("AWAY missing a key player", away_short)    # expect POSITIVE (home overperforms)

    # combined effect from the shorthanded team's own perspective
    short_resid = [-r for r in home_short] + [r for r in away_short]  # +ve = full team beats Elo vs a shorthanded opp... reorient:
    # shorthanded side's own residual: home_short -> home is short, its resid = actual-p (already home persp)
    sh = [r for r in home_short] + [-(r) for r in away_short]  # home_short: home short (home resid); away_short: away short -> away resid = -(home resid)
    if sh:
        m = sum(sh) / len(sh)
        print(f"\nSHORTHANDED TEAM's own win residual vs Elo: {m:+.4f} over n={len(sh)}")
        print(f"  => a team missing a top-{N_KEY} player wins {abs(m)*100:.1f}pp {'LESS' if m<0 else 'MORE'} than Elo predicts")


if __name__ == "__main__":
    main()

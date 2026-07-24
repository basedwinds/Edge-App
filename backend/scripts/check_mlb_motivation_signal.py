"""Investigation script (not a registered backtest): do teams that (in
hindsight) missed the playoffs that season underperform their own Elo rating
specifically in September/October -- a real, well-known "nothing left to
play for" phenomenon (September call-ups diluting a non-contending team's
roster with rookies, resting veterans, less urgency) that Elo's own rating
(built from the FULL season, including their earlier, presumably more
competitive play) wouldn't automatically capture in-the-moment.

Uses data/mlb_playoff_teams.json (real per-season playoff participants,
derived from MLB Stats API's own postseason schedule -- see
build step in this script's own git history / session notes, gameType=
F,D,L,W) joined against the real walk-forward elo_diff this app's baseline
already computes. NOTE: this uses HINDSIGHT knowledge of final playoff
outcomes as the "non-contending" label, a real limitation -- a team that
was mathematically alive in early September but collapsed isn't
distinguished from one that was already dead by August. This checks the
STRONGEST, cleanest version of the hypothesis (season-long non-playoff
teams' September games) as a first pass, not a live-servable point-in-time
signal (see script docstring at the end for why that matters).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datetime as dt  # noqa: E402
import json  # noqa: E402

import numpy as np  # noqa: E402

from app.models.baseline.elo_mlb import EloState, HOME_FIELD_ADV, NEUTRAL_SITE_HOME_FIELD_ADV, predict_and_update, win_prob  # noqa: E402
from app.models.calibration import brier_score  # noqa: E402
from app.models.pitcher_ratings_mlb import MIN_IP, pitcher_elo_adjustment  # noqa: E402

SCHEDULE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_schedule_cache.json"
PITCHER_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_pitcher_snapshot_cache.json"
PLAYOFF_TEAMS_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_playoff_teams.json"


def _snapshot_for(pitcher_cache, season, game_date, pid):
    best = None
    for date_str, snap in pitcher_cache.get(str(season), {}).items():
        snap_date = dt.date.fromisoformat(date_str)
        if snap_date >= game_date:
            continue
        if best is None or snap_date > best[0]:
            best = (snap_date, snap)
    return best[1].get(pid) if best else None


def main():
    games = json.loads(SCHEDULE_PATH.read_text())
    pitcher_cache = json.loads(PITCHER_CACHE_PATH.read_text())
    playoff_teams = json.loads(PLAYOFF_TEAMS_PATH.read_text())
    all_games = [g for g in games if g["game_type"] == "R" and g["season"] < 2027]
    all_games.sort(key=lambda g: (g["season"], g["gameday"], g["game_number"], g["id"]))

    state = EloState()
    # per-team-side rows: (is_sept_oct, team_made_playoffs_this_season, p_team_win, team_won)
    side_rows = []
    for g in all_games:
        hfa = NEUTRAL_SITE_HOME_FIELD_ADV if g.get("location") == "Neutral" else HOME_FIELD_ADV
        hr = state.get(g["home_team"])
        ar = state.get(g["away_team"])
        padj = 0.0
        hp, ap = g.get("home_probable_pitcher_id"), g.get("away_probable_pitcher_id")
        if hp and ap:
            gd = dt.date.fromisoformat(g["gameday"])
            hs = _snapshot_for(pitcher_cache, g["season"], gd, str(hp))
            as_ = _snapshot_for(pitcher_cache, g["season"], gd, str(ap))
            if hs and as_ and hs["ip"] >= MIN_IP and as_["ip"] >= MIN_IP:
                padj = pitcher_elo_adjustment(hs["era"], as_["era"], hs["ip"], as_["ip"])
        p_home = win_prob(hr + padj + hfa, ar, home_field_adv=0.0)

        predict_and_update(state, g)

        if g.get("home_score") is None or g.get("away_score") is None or g["home_score"] == g["away_score"]:
            continue
        home_won = 1.0 if g["home_score"] > g["away_score"] else 0.0
        month = int(g["gameday"].split("-")[1])
        is_sept_oct = month >= 9
        season_playoff_teams = set(playoff_teams.get(str(g["season"]), []))

        side_rows.append((is_sept_oct, g["home_team"] in season_playoff_teams, p_home, home_won))
        side_rows.append((is_sept_oct, g["away_team"] in season_playoff_teams, 1 - p_home, 1 - home_won))

    def stats(rows):
        if not rows:
            return None
        p = np.array([r[2] for r in rows])
        y = np.array([r[3] for r in rows])
        return brier_score(list(p), list(y)), float((y - p).mean()), len(rows)

    for label, month_filter in [("Full season", None), ("Sept/Oct only", True), ("Before Sept", False)]:
        print(f"=== {label} ===")
        for made_playoffs, tag in [(True, "playoff teams"), (False, "non-playoff teams")]:
            rows = [r for r in side_rows if r[1] == made_playoffs and (month_filter is None or r[0] == month_filter)]
            result = stats(rows)
            if result is None:
                continue
            brier, mean_resid, n = result
            print(f"  {tag}: n={n:>6}  Brier={brier:.4f}  mean(actual-predicted)={mean_resid:+.4f} "
                  f"(negative = underperforming their own Elo rating)")
        print()

    print("Real limitation, not glossed over: this uses HINDSIGHT (final season playoff outcome) to label "
          "'non-contending' -- not something knowable live in real time on August 15th. A live version would "
          "need a real-time games-back-from-a-playoff-spot calculation (standings-format-dependent, changed "
          "multiple times 2016-2025: 1 wild card/league through 2019, expanded 2020, 3 wild cards/league "
          "2022+), a bigger, format-aware build not attempted here -- this checks whether the EFFECT exists "
          "at all first, before investing in a live-servable version of it.")


if __name__ == "__main__":
    main()

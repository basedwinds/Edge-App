"""Derives + validates the NBA key-player-availability Elo penalty (task #35).

The feasibility gate (test_nba_availability_signal.py) showed a team missing a
current top-3 rotation player wins ~9-10pp less than the injury-BLIND Elo
predicts. This grid-searches the penalty (Elo points subtracted from a team's
rating per current key player out) that best corrects that on a walk-forward
Brier basis, and confirms the injury-ADJUSTED model beats the blind one
out-of-sample (train-on-early / test-on-late split, so the penalty isn't
tuned on the same games it's scored on).

'Current key players' and 'out' come from ESPN box scores exactly as the
feasibility test defined them (trailing 8-game heavy-minute regulars; out =
absent/0 min this game). Mild accepted proxy: box-score absence stands in for
the pregame injury report -- true for the vast majority of real DNPs, and
production uses the actual ESPN /injuries feed pregame.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.database import SessionLocal  # noqa: E402
from app.db.models import NbaGame  # noqa: E402
from app.models.calibration import brier_score  # noqa: E402
from app.models.baseline.elo_nba import EloState, effective_home_court_adv, update_ratings, win_prob  # noqa: E402
from scripts.test_nba_availability_signal import team_game_lists, current_key  # noqa: E402

# Derive on every season for which box scores were scraped (2024 + 2025), not
# just one -- a production constant on a single 332-game season is below this
# app's bar. build_out_counts spans all cached box games automatically.
SEASONS = {2024, 2025}

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
BOX_PATH = DATA_DIR / "nba_boxscore_probe.json"


def build_out_counts():
    """(date, home, away) -> (home_key_out_count, away_key_out_count)."""
    box = {k: v for k, v in json.loads(BOX_PATH.read_text(encoding="utf-8")).items() if v}
    per = team_game_lists(box)
    pos = {}
    for t, lst in per.items():
        for i, (d, _, _) in enumerate(lst):
            pos[(t, d)] = i

    def outs(team, date, played):
        idx = pos.get((team, date))
        if idx is None:
            return None
        ck = current_key(per[team], idx)
        if ck is None:
            return None
        return len(ck - played)

    res = {}
    for v in box.values():
        played = {t: {p for p, m in mn.items() if m > 0} for t, mn in v["minutes"].items()}
        h = outs(v["home"], v["date"], played.get(v["home"], set()))
        a = outs(v["away"], v["date"], played.get(v["away"], set()))
        if h is None or a is None:
            continue
        res[(v["date"], v["home"], v["away"])] = (h, a)
    return res


def walk(penalty, out_counts):
    """Walk-forward all history; for season-SEASON games return chronological
    (pred, actual) with the availability penalty applied to the shorthanded
    side's effective rating."""
    s = SessionLocal()
    try:
        games = [{
            "season": g.season, "gameday": g.gameday, "id": g.id,
            "home_team": g.home_team, "away_team": g.away_team,
            "home_score": g.home_score, "away_score": g.away_score,
            "location": g.location, "home_rest": g.home_rest, "away_rest": g.away_rest,
            "game_type": g.game_type,
        } for g in s.query(NbaGame).filter(NbaGame.game_type.in_(("REG", "POST", "PLAYIN"))).all()]
    finally:
        s.close()
    games.sort(key=lambda g: (g["season"], g["gameday"], g["id"]))
    state = EloState()
    preds, outs = [], []
    for g in games:
        state.start_season_if_new(g["season"])
        if g["home_score"] is None or g["away_score"] is None:
            continue
        adv = effective_home_court_adv(g["home_team"], g["location"], g["home_rest"], g["away_rest"])
        if g["season"] in SEASONS:
            oc = out_counts.get((g["gameday"], g["home_team"], g["away_team"]))
            if oc is not None:
                h_out, a_out = oc
                hr = state.get(g["home_team"]) - penalty * h_out
                ar = state.get(g["away_team"]) - penalty * a_out
                preds.append(win_prob(hr, ar, adv))
                outs.append(1.0 if g["home_score"] > g["away_score"] else 0.0)
        update_ratings(state, g["home_team"], g["away_team"], g["home_score"], g["away_score"], adv)
    return preds, outs


def main():
    oc = build_out_counts()
    n_inj = sum(1 for h, a in oc.values() if h or a)
    print(f"{len(oc)} classifiable games (seasons 2024+2025); {n_inj} with >=1 key player out")

    grid = [0, 20, 40, 60, 80, 100, 120, 150]
    print(f"\n{'penalty':>8} {'Brier(all season-2025)':>24}")
    full = {}
    for pen in grid:
        p, o = walk(pen, oc)
        full[pen] = (p, o)
        print(f"{pen:>8} {brier_score(p, o):>24.5f}")

    # out-of-sample: fit penalty on first half of season-2025 games, test on second
    p0, o0 = full[0]
    half = len(p0) // 2
    print(f"\nOut-of-sample (fit penalty on first {half} games, test on last {len(p0)-half}):")
    def split_brier(pen):
        p, o = full[pen]
        return brier_score(p[:half], o[:half]), brier_score(p[half:], o[half:])
    rows = {pen: split_brier(pen) for pen in grid}
    best = min(grid, key=lambda pen: rows[pen][0])
    print(f"  {'penalty':>8} {'train':>9} {'TEST':>9}")
    for pen in grid:
        print(f"  {pen:>8} {rows[pen][0]:>9.5f} {rows[pen][1]:>9.5f}")
    print(f"\n  best-on-train penalty: {best} Elo pts")
    print(f"  blind (pen=0) test Brier : {rows[0][1]:.5f}")
    print(f"  adjusted     test Brier  : {rows[best][1]:.5f}")
    print(f"  out-of-sample gain       : {rows[best][1]-rows[0][1]:+.5f}")


def _order(oc):
    return list(oc.keys())


if __name__ == "__main__":
    main()

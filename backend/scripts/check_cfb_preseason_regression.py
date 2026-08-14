"""Do CFB overachievers regress MORE than the preseason model expects?

THE QUESTION, and why the existing evidence cannot answer it. SEASON_REGRESSION
is 0.0 because pulling ratings toward 1500 each August made GAME prediction
worse -- measured, monotone, Brier 0.1766 at 0.0 against 0.1824 at 0.5. That
test is sound for what it measured: predicting individual games, mostly with
in-season information already baked into the ratings.

Season-long WIN-TOTAL markets are a different problem. They are priced in August
off ratings that carry last season forward untouched, and they resolve over the
whole season. If a team that beat its rating last year tends to fall back the
next, a model with no preseason regression will systematically over-project
exactly those teams -- and the within-season game test could never have seen it,
because by the time those games are predicted the ratings have already moved.

That matters right now: after fixing the truncated schedule, CFB win totals
still sit ~50pp above the market, and the teams carrying the biggest edges are
conspicuously last season's overachievers (VAN, UVA, ISU, MINN, and IU rated
2239 with a 112-point gap to second).

THE TEST. Walk the Elo forward exactly as production does. For each season S:

  1. Build ratings from every game BEFORE S, in date order (this is precisely
     the information a preseason market has).
  2. expected_wins(team, S) = sum of win_prob over that team's ACTUAL season-S
     schedule, using those frozen preseason ratings.
  3. resid(team, S) = actual_wins - expected_wins.

Then ask whether resid(S-1) predicts resid(S).

  - If the model is right, resid is unpredictable: correlation ~0 and slope ~0.
  - If overachievers regress, the correlation is NEGATIVE, and the slope is the
    size of the correction a preseason regression term should apply.

A POSITIVE slope would mean the opposite -- that outperformance persists and the
ratings are too SLOW -- which would argue against regression rather than for it.
Reporting the sign honestly matters more than finding an effect.

Deliberately uses the production Elo (elo_cfb.win_prob / update_ratings,
HOME_FIELD_ADV, K, SEASON_REGRESSION as shipped) rather than a hand-rolled twin.
A hand-rolled baseline is how a soccer regression check produced "559 changed"
against a real answer of zero.

Run: backend/.venv/Scripts/python.exe scripts/check_cfb_preseason_regression.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import collections  # noqa: E402

import numpy as np  # noqa: E402

from app.models.baseline import elo_service_cfb as service  # noqa: E402
from app.models.baseline.elo_cfb import (  # noqa: E402
    EloState, effective_home_field_adv, update_ratings, win_prob,
)

MIN_GAMES = 6  # a team needs a real season for its residual to mean anything


def _preseason_state(games: list[dict], season: int) -> EloState:
    """Ratings built from every game strictly before `season`, in date order --
    the exact information set an August market has."""
    state = EloState()
    for g in sorted((x for x in games if x["season"] < season),
                    key=lambda x: (x["gameday"], str(x["id"]))):
        if g.get("home_score") is None or g.get("away_score") is None:
            continue
        update_ratings(state, g["home_team"], g["away_team"],
                       g["home_score"], g["away_score"],
                       effective_home_field_adv(bool(g.get("neutral"))))
    return state


def _season_residuals(games: list[dict], season: int) -> dict[str, float]:
    state = _preseason_state(games, season)
    exp = collections.defaultdict(float)
    act = collections.defaultdict(float)
    n = collections.Counter()
    for g in games:
        if g["season"] != season:
            continue
        if g.get("home_score") is None or g.get("away_score") is None:
            continue
        h, a = g["home_team"], g["away_team"]
        hfa = effective_home_field_adv(bool(g.get("neutral")))
        p_home = win_prob(state.get(h), state.get(a), hfa)
        exp[h] += p_home
        exp[a] += 1.0 - p_home
        if g["home_score"] > g["away_score"]:
            act[h] += 1.0
        elif g["away_score"] > g["home_score"]:
            act[a] += 1.0
        else:
            act[h] += 0.5
            act[a] += 0.5
        n[h] += 1
        n[a] += 1
    return {t: act[t] - exp[t] for t in n if n[t] >= MIN_GAMES}


def main() -> None:
    games = [g for g in service._historical_games()
             if g.get("home_score") is not None and g.get("away_score") is not None]
    seasons = sorted({g["season"] for g in games})
    print(f"{len(games)} games, seasons {seasons[0]}-{seasons[-1]}")
    print("resid = actual wins - expected wins, expected from PRESEASON ratings only\n")

    resid: dict[int, dict[str, float]] = {}
    # The first season has no prior games to build ratings from, so it cannot
    # have a preseason residual -- start one season in.
    for s in seasons[1:]:
        resid[s] = _season_residuals(games, s)
        vals = np.array(list(resid[s].values()))
        print(f"   {s}: {len(vals):3d} teams   mean resid {vals.mean():+.3f}   sd {vals.std():.3f}")

    pairs = []
    for s in seasons[2:]:
        prev = resid.get(s - 1, {})
        for team, r in resid[s].items():
            if team in prev:
                pairs.append((prev[team], r, s, team))
    if len(pairs) < 30:
        raise SystemExit(f"\nonly {len(pairs)} consecutive-season pairs -- too few to conclude")

    x = np.array([p[0] for p in pairs])
    y = np.array([p[1] for p in pairs])
    corr = float(np.corrcoef(x, y)[0, 1])
    slope, intercept = np.polyfit(x, y, 1)
    se = 1.0 / np.sqrt(len(x))

    print(f"\nconsecutive-season pairs: {len(pairs)}")
    print(f"correlation resid(S-1) vs resid(S): {corr:+.4f}")
    print(f"   noise floor ~1/sqrt(n) = {se:.4f}  ->  {abs(corr)/se:.2f} SE from zero")
    print(f"slope: {slope:+.4f} wins of next-season residual per win of prior-season residual")

    print("\nby prior-season residual bucket (does overperformance reverse?):")
    print(f"{'prior resid':>16}{'n':>6}{'mean next resid':>18}")
    edges = [(-99, -1.5), (-1.5, -0.5), (-0.5, 0.5), (0.5, 1.5), (1.5, 99)]
    for lo, hi in edges:
        m = (x >= lo) & (x < hi)
        if m.sum():
            label = f"{lo:+.1f}..{hi:+.1f}" if abs(lo) < 90 else f"< {hi:+.1f}"
            if hi > 90:
                label = f"> {lo:+.1f}"
            print(f"{label:>16}{int(m.sum()):>6}{y[m].mean():>+18.3f}")

    print()
    if corr < -2 * se:
        print("NEGATIVE and clear of the noise floor: teams that beat their rating one")
        print("season fall back the next by more than the model expects. A preseason")
        print("regression term is warranted for SEASON-LONG markets -- and note it can")
        print("coexist with SEASON_REGRESSION=0.0 for game prediction, because these")
        print("are different information sets, not contradictory findings.")
        print(f"Indicative shrink toward the mean: {abs(slope):.3f} per residual win.")
    elif corr > 2 * se:
        print("POSITIVE: outperformance PERSISTS. That argues the ratings move too")
        print("slowly, the opposite of a regression term. Do not ship regression.")
    else:
        print("INSIDE THE NOISE FLOOR: prior-season residual does not predict the next.")
        print("The model is already unbiased on this axis, and the CFB win-total gap")
        print("must be explained somewhere else. Record and close this line.")


if __name__ == "__main__":
    main()

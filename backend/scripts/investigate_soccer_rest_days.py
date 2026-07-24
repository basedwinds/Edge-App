"""Investigates whether a fixture-congestion / rest-days signal exists in
this app's own real data -- a well-documented real soccer phenomenon (a team
playing on short rest, e.g. after a midweek European/cup fixture, tends to
underperform) that's testable with data ALREADY in the cache (match dates),
no new scraping needed, unlike injury/manager-change signals.

REAL, HONEST LIMITATION up front: this app's own historical cache is
TOP-FLIGHT LEAGUE MATCHES ONLY (football-data.co.uk doesn't carry domestic
cup or European competition fixtures) -- so "days since last match" computed
here systematically OVERESTIMATES real rest for any team that played a real
cup/European midweek fixture this app has no record of. This biases the test
toward a FALSE NEGATIVE (missing a real effect), not a false positive, so a
clean null result here is less conclusive than it would be with complete
fixture data -- reported honestly rather than treated as a clean "no
signal" the way a full-fixture-data test would be.

Methodology: walk-forward, track each team's most recent PRIOR match date
(within this cache, i.e. same league) at prediction time, compute rest-days
for both sides, bucket the SHORTER-rest side's disadvantage, and compare the
model's own pre-match residual (actual result minus model's assigned
probability for what happened) across buckets -- if short rest hurts, the
short-rest side should systematically UNDER-perform its own model_prob in
the low-rest-differential buckets."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import datetime as dt  # noqa: E402

from app.ingestion import soccer_data  # noqa: E402
from app.models.baseline.elo_soccer import SoccerRatingState, predict_and_update  # noqa: E402


def main():
    matches = soccer_data.load_matches()
    print(f"Total matches: {len(matches)}")

    states: dict[str, SoccerRatingState] = {}
    last_match_date: dict[tuple[str, str], dt.date] = {}  # (league, team) -> date

    # rows: (rest_diff_days, actual_outcome_for_home[-1/0/1 margin sign], model_p_home, model_p_draw, model_p_away)
    rows = []

    for m in matches:
        league = m["league"]
        state = states.setdefault(league, SoccerRatingState())
        home, away = m["home_team"], m["away_team"]
        match_date = dt.date.fromisoformat(m["match_date"])

        home_last = last_match_date.get((league, home))
        away_last = last_match_date.get((league, away))

        dist = predict_and_update(state, m)

        if dist is not None and home_last is not None and away_last is not None and m.get("result_ft") in ("H", "D", "A"):
            home_rest = (match_date - home_last).days
            away_rest = (match_date - away_last).days
            # Positive = home MORE rested than away (home has a rest advantage)
            rest_diff = home_rest - away_rest
            # Clip extreme values (e.g. after a long mid-season break) -- a
            # 60-day gap isn't "extra rest", it's a real season boundary,
            # not the short-rest fatigue effect this is testing for.
            if abs(rest_diff) <= 10 and home_rest <= 21 and away_rest <= 21:
                rows.append((rest_diff, m["result_ft"], dist.prob_home_win(), dist.prob_draw(), dist.prob_away_win()))

        last_match_date[(league, home)] = match_date
        last_match_date[(league, away)] = match_date

    print(f"Scored matches (both sides have a real prior match, non-season-boundary gap): {len(rows)}\n")

    # Bucket by rest_diff (home_rest - away_rest): negative = home on SHORTER rest.
    buckets = {
        "home much less rested (<=-3)": lambda d: d <= -3,
        "home slightly less rested (-2,-1)": lambda d: -3 < d <= -1,
        "equal rest (0)": lambda d: d == 0,
        "home slightly more rested (1,2)": lambda d: 1 <= d < 3,
        "home much more rested (>=3)": lambda d: d >= 3,
    }

    print(f"{'Bucket':<38}{'n':>6}{'Home win rate':>16}{'Model P(home)':>16}{'Residual':>12}")
    for label, cond in buckets.items():
        subset = [r for r in rows if cond(r[0])]
        if not subset:
            print(f"{label:<38}{'(no matches)':>6}")
            continue
        n = len(subset)
        actual_home_win_rate = sum(1 for r in subset if r[1] == "H") / n
        avg_model_p_home = sum(r[2] for r in subset) / n
        residual = actual_home_win_rate - avg_model_p_home
        print(f"{label:<38}{n:>6}{actual_home_win_rate:>16.4f}{avg_model_p_home:>16.4f}{residual:>+12.4f}")

    print("\nIf short rest hurts, the 'home much less rested' bucket should show a")
    print("NEGATIVE residual (home underperforms the model's own pregame estimate)")
    print("and 'home much more rested' should show a POSITIVE residual. A residual")
    print("comparable across all buckets (no consistent direction/size) means no")
    print("usable signal survived this test -- see module docstring's real")
    print("limitation (cup/European fixtures aren't in this app's own data,")
    print("biasing toward a false negative, not a false positive).")


if __name__ == "__main__":
    main()

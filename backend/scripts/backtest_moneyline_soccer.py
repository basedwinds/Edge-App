"""Soccer moneyline/spread/total go/no-go backtest -- same rhythm as
backtest_moneyline_tennis.py (walk-forward, predict BEFORE seeing each
match's result). Moneyline scores a 3-WAY (Home/Draw/Away) multinomial
Brier, same inline multinomial-Brier pattern as backtest_mma_method.py
(`sum((p - a) ** 2 for p, a in zip(proba, one_hot))`) since this is the
first 3-way market this app backtests against REAL market odds (MMA's 3-way
method-of-victory backtest scores against base rates, not de-vigged market
odds -- no equivalent devig_three_way existed before this script, see
calibration.py). Spread and total both score a standard 2-way Brier, same
shape as every other sport's spread/total backtest.

Spread uses football-data.co.uk's own Asian Handicap columns (AHh/AHCh +
AvgAHH/AvgAHA -- a SINGLE line per match, not a ladder like Kalshi's live
1.5/2.5 rungs, and only ~7% of matches carry it: 3,956/57,264, confirmed
live 2026-07-19 -- this field was only tracked for a subset of seasons).
Total uses the fixed Over/Under 2.5 goals columns (12,459/57,264 matches,
same coverage window as moneyline's own odds). Both were confirmed to use
the SAME sign convention as this app's own elo_soccer.py::
prob_home_spread_cover (negative line = home favored) by checking a real,
extremely lopsided match (West Ham vs Man City, home_odds=11.14, ah_line=
+1.75 -- a positive line on the massive underdog, confirming AHh's positive-
means-underdog convention lines up directly, no sign flip needed).

Reports per-LEAGUE (not pooled) -- E0/SP1/I1/D1/F1 may calibrate
differently, and MLS is excluded entirely here (ESPN has no odds, so it can
never appear in this backtest -- ships live-only, model_validated: false
permanently, see SoccerMatch's docstring in app/db/models.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import soccer_data  # noqa: E402
from app.models.baseline.elo_soccer import SoccerRatingState, predict_and_update  # noqa: E402
from app.models.calibration import brier_score, decimal_odds_to_implied_prob, devig_three_way, devig_two_way  # noqa: E402


def main():
    matches = soccer_data.load_matches()
    print(f"Total matches in merged cache: {len(matches)}")

    states: dict[str, SoccerRatingState] = {}
    rows = []  # (league, year, model_probs(H,D,A), market_probs(H,D,A), actual_idx)
    spread_rows = []  # (league, model_p, market_p, actual)
    total_rows = []  # (league, model_p, market_p, actual)

    for m in matches:
        league = m["league"]
        state = states.setdefault(league, SoccerRatingState())
        dist = predict_and_update(state, m)

        if dist is None or m.get("result_ft") not in ("H", "D", "A"):
            continue

        home_goals, away_goals = m["home_goals_ft"], m["away_goals_ft"]

        odds_h, odds_d, odds_a = m.get("home_odds"), m.get("draw_odds"), m.get("away_odds")
        if odds_h and odds_d and odds_a:
            model_probs = (dist.prob_home_win(), dist.prob_draw(), dist.prob_away_win())
            raw = (decimal_odds_to_implied_prob(odds_h), decimal_odds_to_implied_prob(odds_d), decimal_odds_to_implied_prob(odds_a))
            market_probs = devig_three_way(*raw)
            actual_idx = {"H": 0, "D": 1, "A": 2}[m["result_ft"]]
            year = m["match_date"][:4]
            rows.append((league, year, model_probs, market_probs, actual_idx))

        ah_line, ah_home_odds, ah_away_odds = m.get("ah_line"), m.get("ah_home_odds"), m.get("ah_away_odds")
        if ah_line is not None and ah_home_odds and ah_away_odds:
            model_p = dist.prob_home_spread_cover(ah_line)
            market_p, _ = devig_two_way(decimal_odds_to_implied_prob(ah_home_odds), decimal_odds_to_implied_prob(ah_away_odds))
            actual = 1.0 if (home_goals - away_goals) > -ah_line else 0.0
            spread_rows.append((league, model_p, market_p, actual))

        over_odds, under_odds = m.get("total_over_2_5_odds"), m.get("total_under_2_5_odds")
        if over_odds and under_odds:
            model_p = dist.prob_total_over(2.5)
            market_p, _ = devig_two_way(decimal_odds_to_implied_prob(over_odds), decimal_odds_to_implied_prob(under_odds))
            actual = 1.0 if (home_goals + away_goals) > 2.5 else 0.0
            total_rows.append((league, model_p, market_p, actual))

    n = len(rows)
    print(f"Scored moneyline matches (real result, has 3-way market odds): {n}\n")

    def multinomial_brier(prob_rows, actual_idxs) -> float:
        total = 0.0
        for probs, actual in zip(prob_rows, actual_idxs):
            one_hot = [1.0 if i == actual else 0.0 for i in range(3)]
            total += sum((p - a) ** 2 for p, a in zip(probs, one_hot))
        return total / len(prob_rows)

    def favorite_accuracy(prob_rows, actual_idxs) -> float:
        correct = sum(1 for probs, actual in zip(prob_rows, actual_idxs) if max(range(3), key=lambda i: probs[i]) == actual)
        return correct / len(prob_rows)

    def report(subset, label):
        if not subset:
            print(f"{label}: no scoreable matches")
            return
        model_p = [r[2] for r in subset]
        market_p = [r[3] for r in subset]
        actual = [r[4] for r in subset]
        n_sub = len(subset)
        model_b = multinomial_brier(model_p, actual)
        market_b = multinomial_brier(market_p, actual)
        model_acc = favorite_accuracy(model_p, actual)
        market_acc = favorite_accuracy(market_p, actual)
        print(f"{label} (n={n_sub}):")
        print(f"  {'Model':<28}{'Brier (3-way)':>15}{'Favorite-acc':>14}")
        print(f"  {'Attack/defense Poisson':<28}{model_b:>15.4f}{model_acc:>14.4f}")
        print(f"  {'Market (de-vigged)':<28}{market_b:>15.4f}{market_acc:>14.4f}")
        verdict = "GO" if model_b < market_b else "NO-GO"
        print(f"  {verdict}: model Brier ({model_b:.4f}) {'beats' if verdict == 'GO' else 'does NOT beat'} market Brier ({market_b:.4f})")
        print()

    report(rows, "ALL LEAGUES POOLED")
    leagues = sorted({r[0] for r in rows})
    for league in leagues:
        report([r for r in rows if r[0] == league], league)

    print("Year-by-year Brier, EPL (E0) only (deepest history):")
    years = sorted({r[1] for r in rows if r[0] == "E0"})
    for year in years:
        subset = [r for r in rows if r[0] == "E0" and r[1] == year]
        if len(subset) < 20:
            continue
        model_b = multinomial_brier([r[2] for r in subset], [r[4] for r in subset])
        market_b = multinomial_brier([r[3] for r in subset], [r[4] for r in subset])
        print(f"  {year}: model={model_b:.4f}  market={market_b:.4f}  n={len(subset)}")

    def report_2way(subset, label):
        if not subset:
            print(f"{label}: no scoreable matches")
            return
        model_p = [r[1] for r in subset]
        market_p = [r[2] for r in subset]
        actual = [r[3] for r in subset]
        n_sub = len(subset)
        model_b = brier_score(model_p, actual)
        market_b = brier_score(market_p, actual)
        print(f"{label} (n={n_sub}): model Brier {model_b:.4f}  market Brier {market_b:.4f}", end="  ")
        verdict = "GO" if model_b < market_b else "NO-GO"
        print(f"[{verdict}]")

    print("\n" + "=" * 60)
    print(f"SPREAD (Asian Handicap vs. own line, n={len(spread_rows)} -- see module docstring on coverage)")
    print("=" * 60)
    report_2way(spread_rows, "ALL LEAGUES POOLED")
    for league in sorted({r[0] for r in spread_rows}):
        report_2way([r for r in spread_rows if r[0] == league], league)

    print("\n" + "=" * 60)
    print(f"TOTAL (Over/Under 2.5 goals, n={len(total_rows)})")
    print("=" * 60)
    report_2way(total_rows, "ALL LEAGUES POOLED")
    for league in sorted({r[0] for r in total_rows}):
        report_2way([r for r in total_rows if r[0] == league], league)


if __name__ == "__main__":
    main()

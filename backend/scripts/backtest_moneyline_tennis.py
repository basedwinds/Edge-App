"""Tennis moneyline go/no-go backtest -- same rhythm as backtest_moneyline.py
(NFL)/backtest_mma_distance.py: walk-forward Elo (predicts BEFORE seeing
each match's result, using only ratings built from strictly earlier
matches) vs. the real de-vigged market price, using tennis-data.co.uk's
(tour-level) and tennisexplorer.com's (Challenger/ITF) own historical
bookmaker odds as the market baseline -- NOT ported from the user's earlier
standalone tennis-model project's own backtest, this is a fresh run against
this app's own Elo implementation and match cache.

Odds here are DECIMAL (e.g. 1.70), not American moneyline -- implied prob
is simply 1/decimal_odds, no moneyline_to_implied_prob conversion needed.

Reports per-tier (tour/challenger/itf) breakdown separately, not just
pooled -- Challenger/ITF backtestability against real historical odds is
the genuinely new capability this session's live data audit found (the
user's earlier standalone project only had this for tour level), worth
seeing on its own rather than folded into one aggregate number.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import tennis_data  # noqa: E402
from app.models.baseline.elo_tennis import TennisEloState, predict_and_update  # noqa: E402
from app.models.calibration import brier_score, log_loss, devig_two_way  # noqa: E402


def main():
    matches = tennis_data.load_matches()
    print(f"Total matches in merged cache: {len(matches)}")

    state = TennisEloState()
    rows = []  # (tier, year, model_prob_a, market_prob_a, favorite_pick_a, actual_a)

    for m in matches:
        p_a_model = predict_and_update(state, m)

        if m.get("winner_key") is None or m.get("is_retirement"):
            continue  # no result, or a real-play-but-no-clean-signal retirement -- excluded from scoring same as training
        if p_a_model is None:
            continue
        odds_a, odds_b = m.get("player_a_odds"), m.get("player_b_odds")
        if not odds_a or not odds_b or odds_a <= 1.0 or odds_b <= 1.0:
            continue  # no usable market odds for this match

        actual_a = 1.0 if m["winner_key"] == m["player_a_key"] else 0.0
        raw_a, raw_b = 1.0 / odds_a, 1.0 / odds_b
        p_a_market, _ = devig_two_way(raw_a, raw_b)
        favorite_a = 1.0 if p_a_market >= 0.5 else 0.0

        year = m["match_date"][:4]
        rows.append((m["tier"], year, p_a_model, p_a_market, favorite_a, actual_a))

    n = len(rows)
    print(f"Scored matches (real result, non-retirement, has 2-way market odds): {n}")
    print()

    def report(subset, label):
        if not subset:
            print(f"{label}: no scoreable matches")
            return
        model_p = [r[2] for r in subset]
        market_p = [r[3] for r in subset]
        fav = [r[4] for r in subset]
        actual = [r[5] for r in subset]
        n_sub = len(subset)
        model_b, market_b = brier_score(model_p, actual), brier_score(market_p, actual)
        fav_acc = sum(1 for f, o in zip(fav, actual) if f == o) / n_sub
        print(f"{label} (n={n_sub}):")
        print(f"  {'Model':<20}{'Brier':>10}{'LogLoss':>10}")
        print(f"  {'Elo baseline':<20}{model_b:>10.4f}{log_loss(model_p, actual):>10.4f}")
        print(f"  {'Market (de-vigged)':<20}{market_b:>10.4f}{log_loss(market_p, actual):>10.4f}")
        print(f"  {'Always-favorite acc':<20}{fav_acc:>10.4f}")
        verdict = "GO" if model_b < market_b else "NO-GO"
        print(f"  {verdict}: Elo Brier ({model_b:.4f}) {'beats' if verdict == 'GO' else 'does NOT beat'} market Brier ({market_b:.4f})")
        print()

    report(rows, "ALL TIERS POOLED")
    for tier in ("tour", "challenger", "itf"):
        report([r for r in rows if r[0] == tier], tier.upper())

    print("Year-by-year Brier, tour level only (deepest history):")
    years = sorted({r[1] for r in rows if r[0] == "tour"})
    for year in years:
        subset = [r for r in rows if r[0] == "tour" and r[1] == year]
        if len(subset) < 20:
            continue
        model_b = brier_score([r[2] for r in subset], [r[5] for r in subset])
        market_b = brier_score([r[3] for r in subset], [r[5] for r in subset])
        print(f"  {year}: elo={model_b:.4f}  market={market_b:.4f}  n={len(subset)}")


if __name__ == "__main__":
    main()

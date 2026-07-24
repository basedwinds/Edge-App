"""Derives real, data-fit constants for Tennis set/game markets (set winner,
game spread, game total, exact match score) -- same "regress the real
outcome against Elo diff, use the fitted slope + residual std" pattern as
game_lines.py (NFL)/game_lines_mlb.py/game_lines_nba.py, not a guessed
number. Walk-forward Elo diff (pre-match, no leakage) is the only feature;
this app's own moneyline backtest already showed Elo doesn't beat the
market, so these constants are honestly derived from a known-imperfect
rating, same category as every other "regress against Elo diff" constant
in this app.

Uses real per-set game scores (495,403 of 502,211 merged matches have
them -- tennis-data.co.uk's W1-W5/L1-L5 columns + tennisexplorer's own
score string, see tennis_data.py::_parse_tennisdata_sets/_parse_score_string).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import tennis_data  # noqa: E402
from app.models.baseline.elo_tennis import TennisEloState, predict_and_update, win_prob  # noqa: E402


def ols(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Returns (slope, intercept, residual_std)."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    slope = cov / vx if vx else 0.0
    intercept = my - slope * mx
    resid_sq = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    std = (resid_sq / (n - 2)) ** 0.5
    return slope, intercept, std


def main():
    matches = tennis_data.load_matches()
    state = TennisEloState()

    total_games_xs, total_games_ys = [], []  # elo_diff (a-b) magnitude irrelevant to sign for totals -> use |diff|; y = total games
    game_diff_xs, game_diff_ys = [], []  # x = elo_diff (a-b, signed); y = games_a - games_b
    set_win_xs, set_win_ys = [], []  # x = elo_diff; y = 1 if a won that set

    # scoreline -> count, bucketed by rounded match win-prob decile (favorite's perspective) and best_of
    from collections import Counter
    scoreline_counts: dict[tuple[int, int], Counter] = {}

    for m in matches:
        a_r = state.blended_rating(m["player_a_key"], m.get("surface"))
        b_r = state.blended_rating(m["player_b_key"], m.get("surface"))
        elo_diff = a_r - b_r
        p_a = predict_and_update(state, m)

        sets = m.get("sets") or []
        has_result = m.get("winner_key") is not None and not m.get("is_retirement") and sets and p_a is not None
        if not has_result:
            continue

        games_a = sum(s[0] for s in sets)
        games_b = sum(s[1] for s in sets)
        total_games_xs.append(abs(elo_diff))
        total_games_ys.append(games_a + games_b)
        game_diff_xs.append(elo_diff)
        game_diff_ys.append(games_a - games_b)

        for s in sets:
            if s[0] == s[1]:
                continue  # shouldn't happen in real tennis, defensive skip
            set_win_xs.append(elo_diff)
            set_win_ys.append(1.0 if s[0] > s[1] else 0.0)

        favorite_p = max(p_a, 1 - p_a)
        bucket = min(int(favorite_p * 10), 9)
        best_of = m.get("best_of") or 3
        a_sets_won = sum(1 for s in sets if s[0] > s[1])
        b_sets_won = sum(1 for s in sets if s[0] < s[1])
        favorite_won = (p_a >= 0.5 and m["winner_key"] == m["player_a_key"]) or (p_a < 0.5 and m["winner_key"] == m["player_b_key"])
        favorite_sets = a_sets_won if p_a >= 0.5 else b_sets_won
        underdog_sets = b_sets_won if p_a >= 0.5 else a_sets_won
        scoreline = (favorite_sets, underdog_sets) if favorite_won else (underdog_sets, favorite_sets)
        # store as "favorite_sets-underdog_sets" labeled scoreline, keyed by (bucket, best_of)
        scoreline_counts.setdefault((bucket, best_of), Counter())[scoreline] += 1

    print(f"Matches with real set data: {len(total_games_xs)}")
    print()

    slope_tg, intercept_tg, std_tg = ols(total_games_xs, total_games_ys)
    print(f"TOTAL GAMES vs |Elo diff|: slope={slope_tg:.6f}, intercept={intercept_tg:.4f}, resid_std={std_tg:.4f}")

    slope_gd, intercept_gd, std_gd = ols(game_diff_xs, game_diff_ys)
    print(f"GAME DIFF (a-b) vs Elo diff (a-b): slope={slope_gd:.6f}, intercept={intercept_gd:.4f}, resid_std={std_gd:.4f}")

    slope_sw, intercept_sw, _ = ols(set_win_xs, set_win_ys)
    print(f"SET WIN (linear prob approx) vs Elo diff: slope={slope_sw:.6f}, intercept={intercept_sw:.4f} (n={len(set_win_xs)})")

    # Also fit a proper logistic (matches the standard win_prob shape) via simple gradient descent -- no sklearn dependency needed for one feature.
    import math
    b0, b1 = 0.0, 1.0 / 400.0  # init near the Elo logistic's own scale
    lr = 1e-7
    n = len(set_win_xs)
    for epoch in range(200):
        grad0 = grad1 = 0.0
        for x, y in zip(set_win_xs, set_win_ys):
            p = 1.0 / (1.0 + math.exp(-(b0 + b1 * x)))
            err = p - y
            grad0 += err
            grad1 += err * x
        b0 -= lr * grad0 / n
        b1 -= lr * grad1 / n
    print(f"SET WIN (logistic): intercept={b0:.4f}, slope={b1:.6f}")

    print()
    print("EXACT SCORELINE frequencies (favorite_sets-underdog_sets), by favorite win-prob decile:")
    for (bucket, best_of), counter in sorted(scoreline_counts.items()):
        total = sum(counter.values())
        if total < 50:
            continue
        line = f"  bucket={bucket*10}-{bucket*10+10}% best_of={best_of} n={total}: "
        line += ", ".join(f"{k[0]}-{k[1]}={v/total:.3f}" for k, v in sorted(counter.items(), key=lambda kv: -kv[1]))
        print(line)


if __name__ == "__main__":
    main()

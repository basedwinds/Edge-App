"""Is the CS2 Elo GAP itself too wide -- and if so, by how much, as a function of
the gap?

WHY THIS AXIS, AND WHY IT IS NOT `measure_cs2_min_games_confidence` AGAIN. That
script bucketed on min(games_a, games_b) and UPHELD MIN_GAMES=3, finding the
overconfidence lives at HIGH counts, not low (3 games claimed .799 delivered
.784 = 1.05x; 50+ claimed .843 delivered .755 = 1.35x). It answered "does a thin
rating deserve its confidence". It did NOT ask whether a WIDE rating gap deserves
its confidence, and those are different questions that happen to correlate --
well-established teams accumulate extreme ratings precisely because they have had
many games to drift away from BASE.

THE LIVE CASE (2026-08-15, user-reported). cs2 Spirit vs BIG:

    Spirit 2093 (197 series)   BIG 1677 (165 series)   gap 416 Elo
    -> 91.6% per map -> 98.0% series. Model said 0.983.
    Market said 82.5% series -> ~73% per map -> ~173 Elo implied.

The model's gap is 2.4x the market's. Both names resolve correctly and both have
deep history, so this is neither an identity split nor thin data -- I checked
both and was wrong about the first. It is the top-end of the rating scale.

WHY IT MATTERS MORE THAN ITS FREQUENCY SUGGESTS. A single wide gap does not
produce one bad row, it produces a CORRELATED FAMILY -- series_winner, handicap,
map_winner, series_total all inherit it. The frontend then caps to one row per
game by taking the HIGHEST EDGE (markets.ts capToOneRowPerGame), which
systematically selects the largest distortion in that family. So a per-row guard
cannot see this and the display layer actively promotes it.

WHAT IS MEASURED. Identical walk-forward to
`measure_cs2_min_games_confidence` -- same production composition (Elo +
roster-K + h2h + rest), same no-lookahead replay, same orientation onto the
model's favoured side -- re-bucketed on |rating_a - rating_b| AT PREDICTION TIME.

BOTH SIDES ARE GATED TO >= MIN_GAMES so this cannot be re-measuring the thin-
rating question under a new name. Any gradient found here is about the SCALE, on
matches the app would actually price.

Reports a Wilson CI per bucket. A bucket whose CI covers its own claim is not
evidence, however suggestive the point estimate looks -- the same bar
`calibration_report.py` applies.

Run: backend/.venv/Scripts/python.exe scripts/measure_cs2_elo_gap_calibration.py
"""
from __future__ import annotations

import datetime as dt
import sys
from math import sqrt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.calibration import brier_score  # noqa: E402
from app.models.baseline.elo_cs2 import (  # noqa: E402
    BASE_RATING, K, RATING_CLAMP, ROSTER_BOOST_MULTIPLIER, ROSTER_BOOST_GAMES,
    implied_elo_diff, map_win_prob,
)
from app.models.baseline.elo_service_cs2 import (  # noqa: E402
    H2H_PRIOR_WEIGHT, REST_POINTS_PER_DAY, REST_CAP_DAYS, MIN_GAMES,
)
from scripts.backtest_cs2_market_odds_fullmodel import (  # noqa: E402
    load_historical, load_transfers_by_team, resolve_transfer_date,
    prob_series_win_a_from_map_p, map_p_for_series_prob,
)

# Elo-gap buckets. The top bucket is split at 300/400 because the reported case
# sits at 416 and the question is specifically whether the far tail misbehaves --
# folding 300+ into one bucket would average the tail away, which is the exact
# mistake the plate-racing calibration work already made once.
BUCKETS = [(0, 49), (50, 99), (100, 149), (150, 199), (200, 299), (300, 399), (400, 10**9)]


def wilson(k: int, n: int) -> tuple[float, float]:
    if not n:
        return (0.0, 1.0)
    p, z = k / n, 1.96
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def bucket_for(gap: float):
    for lo, hi in BUCKETS:
        if lo <= gap <= hi:
            return (lo, hi)
    return None


def label(b) -> str:
    lo, hi = b
    return f"{lo}+" if hi >= 10**9 else f"{lo}-{hi}"


def replay():
    """Walk-forward once; yield (elo_gap, model_p_favoured, favoured_won, date)."""
    historical = load_historical()
    transfers_by_team = load_transfers_by_team()
    ratings, games, h2h = {}, {}, {}
    last_transfer_date, games_since_roster = {}, {}
    last_played = {}
    out = []

    for m in historical:
        team_a, team_b, best_of, winner = m["team_a"], m["team_b"], m["best_of"], m["winner"]
        match_date = m.get("match_date")
        a_r, b_r = ratings.get(team_a, BASE_RATING), ratings.get(team_b, BASE_RATING)
        g_a, g_b = games.get(team_a, 0), games.get(team_b, 0)

        # BOTH sides gated: this is a question about the scale, asked only of
        # matches the app would actually price.
        if min(g_a, g_b) >= MIN_GAMES:
            map_p = map_win_prob(a_r, b_r)
            key = tuple(sorted((team_a, team_b)))
            wins_first, total = h2h.get(key, (0, 0))
            if total > 0:
                wins_a = wins_first if team_a == key[0] else (total - wins_first)
                elo_series = prob_series_win_a_from_map_p(map_p, best_of)
                blended = (elo_series * H2H_PRIOR_WEIGHT + wins_a) / (H2H_PRIOR_WEIGHT + total)
                map_p = map_p_for_series_prob(blended, best_of)

            def rest_bonus(team):
                last = last_played.get(team)
                if last is None or not match_date:
                    return 0.0
                rd = (dt.date.fromisoformat(match_date[:10]) - dt.date.fromisoformat(last[:10])).days
                return REST_POINTS_PER_DAY * min(max(rd, 0), REST_CAP_DAYS)

            ba, bb = rest_bonus(team_a), rest_bonus(team_b)
            if ba != bb:
                map_p = map_win_prob(implied_elo_diff(map_p) + (ba - bb), 0.0)
            model_p_a = prob_series_win_a_from_map_p(map_p, best_of)
            won_a = 1.0 if winner == "team_a" else 0.0
            gap = abs(a_r - b_r)
            if model_p_a >= 0.5:
                out.append((gap, model_p_a, won_a, match_date, a_r - b_r, best_of))
            else:
                out.append((gap, 1.0 - model_p_a, 1.0 - won_a, match_date, a_r - b_r, best_of))

        # ---- update (unchanged production order) ----
        td_a = resolve_transfer_date(team_a, match_date, transfers_by_team)
        td_b = resolve_transfer_date(team_b, match_date, transfers_by_team)
        for team, td in ((team_a, td_a), (team_b, td_b)):
            if td is not None and last_transfer_date.get(team) != td:
                games_since_roster[team] = 0
                last_transfer_date[team] = td

        def eff_k(team):
            return K * ROSTER_BOOST_MULTIPLIER if games_since_roster.get(team, ROSTER_BOOST_GAMES) < ROSTER_BOOST_GAMES else K

        p_a = map_win_prob(a_r, b_r)
        actual_a = 1.0 if winner == "team_a" else 0.0
        k_a, k_b = eff_k(team_a), eff_k(team_b)
        ratings[team_a] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, a_r + k_a * (actual_a - p_a)))
        ratings[team_b] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, b_r - k_b * (actual_a - p_a)))
        games[team_a] = g_a + 1
        games[team_b] = g_b + 1
        if team_a in games_since_roster:
            games_since_roster[team_a] += 1
        if team_b in games_since_roster:
            games_since_roster[team_b] += 1
        key = tuple(sorted((team_a, team_b)))
        wf, tot = h2h.get(key, (0, 0))
        first_won = (winner == "team_a") if team_a == key[0] else (winner == "team_b")
        h2h[key] = (wf + (1 if first_won else 0), tot + 1)
        if match_date:
            last_played[team_a] = match_date
            last_played[team_b] = match_date
    return out


def main() -> None:
    rows = replay()
    print(f"{len(rows)} predictions where BOTH sides clear MIN_GAMES={MIN_GAMES}")
    print("(gated so this cannot be the thin-rating question under a new name)")

    print(f"\nCALIBRATION BY ELO GAP AT PREDICTION TIME")
    print("  A bucket is EVIDENCE only if the 95% CI on `actual` EXCLUDES `claimed`.")
    print(f"\n{'gap':>9s} {'n':>7s} {'claimed':>9s} {'actual':>9s} {'miss':>8s} "
          f"{'95% CI':>17s} {'overstate':>10s}  sig")
    for b in BUCKETS:
        r = [(p, o) for g, p, o, *_ in rows if bucket_for(g) == b]
        if len(r) < 25:
            continue
        preds = [p for p, _ in r]
        outs = [o for _, o in r]
        claimed = sum(preds) / len(preds)
        wins = int(sum(outs))
        actual = wins / len(r)
        lo, hi = wilson(wins, len(r))
        c_edge, a_edge = claimed - 0.5, actual - 0.5
        ratio = (c_edge / a_edge) if a_edge > 0.01 else float("inf")
        rs = f"{ratio:9.2f}x" if ratio != float("inf") else "      inf"
        sig = "YES" if not (lo <= claimed <= hi) else "-"
        print(f"{label(b):>9s} {len(r):7d} {claimed:9.4f} {actual:9.4f} {actual-claimed:+8.4f} "
              f"[{lo:.3f},{hi:.3f}] {rs:>10s}  {sig}")

    print(f"\nOverall Brier (gated set): {brier_score([p for _, p, _, *_ in rows], [o for _, _, o, *_ in rows]):.5f}")

    # How much of the board is actually in the wide tail? A real but rare defect
    # is a different priority from a real and common one.
    wide = [r for r in rows if r[0] >= 300]
    print(f"Share of gated predictions at gap >= 300: {len(wide)}/{len(rows)} "
          f"({100*len(wide)/max(len(rows),1):.1f}%)")


if __name__ == "__main__":
    main()

"""Does a CS2 rating built on barely MIN_GAMES series deserve the CONFIDENCE the
model puts on it?

WHY THIS IS NOT ALREADY ANSWERED. MIN_GAMES=3 was fitted (2026-07-20, see
elo_service_cs2.get_series_distribution's docstring) on AVERAGE BRIER over all
post-warmup predictions: 0.23368 at >=0 games, 0.23089 at >=3. That is the right
measurement for "should this match be priced at all" and it is not the question
staking asks. Average Brier is dominated by the ordinary matches; it can improve
while a thin-rating tail quietly produces enormous, wrong edges -- and it is the
EDGE, not the Brier, that decides whether real money is allocated.

The live case that prompted this (2026-08-11, user-reported): Luminosity vs
GamerLegion, 2026-08-12. Luminosity had exactly 3 settled series -- precisely the
threshold -- and a rating of 1456, i.e. the 1500 default barely moved.
GamerLegion had 201 series at 1739. The model returned 83.6% for GamerLegion
against a market at 48.5%: a +35pp edge, and a staked bet. A +35pp edge against a
liquid book is not a finding, it is a smell, and its entire source is that one
side has no history.

WHAT THIS MEASURES. The same walk-forward as
backtest_cs2_market_odds_fullmodel.py -- identical production composition (Elo +
roster-K + h2h + rest), strictly no lookahead -- but over EVERY historical match
rather than only the 85 with a cached Kalshi price, because bucketing 85 events
by games-played would be far too thin to read. For each prediction we record
min(games_a, games_b): the thinner of the two ratings is what limits the pair,
which is exactly what MIN_GAMES gates on.

Then, per bucket, the number that matters: within the CONFIDENT band (model
>= 0.75 on one side), what does the model claim on average and what actually
happened? If a thin bucket claims 0.85 and delivers 0.60, the confidence is
manufactured from the absence of data, and MIN_GAMES is set too low for staking
even if it is set correctly for pricing.

This is the same shape as the price-band overstatement gradient already measured
across the app, asked of a different axis.

Run: backend/.venv/Scripts/python.exe scripts/measure_cs2_min_games_confidence.py
"""
from __future__ import annotations

import datetime as dt
import sys
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

# Buckets on min(games_a, games_b). 3 and 4 are deliberately their OWN buckets:
# the whole question is what happens at and just above the gate, and folding
# them into a "3-5" bucket would hide it.
BUCKETS = [(3, 3), (4, 4), (5, 6), (7, 9), (10, 19), (20, 49), (50, 10**9)]
CONFIDENT = 0.75


def bucket_for(g: int):
    for lo, hi in BUCKETS:
        if lo <= g <= hi:
            return (lo, hi)
    return None


def label(b) -> str:
    lo, hi = b
    if lo == hi:
        return f"{lo}"
    return f"{lo}+" if hi >= 10**9 else f"{lo}-{hi}"


def main() -> None:
    historical = load_historical()
    transfers_by_team = load_transfers_by_team()
    print(f"replaying {len(historical)} real settled CS2 series (walk-forward, no lookahead)")

    ratings, games, h2h = {}, {}, {}
    last_transfer_date, games_since_roster = {}, {}
    last_played = {}
    # bucket -> list of (model_prob_on_the_side_the_model_favours, did_that_side_win)
    rows: dict[tuple, list[tuple[float, float]]] = {b: [] for b in BUCKETS}

    for m in historical:
        team_a, team_b, best_of, winner = m["team_a"], m["team_b"], m["best_of"], m["winner"]
        match_date = m.get("match_date")
        a_r, b_r = ratings.get(team_a, BASE_RATING), ratings.get(team_b, BASE_RATING)
        g_a, g_b = games.get(team_a, 0), games.get(team_b, 0)

        b = bucket_for(min(g_a, g_b))
        if b is not None:
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
            # Orient onto the side the model FAVOURS, so "claimed vs actual" reads
            # directly as "when the model says it is this sure, how often is it
            # right" regardless of which team happened to be listed first.
            if model_p_a >= 0.5:
                rows[b].append((model_p_a, won_a))
            else:
                rows[b].append((1.0 - model_p_a, 1.0 - won_a))

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

    print(f"\nALL predictions, by min(games_a, games_b)  [MIN_GAMES gate is {MIN_GAMES}]")
    print(f"{'bucket':>8s} {'n':>7s} {'Brier':>9s} {'claimed':>9s} {'actual':>9s} {'gap':>8s}")
    for b in BUCKETS:
        r = rows[b]
        if not r:
            continue
        preds = [p for p, _ in r]
        outs = [o for _, o in r]
        claimed = sum(preds) / len(preds)
        actual = sum(outs) / len(outs)
        print(f"{label(b):>8s} {len(r):7d} {brier_score(preds, outs):9.5f} "
              f"{claimed:9.4f} {actual:9.4f} {actual - claimed:+8.4f}")

    print(f"\nCONFIDENT band only (model >= {CONFIDENT:.2f} on its favoured side) -- this is the")
    print("band that generates big edges and therefore stakes.")
    print(f"{'bucket':>8s} {'n':>7s} {'claimed':>9s} {'actual':>9s} {'gap':>8s} {'overstate':>10s}")
    for b in BUCKETS:
        r = [(p, o) for p, o in rows[b] if p >= CONFIDENT]
        if not r:
            continue
        preds = [p for p, _ in r]
        outs = [o for _, o in r]
        claimed = sum(preds) / len(preds)
        actual = sum(outs) / len(outs)
        # How many times the claimed EDGE OVER A COINFLIP the model actually
        # delivered. Stated as a ratio because that is how the app's existing
        # price-band overstatement work reports the same failure.
        c_edge, a_edge = claimed - 0.5, actual - 0.5
        ratio = (c_edge / a_edge) if a_edge > 0.01 else float("inf")
        rs = f"{ratio:9.2f}x" if ratio != float("inf") else "      inf"
        print(f"{label(b):>8s} {len(r):7d} {claimed:9.4f} {actual:9.4f} {actual - claimed:+8.4f} {rs:>10s}")


if __name__ == "__main__":
    main()

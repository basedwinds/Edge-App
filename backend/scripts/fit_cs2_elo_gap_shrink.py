"""Fit a shrink on the CS2 Elo DIFFERENCE, and test it out of sample.

THE DEFECT (measure_cs2_elo_gap_calibration.py, 2026-08-15). On 6,519 gated
walk-forward predictions the model is overconfident at every gap above 50 Elo,
and the miss widens with the gap:

      gap        n   claimed   actual     miss   sig
     0-49    2610    0.5634   0.5598  -0.0037    -      <- already correct
    50-99    1827    0.6404   0.5950  -0.0454   YES
  100-149    1019    0.7240   0.6909  -0.0331   YES
  150-199     493    0.7863   0.7323  -0.0541   YES
  200-299     370    0.8615   0.7811  -0.0804   YES
  300-399      84    0.9281   0.8452  -0.0828   YES

60% of gated predictions are above 50 Elo, so this is not a tail curiosity.

WHY SHRINK THE DIFFERENCE AND NOT THE PROBABILITY. The 0-49 bucket is already
well calibrated and must not be disturbed. A global temperature on the
probability moves every prediction including that one -- which is exactly why
the tennis temperature was REJECTED (#192): T=1.53 improved Brier but made ECE
worse, because it wrecked an excellent middle to repair thin tails. Scaling the
Elo DIFFERENCE is proportional by construction: a 20-point gap shrinks by 4
points at lam=0.8 and moves the probability almost not at all, while a 416-point
gap shrinks by 83 and moves a lot. The correction is concentrated exactly where
the error is.

APPLIED AT PREDICTION TIME, NOT IN THE UPDATE. Shrinking K instead would change
the rating dynamics and therefore every rating's meaning; the ratings are fine as
an ordering, it is the mapping from gap to probability that is too steep. This
keeps the update loop untouched.

THE BAR, from calibration_temp.py: a fit ships only if it improves BOTH ECE and
Brier OUT OF SAMPLE. Train on the earlier 70% of matches by date, test on the
most recent 30%, and never look at test while choosing lam.

Run: backend/.venv/Scripts/python.exe scripts/fit_cs2_elo_gap_shrink.py
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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

LAMBDAS = [round(0.50 + 0.02 * i, 2) for i in range(26)]   # 0.50 .. 1.00


def brier(preds, outs):
    return sum((p - o) ** 2 for p, o in zip(preds, outs)) / len(preds)


def logloss(preds, outs):
    from math import log
    eps = 1e-12
    return -sum(o * log(max(p, eps)) + (1 - o) * log(max(1 - p, eps))
                for p, o in zip(preds, outs)) / len(preds)


def ece(preds, outs, bins=10):
    """Expected calibration error -- mean |claimed - actual| weighted by bin size."""
    buckets = [[] for _ in range(bins)]
    for p, o in zip(preds, outs):
        buckets[min(int(p * bins), bins - 1)].append((p, o))
    n = len(preds)
    tot = 0.0
    for b in buckets:
        if not b:
            continue
        c = sum(p for p, _ in b) / len(b)
        a = sum(o for _, o in b) / len(b)
        tot += (len(b) / n) * abs(c - a)
    return tot


def replay(lam: float):
    """Walk-forward with the Elo difference scaled by `lam` AT PREDICTION TIME.
    The update loop is untouched, so ratings are identical for every lam."""
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

        if min(g_a, g_b) >= MIN_GAMES:
            # THE INTERVENTION, and the only line that differs from production.
            map_p = map_win_prob((a_r - b_r) * lam, 0.0)
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
            out.append((match_date or "", model_p_a, won_a, abs(a_r - b_r)))

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


def split(rows):
    dated = sorted([r for r in rows if r[0]], key=lambda r: r[0])
    cut = int(len(dated) * 0.70)
    return dated[:cut], dated[cut:]


def score(rows):
    preds = [p for _, p, _, _ in rows]
    outs = [o for _, _, o, _ in rows]
    return brier(preds, outs), ece(preds, outs), logloss(preds, outs)


def main() -> None:
    print("Fitting a shrink on the CS2 Elo DIFFERENCE (prediction time only).")
    print("Ratings are IDENTICAL across lambdas -- the update loop is untouched.\n")

    base_rows = replay(1.0)
    tr, te = split(base_rows)
    print(f"train {len(tr)} matches  {tr[0][0][:10]} -> {tr[-1][0][:10]}")
    print(f"test  {len(te)} matches  {te[0][0][:10]} -> {te[-1][0][:10]}")
    b0, e0, l0 = score(tr)
    B0, E0, L0 = score(te)
    print(f"\nbaseline lam=1.00   TRAIN brier {b0:.5f} ece {e0:.5f}   "
          f"TEST brier {B0:.5f} ece {E0:.5f} logloss {L0:.5f}")

    print(f"\n{'lam':>6s} {'TRAIN brier':>12s} {'TRAIN ece':>10s}")
    best, best_b = None, None
    cache = {}
    for lam in LAMBDAS:
        rows = replay(lam)
        cache[lam] = rows
        a, _ = split(rows)
        b, e, _ = score(a)
        print(f"{lam:6.2f} {b:12.5f} {e:10.5f}")
        if best_b is None or b < best_b:
            best, best_b = lam, b

    print(f"\nchosen on TRAIN brier only: lam = {best:.2f}")

    _, te2 = split(cache[best])
    B1, E1, L1 = score(te2)
    print(f"\nOUT OF SAMPLE (test set, never used to choose lam):")
    print(f"  brier    {B0:.5f} -> {B1:.5f}   {'BETTER' if B1 < B0 else 'WORSE'}")
    print(f"  ece      {E0:.5f} -> {E1:.5f}   {'BETTER' if E1 < E0 else 'WORSE'}")
    print(f"  logloss  {L0:.5f} -> {L1:.5f}   {'BETTER' if L1 < L0 else 'WORSE'}")
    ships = (B1 < B0) and (E1 < E0)
    print(f"\n  VERDICT: {'SHIP -- improves BOTH ECE and Brier out of sample' if ships else 'DO NOT SHIP -- the module rule needs BOTH'}")

    # What it does to the buckets that were broken, on TEST only.
    print(f"\nTEST-set calibration by gap, before vs after (lam={best:.2f}):")
    print(f"{'gap':>9s} {'n':>6s} {'claimed':>9s} {'actual':>9s} {'miss':>8s} | "
          f"{'claimed':>9s} {'miss':>8s}")
    BUCK = [(0, 49), (50, 99), (100, 149), (150, 199), (200, 299), (300, 10**9)]
    for lo, hi in BUCK:
        pre = [(p, o) for _, p, o, g in te if lo <= g <= hi]
        post = [(p, o) for _, p, o, g in te2 if lo <= g <= hi]
        if len(pre) < 20:
            continue
        def orient(rs):
            return [(p if p >= 0.5 else 1 - p, o if p >= 0.5 else 1 - o) for p, o in rs]
        pre_o, post_o = orient(pre), orient(post)
        c0 = sum(p for p, _ in pre_o) / len(pre_o); a0 = sum(o for _, o in pre_o) / len(pre_o)
        c1 = sum(p for p, _ in post_o) / len(post_o); a1 = sum(o for _, o in post_o) / len(post_o)
        lab = f"{lo}+" if hi >= 10**9 else f"{lo}-{hi}"
        print(f"{lab:>9s} {len(pre_o):6d} {c0:9.4f} {a0:9.4f} {a0-c0:+8.4f} | "
              f"{c1:9.4f} {a1-c1:+8.4f}")


if __name__ == "__main__":
    main()

"""Is a PROMOTED club's second-tier rating good enough to price its first
top-flight season, instead of abstaining?

WHY THIS IS NOT ALREADY ANSWERED. The bridge itself exists and is fitted --
season_sim_soccer.PROMOTED_TEAM_*_LOG_DISCOUNT, from 476 real promotion events
-- and check_cup_tier_bridge.py showed it transfers to CUP ties. But it is
wired into the SEASON SIM and the CUP model only. The per-match league path
(elo_service_soccer.get_match_distribution) still gates on a per-league match
COUNT, so a promoted club reads as unrated and its whole first season goes
unpriced. That is why Elversberg v Leverkusen showed 0 of 64 rows priced.

THREE THINGS THIS MEASURES, because "better than nothing" is not the bar:

1. ACCURACY against the alternatives, on the promoted club's OWN first-season
   matches: the bridge, the bottom-quartile placeholder the sim used before it,
   a flat league-average rating, and an ORACLE arm (the club's real
   end-of-season rating) that bounds what ANY prior could buy.

2. BIAS, which is the reason to be suspicious. cup_match.py's own docstring
   records that this constant probably OVERRATES the weaker side, and that
   because this app stakes where model > market the residual points at buying
   the underdog. A promoted club IS the weaker side in almost every fixture, so
   that bias lands squarely on this population. Measured as predicted-minus-
   actual win rate for the promoted club.

3. WHETHER IT MANUFACTURES EDGE. football-data.co.uk ships closing 1X2 odds, so
   the honest question is not Brier at all: it is how often each arm would clear
   this app's 20pp edge gate against the market, which side those picks land on,
   and how they actually did. An arm that is slightly better on Brier but
   invents hundreds of fake edges is worse than abstaining.

HELD OUT. The discount was fitted on these same promotion events, so scoring it
in-sample would be circular. It is REFIT leave-one-season-out: for each test
season the constant is averaged over promotion events from all OTHER seasons.

model_validated stays False either way -- this decides whether to PRICE, not
whether the number is trustworthy enough to stake.
"""
from __future__ import annotations

import collections
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.football_data_client import PROMOTION_SOURCE_DIVISION  # noqa: E402
from app.ingestion import soccer_data  # noqa: E402
from app.models.baseline.elo_soccer import (  # noqa: E402
    SoccerRatingState, predict_and_update, predict_match,
)

# How far into the promoted club's first season to keep treating it as unrated.
# The live gate abstains until the club has ANY top-flight history, so match 1
# is the purest case; the later horizons show how fast the question stops
# mattering on its own.
HORIZONS = (1, 5, 10, 19)
EDGE_GATE = 0.20            # the live staking gate
MIN_OPPONENT_MATCHES = 10   # the opponent must itself be properly rated


def devig(h, d, a):
    """Closing 1X2 odds -> probabilities, overround removed proportionally."""
    try:
        ih, idr, ia = 1.0 / float(h), 1.0 / float(d), 1.0 / float(a)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    s = ih + idr + ia
    if not (1.0 < s < 1.5):
        return None
    return ih / s, idr / s, ia / s


def brier3(p, outcome):
    """3-way Brier: summed squared error over (H, D, A)."""
    return sum((p[i] - (1.0 if outcome == i else 0.0)) ** 2 for i in range(3))


def clone(st: SoccerRatingState) -> SoccerRatingState:
    return SoccerRatingState(
        attack_log=dict(st.attack_log), concede_log=dict(st.concede_log),
        match_counts=dict(st.match_counts), goals_sum=st.goals_sum,
        goals_n=st.goals_n, current_season=st.current_season, home_log=st.home_log,
    )


def main() -> None:
    matches = [m for m in soccer_data.load_football_data_matches()
               if m.get("match_date") and m.get("season")]
    matches.sort(key=lambda m: (m["match_date"], str(m.get("source_match_id") or "")))
    print(f"{len(matches)} dated football-data matches\n")

    # ---- pass 1: end-of-season ratings -> promotion events + the discount ----
    states: dict[str, SoccerRatingState] = {}
    eos: dict[tuple, tuple] = {}
    for m in matches:
        st = states.setdefault(m["league"], SoccerRatingState())
        predict_and_update(st, m)
        for t in (m["home_team"], m["away_team"]):
            eos[(m["league"], t, m["season"])] = (st.get_attack(t), st.get_concede(t))

    events = []
    for top_div, second_div in PROMOTION_SOURCE_DIVISION.items():
        for (lg, team, season) in list(eos):
            if lg != second_div:
                continue
            y = int(season.split("-")[0])
            nxt = f"{y + 1}-{y + 2}"
            if (top_div, team, nxt) in eos:
                events.append((top_div, second_div, team, season, nxt))
    print(f"{len(events)} promotion events across {len(PROMOTION_SOURCE_DIVISION)} tier pairs")

    gaps = []   # (season_entered, attack_gap, concede_gap)
    for top_div, second_div, team, season, nxt in events:
        sa, sc = eos[(second_div, team, season)]
        ta, tc = eos[(top_div, team, nxt)]
        gaps.append((nxt, ta - sa, tc - sc))
    print(f"pooled refit here : attack {statistics.fmean(g[1] for g in gaps):+.4f}  "
          f"concede {statistics.fmean(g[2] for g in gaps):+.4f}")
    print("shipped constants : attack -0.2558  concede +0.2444\n")

    def loso(test_season):
        sub = [(a, c) for (s, a, c) in gaps if s != test_season]
        if not sub:
            return None
        return statistics.fmean(x[0] for x in sub), statistics.fmean(x[1] for x in sub)

    promoted = {(td, tm, nx): (sd, se) for td, sd, tm, se, nx in events}

    # ---- pass 2: walk again, scoring each promoted club's first matches ----
    states = {}
    played: collections.Counter = collections.Counter()
    rows = []
    for m in matches:
        lg, season = m["league"], m["season"]
        st = states.setdefault(lg, SoccerRatingState())
        h, a = m["home_team"], m["away_team"]
        outcome = {"H": 0, "D": 1, "A": 2}.get(m.get("result_ft"))

        for team, is_home in ((h, True), (a, False)):
            if outcome is None or (lg, team, season) not in promoted:
                continue
            if played[(lg, team, season)] >= max(HORIZONS):
                continue
            opp = a if is_home else h
            if st.get_count(opp) < MIN_OPPONENT_MATCHES:
                continue
            d = loso(season)
            if d is None:
                continue
            dA, dC = d
            second_div, prev_season = promoted[(lg, team, season)]
            sa, sc = eos[(second_div, team, prev_season)]

            rated = [t for t in st.attack_log
                     if st.get_count(t) >= MIN_OPPONENT_MATCHES and t != team]
            if len(rated) < 8:
                continue
            ranked = sorted(rated, key=lambda t: st.get_attack(t) - st.get_concede(t))
            bq = ranked[: max(1, len(ranked) // 4)]

            arms = {
                "bridge":      (sa + dA, sc + dC),
                "placeholder": (statistics.fmean(st.get_attack(t) for t in bq),
                                statistics.fmean(st.get_concede(t) for t in bq)),
                "league_avg":  (0.0, 0.0),
                "oracle":      eos[(lg, team, season)],
            }
            probs = {}
            for name, (at, cn) in arms.items():
                tmp = clone(st)
                tmp.attack_log[team] = at
                tmp.concede_log[team] = cn
                tmp.match_counts[team] = max(tmp.match_counts.get(team, 0), MIN_OPPONENT_MATCHES)
                dist = predict_match(tmp, h, a)
                probs[name] = (dist.prob_home_win(), dist.prob_draw(), dist.prob_away_win())

            rows.append({
                "league": lg, "season": season, "team": team,
                "n_before": played[(lg, team, season)], "is_home": is_home,
                "outcome": outcome, "probs": probs,
                "market": devig(m.get("home_odds"), m.get("draw_odds"), m.get("away_odds")),
            })

        played[(lg, h, season)] += 1
        played[(lg, a, season)] += 1
        predict_and_update(st, m)

    club_seasons = {(r["league"], r["season"], r["team"]) for r in rows}
    print(f"{len(rows)} scored promoted-club matches over {len(club_seasons)} club-seasons\n")
    if not rows:
        return

    ARMS = ["bridge", "placeholder", "league_avg", "oracle"]
    for hz in HORIZONS:
        sub = [r for r in rows if r["n_before"] < hz]
        if not sub:
            continue
        print(f"=== first {hz:2d} match(es) of the promoted club's season   n={len(sub)} ===")
        print(f"   {'arm':12s} {'Brier':>8s} {'logloss':>9s} {'pred win':>9s} {'actual':>8s} {'bias':>9s}")
        for arm in ARMS:
            b = statistics.fmean(brier3(r["probs"][arm], r["outcome"]) for r in sub)
            ll = statistics.fmean(-math.log(max(r["probs"][arm][r["outcome"]], 1e-9)) for r in sub)
            pw = statistics.fmean(r["probs"][arm][0] if r["is_home"] else r["probs"][arm][2] for r in sub)
            aw = statistics.fmean(
                1.0 if ((r["is_home"] and r["outcome"] == 0)
                        or (not r["is_home"] and r["outcome"] == 2)) else 0.0
                for r in sub)
            print(f"   {arm:12s} {b:8.4f} {ll:9.4f} {100 * pw:8.1f}% {100 * aw:7.1f}% {100 * (pw - aw):+8.1f}pp")
        wm = [r for r in sub if r["market"]]
        if wm:
            print(f"   {'MARKET':12s} {statistics.fmean(brier3(r['market'], r['outcome']) for r in wm):8.4f}"
                  f"            (n={len(wm)} with closing odds)")
        print()

    print("=== EDGE TEST vs closing odds -- would these arms clear the 20pp gate? ===")
    print(f"   {'arm':12s} {'picks':>6s} {'on promoted':>12s} {'win%':>7s} {'ROI':>8s}")
    for arm in ARMS:
        picks = []
        for r in rows:
            if not r["market"]:
                continue
            for side in range(3):
                if r["probs"][arm][side] - r["market"][side] >= EDGE_GATE:
                    on_promoted = (side == 0 and r["is_home"]) or (side == 2 and not r["is_home"])
                    picks.append((r, side, on_promoted))
        if not picks:
            print(f"   {arm:12s} {0:6d}")
            continue
        won = sum(1 for r, s, _ in picks if r["outcome"] == s)
        onp = sum(1 for _, _, p in picks if p)
        roi = statistics.fmean((1.0 / r["market"][s] - 1.0) if r["outcome"] == s else -1.0
                               for r, s, _ in picks)
        print(f"   {arm:12s} {len(picks):6d} {100 * onp / len(picks):11.0f}% "
              f"{100 * won / len(picks):6.1f}% {100 * roi:+7.1f}%")
    print("\n'on promoted' = share of an arm's picks that BACK the promoted club.")
    print("A high number there is the overrating bias cup_match.py warns about,")
    print("landing exactly where this app would stake it.")
    print("ROI is priced at the devigged fair line, so it is optimistic by the")
    print("bookmaker's margin -- a negative number here is decisive, a small")
    print("positive one is not.")


if __name__ == "__main__":
    main()

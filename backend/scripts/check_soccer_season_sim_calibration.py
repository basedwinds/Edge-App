"""Is the soccer season simulator calibrated on the FUTURES it actually prices?

WHY THIS EXISTS. ~118 undecided catalog markets across 23 leagues are gated on
this one question -- "Team with Most Clean Sheets" (23), "Nth Place / Relegation
Survivor" (23), "3rd Place Finish" (23), "Teams relegated" (23), UEFA
qualification (21), "Last Place Finisher" (5). Every one of them is priced from
season_sim_soccer's position_dist / most_clean_sheets_prob / relegation_prob, and
NONE of those outputs has ever been scored against a finished season.

The app already half-knows this. soccer_markets.MID_SEASON_SIM_NOTE says of the
part-played path, verbatim: "That path is new and has never been checked against
a finished season ... expect it to overstate the leader". integrity_checks.py
excludes relegation_prob outright because the module documents it as
miscalibrated above 30%. This script is what turns those hedges into numbers.

Same shape as check_mlb_season_sim_calibration.py, and the same worry: a model
fitted and validated on one question (match goals) then asked several others
nobody checked. That is exactly how racing top_n turned out 20-30pp wrong.

WHAT IS MEASURED. Five questions, each a REAL priced market, per team per season:

    champion          P(finish 1st)              -- "<League> Winner"
    top_four          P(finish top 4)            -- UEFA qualification markets
    exact_third       P(finish exactly 3rd)      -- "3rd Place Finish"
    relegated         P(finish in the drop zone) -- "Teams relegated"
    most_clean_sheets P(lead the league on CS)   -- "Team with Most Clean Sheets"

exact_third is included deliberately even though it is a thin question: an
exact-position market is the hardest thing a position histogram can be asked, so
it is where a mis-shaped distribution shows first.

AT FOUR POINTS IN THE SEASON. 0% (preseason), 25%, 50%, 75% of matches played.
This is the part the MLB script has no equivalent of, and it is the decision that
matters: these markets are listed all season, so "is it calibrated" may well have
different answers early and late. If preseason is bad and 50% is fine, the action
is a time gate, not a shelved feature.

NO LEAKAGE. For a cutoff of F, the rating state is built from that league's
matches STRICTLY BEFORE the cutoff -- all prior seasons plus the first F of this
one -- and the simulation resamples only the fixtures not yet played. The
remainder of the season contributes nothing to the ratings that price it. Ratings
are built with predict_and_update and the production xG blend, so this scores the
shipped model rather than a stand-in.

SECOND TIER INCLUDED. Production hands simulate_season the league's own
promotion-source state (PROMOTION_SOURCE_DIVISION), so a club with no top-flight
history is rated off the division it came up from rather than a placeholder. This
builds the same state and advances it to the same CUTOFF DATE, so promoted sides
are rated here exactly as they are live and no future second-tier result leaks in.
Only the five leagues in that map are affected; every other league gets None in
production too.

READING IT. Look for the racing failure mode: favourites claiming more than they
deliver and longshots less, with the error growing toward the extremes. A
uniform gap is a shift and is fixable by shrinking; a gap that grows with the
claim is overconfidence and needs the distribution widened.
"""
from __future__ import annotations

import copy
import datetime
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import soccer_data
from app.ingestion.market_matcher_soccer import canonical_team_key
from app.models.baseline.elo_soccer import (
    SoccerRatingState,
    home_advantage_for_league,
    predict_and_update,
)
from app.models.baseline import elo_service_soccer, soccer_xg
from app.clients.football_data_client import PROMOTION_SOURCE_DIVISION
from app.models.season_sim_soccer import (
    CALENDAR_YEAR_LEAGUES,
    RELEGATION_ZONE_SIZE,
    simulate_season,
)

# Every top-flight league the app prices season futures for that has enough
# history to score. The first pass covered only RELEGATION_ZONE_SIZE's five, but
# 735 season-futures markets are live and three of the user's open positions sit
# in leagues outside that set -- so their corrections were extrapolated.
#
# The relegation question is scored ONLY where RELEGATION_ZONE_SIZE defines a
# real zone; inventing a zone size for the rest would be making up the answer
# key. The other four questions need no zone and are scored everywhere.
#
# DELIBERATELY EXCLUDED, because simulate_season models a straight double
# round-robin and these are not one -- a bad score would measure the format
# mismatch, not calibration:
#   MEX1  split torneos, won in the Liguilla (playoff_sim_service_ligamx)
#   MLS   unbalanced conference schedule (season_sim_soccer's own docstring)
#   ARG1  rotating multi-stage format
# KEPT despite partial-format doubts (B1/G1 playoffs, SC0's post-33-game split)
# precisely so the per-league table SHOWS whether that breaks them.
LEAGUES = ["E0", "SP1", "I1", "D1", "F1",        # already tested
           "N1", "P1", "BRA1",                   # the user's open positions
           "T1", "B1", "G1", "SC0",              # other European top flights
           "JPN1", "DNK1", "NOR1", "SWE1", "CHN1"]
# Top flights that are SCORED, plus the second tiers they promote from. Only
# the first set is measured; the second is rating input.
_WANTED_DIVISIONS = set(LEAGUES) | {
    d for lg, d in PROMOTION_SOURCE_DIVISION.items() if lg in LEAGUES
}
CUTOFFS = [0.0, 0.25, 0.50, 0.75]
N_SIMULATIONS = 2000
WARMUP_SEASONS = 3          # seasons of ratings before the first scored season
MIN_TEAMS = 14              # skip malformed/partial season slices
SEED = 20260825

BUCKETS = [(0.0, 0.05), (0.05, 0.15), (0.15, 0.30), (0.30, 0.50),
           (0.50, 0.70), (0.70, 0.90), (0.90, 1.01)]


def season_of(league: str, d: datetime.date) -> int:
    """Season START year, matching current_season_table's own windowing."""
    if league in CALENDAR_YEAR_LEAGUES:
        return d.year
    return d.year if d.month >= 7 else d.year - 1


def parse_date(v):
    try:
        return datetime.date.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


def final_table(matches):
    """team -> (points, gd, gf, clean_sheets), and the ranked order."""
    agg = defaultdict(lambda: [0, 0, 0, 0])   # pts, gf, ga, cs
    for m in matches:
        h, a = m["home"], m["away"]
        hg, ag = m["hg"], m["ag"]
        for team, gf, ga in ((h, hg, ag), (a, ag, hg)):
            row = agg[team]
            row[1] += gf
            row[2] += ga
            if gf > ga:
                row[0] += 3
            elif gf == ga:
                row[0] += 1
            if ga == 0:
                row[3] += 1
    table = {t: (v[0], v[1] - v[2], v[1], v[3]) for t, v in agg.items()}
    order = sorted(table, key=lambda t: (-table[t][0], -table[t][1], -table[t][2], t))
    return table, order


def main() -> int:
    print("loading matches...", flush=True)
    raw = [m for m in soccer_data.load_matches() if not elo_service_soccer._is_exhibition(m)]
    by_league = defaultdict(list)
    for m in raw:
        # Second-tier divisions are loaded but never SCORED -- they exist only
        # to build the promotion-source rating state that production passes
        # into simulate_season.
        if m.get("league") not in _WANTED_DIVISIONS:
            continue
        if m.get("home_goals_ft") is None:
            continue
        d = parse_date(m.get("match_date"))
        if d is None:
            continue
        by_league[m["league"]].append({
            "date": d,
            "home": canonical_team_key(m["home_team"]),
            "away": canonical_team_key(m["away_team"]),
            "hg": int(m["home_goals_ft"]),
            "ag": int(m["away_goals_ft"]),
            "raw_home": m["home_team"],
            "raw_away": m["away_team"],
            "raw_date": m.get("match_date"),
        })
    for lg in by_league:
        by_league[lg].sort(key=lambda m: (m["date"], m["home"], m["away"]))
        print(f"  {lg}: {len(by_league[lg])} matches", flush=True)

    # question -> cutoff -> list of (predicted, actual 0/1)
    scored = defaultdict(lambda: defaultdict(list))
    per_league = defaultdict(lambda: defaultdict(list))
    n_team_seasons = defaultdict(int)

    for league in LEAGUES:
        matches = by_league.get(league, [])
        if not matches:
            continue
        seasons = defaultdict(list)
        for m in matches:
            seasons[season_of(league, m["date"])].append(m)
        years = sorted(y for y, ms in seasons.items() if len(ms) >= 100)
        scored_years = years[WARMUP_SEASONS:]
        # The final year is usually a season in progress -- drop it, since an
        # unfinished table has no answer key.
        if scored_years:
            last = scored_years[-1]
            if len(seasons[last]) < 0.9 * max(len(seasons[y]) for y in scored_years):
                scored_years = scored_years[:-1]
        print(f"\n{league}: scoring seasons {scored_years[0] if scored_years else '-'}"
              f"..{scored_years[-1] if scored_years else '-'} "
              f"({len(scored_years)})", flush=True)
        zone = RELEGATION_ZONE_SIZE.get(league)  # None -> skip relegation

        for year in scored_years:
            season_matches = seasons[year]
            teams = sorted({t for m in season_matches for t in (m["home"], m["away"])})
            if len(teams) < MIN_TEAMS:
                continue
            table, order = final_table(season_matches)
            best_cs = max(v[3] for v in table.values())
            cs_leaders = {t for t, v in table.items() if v[3] == best_cs}
            pos_of = {t: i + 1 for i, t in enumerate(order)}

            # PROMOTION SOURCE. Production hands simulate_season the second-tier
            # state so a club with no top-flight history is rated off the division
            # it came up from instead of a placeholder. Built here the same way and
            # advanced to the same CUTOFF DATE, so it carries no future information
            # -- a second-tier result from later in the season would leak exactly
            # the kind of hindsight this harness exists to avoid.
            second_div = PROMOTION_SOURCE_DIVISION.get(league)
            second_matches = by_league.get(second_div, []) if second_div else []
            second_state = (SoccerRatingState(home_log=home_advantage_for_league(second_div))
                            if second_div else None)
            second_fed = 0

            def _advance_second(until_date):
                """Feed second-tier matches strictly BEFORE until_date."""
                nonlocal second_fed
                if second_state is None:
                    return
                while (second_fed < len(second_matches)
                       and second_matches[second_fed]["date"] < until_date):
                    sm = second_matches[second_fed]
                    scm = {"home_team": sm["home"], "away_team": sm["away"],
                           "home_goals_ft": sm["hg"], "away_goals_ft": sm["ag"],
                           "match_date": sm["raw_date"], "league": second_div}
                    sxg = soccer_xg.lookup(second_div, sm["raw_date"],
                                           sm["raw_home"], sm["raw_away"])
                    if sxg is not None:
                        scm["xg_h"], scm["xg_a"] = sxg
                    predict_and_update(second_state, scm)
                    second_fed += 1

            # Ratings from every earlier season of this league.
            state = SoccerRatingState(home_log=home_advantage_for_league(league))
            for y in sorted(seasons):
                if y >= year:
                    break
                for m in seasons[y]:
                    cm = {"home_team": m["home"], "away_team": m["away"],
                          "home_goals_ft": m["hg"], "away_goals_ft": m["ag"],
                          "match_date": m["raw_date"], "league": league}
                    xg = soccer_xg.lookup(league, m["raw_date"], m["raw_home"], m["raw_away"])
                    if xg is not None:
                        cm["xg_h"], cm["xg_a"] = xg
                    predict_and_update(state, cm)

            n = len(season_matches)
            fed = 0
            for frac in CUTOFFS:
                target = int(frac * n)
                # Advance ratings + the part-season table to this cutoff.
                while fed < target:
                    m = season_matches[fed]
                    cm = {"home_team": m["home"], "away_team": m["away"],
                          "home_goals_ft": m["hg"], "away_goals_ft": m["ag"],
                          "match_date": m["raw_date"], "league": league}
                    xg = soccer_xg.lookup(league, m["raw_date"], m["raw_home"], m["raw_away"])
                    if xg is not None:
                        cm["xg_h"], cm["xg_a"] = xg
                    predict_and_update(state, cm)
                    fed += 1

                # Second tier advanced to the cutoff's own date (season start
                # when nothing has been played yet).
                _advance_second(season_matches[fed]["date"] if fed < len(season_matches)
                                else season_matches[-1]["date"])

                played = season_matches[:target]
                if played:
                    part, _ = final_table(played)
                    # simulate_season wants (points, goals_for, goals_against);
                    # final_table returns (pts, gd, gf, cs), so ga = gf - gd.
                    starting_table = {t: (v[0], v[2], v[2] - v[1]) for t, v in part.items()}
                    starting_cs = {t: v[3] for t, v in part.items()}
                    played_pairs = {(m["home"], m["away"]) for m in played}
                else:
                    starting_table, starting_cs, played_pairs = None, None, None

                res = simulate_season(
                    copy.deepcopy(state), teams, league,
                    n_simulations=N_SIMULATIONS, seed=SEED,
                    second_tier_state=(copy.deepcopy(second_state)
                                       if second_state is not None else None),
                    starting_table=starting_table,
                    played_pairs=played_pairs,
                    starting_clean_sheets=starting_cs,
                )
                pdist = res.position_dist or {}
                for t in teams:
                    d = pdist.get(t, {})
                    p_champ = d.get(1, 0.0)
                    p_top4 = sum(d.get(p, 0.0) for p in (1, 2, 3, 4))
                    p_third = d.get(3, 0.0)
                    p_rel = (res.relegation_prob or {}).get(t, 0.0)
                    p_cs = (res.most_clean_sheets_prob or {}).get(t, 0.0)
                    pos = pos_of.get(t)
                    if pos is None:
                        continue
                    scored["champion"][frac].append((p_champ, 1.0 if pos == 1 else 0.0))
                    per_league["champion"][(league, frac)].append(
                        (p_champ, 1.0 if pos == 1 else 0.0))
                    scored["top_four"][frac].append((p_top4, 1.0 if pos <= 4 else 0.0))
                    per_league["top_four"][(league, frac)].append(
                        (p_top4, 1.0 if pos <= 4 else 0.0))
                    scored["exact_third"][frac].append((p_third, 1.0 if pos == 3 else 0.0))
                    if zone:
                        scored["relegated"][frac].append(
                            (p_rel, 1.0 if pos > len(order) - zone else 0.0))
                        per_league["relegated"][(league, frac)].append(
                            (p_rel, 1.0 if pos > len(order) - zone else 0.0))
                    scored["most_clean_sheets"][frac].append(
                        (p_cs, 1.0 if t in cs_leaders else 0.0))
                    per_league["most_clean_sheets"][(league, frac)].append(
                        (p_cs, 1.0 if t in cs_leaders else 0.0))
                    n_team_seasons[frac] += 1
            print(f"  {year} done ({len(teams)} teams)", flush=True)

    # ---------------- report ------------------------------------------------
    print("\n" + "=" * 96)
    print("CALIBRATION -- claimed probability vs how often it actually happened")
    print("=" * 96)
    for question in ("champion", "top_four", "exact_third", "relegated", "most_clean_sheets"):
        print(f"\n### {question}")
        for frac in CUTOFFS:
            rows = scored[question][frac]
            if not rows:
                continue
            claimed = sum(p for p, _ in rows) / len(rows)
            actual = sum(y for _, y in rows) / len(rows)
            brier = sum((p - y) ** 2 for p, y in rows) / len(rows)
            print(f"  {int(frac*100):>3}% played   n={len(rows):>5}   "
                  f"claimed {claimed:.4f}  actual {actual:.4f}  "
                  f"gap {(claimed-actual)*100:+.2f}pp   Brier {brier:.4f}")
            for lo, hi in BUCKETS:
                g = [(p, y) for p, y in rows if lo <= p < hi]
                if len(g) < 15:
                    continue
                c = sum(p for p, _ in g) / len(g)
                a = sum(y for _, y in g) / len(g)
                flag = ""
                if abs(c - a) > 0.10:
                    flag = "   <-- off by >10pp"
                print(f"        {lo:.2f}-{hi:.2f}  n={len(g):>5}  "
                      f"claimed {c:.3f}  actual {a:.3f}  {(c-a)*100:+7.1f}pp{flag}")
    # PER LEAGUE. Pooling 17 leagues lets the well-behaved big five mask a
    # league whose format the round-robin model does not describe -- the same
    # "aggregate hides the build" trap this exercise exists to avoid. B1/G1
    # have playoffs and SC0 splits after 33 games; if that breaks them, it
    # shows up here rather than being averaged away.
    print("=" * 96)
    print("PER LEAGUE -- overconfidence on rows claiming >= 0.50 (claimed - actual, pp)")
    print("  dash = fewer than 10 such rows at that cutoff")
    print("=" * 96)
    for question in ("champion", "top_four", "most_clean_sheets", "relegated"):
        print("")
        print("### " + question)
        print("  league  " + "  ".join(f"{int(f*100):>3}%played" for f in CUTOFFS))
        for league in LEAGUES:
            cells, any_data = [], False
            for frac in CUTOFFS:
                rr = [(pp, yy) for pp, yy in per_league[question][(league, frac)] if pp >= 0.50]
                if len(rr) < 10:
                    cells.append("       -  ")
                    continue
                any_data = True
                gap = (sum(pp for pp, _ in rr) - sum(yy for _, yy in rr)) / len(rr)
                cells.append(f"{gap*100:+7.1f}pp ")
            if any_data:
                print(f"  {league:6}  " + " ".join(cells))
    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

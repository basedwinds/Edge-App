"""Measure per-league STRENGTH OFFSETS for soccer, from promoted/relegated clubs.

THE PROBLEM. elo_service_soccer keeps one SoccerRatingState per league, and
attack_log/concede_log are relative to THAT league's average (0.0 = average).
So each pool is internally consistent but two pools are not comparable: a 0.0
in E0 is a much stronger side than a 0.0 in E1. That is what blocks every
cross-league market (domestic cups spanning two divisions, UEFA competitions).

THE ONLY BRIDGE IN THIS DATA. football-data.co.uk is domestic-only -- there are
no matches BETWEEN leagues to calibrate against. Measured 2026-08-06, the teams
shared between any two leagues are exactly the promoted/relegated ones, and only
WITHIN a country:

    I1 <-> I2  48    SP1 <-> SP2  48    E0 <-> E1  45
    F1 <-> F2  42    D1  <-> D2   42
    (cross-country: zero, once the All-Star exhibitions are excluded)

So within-country offsets are measurable and cross-country ones are NOT, from
this source. That is a hard limit, not a modelling choice.

METHOD. Walk each league chronologically, snapshotting every club's attack_log
and concede_log at the end of each season. For a club in league A in season N
and league B in season N+1, the change in its rating is (league quality gap) +
(real change in the club). Averaged over many moves the second term shrinks,
leaving the gap.

KNOWN BIAS, stated up front: relegated clubs genuinely weaken (players leave)
and promoted clubs strengthen, so this estimate UNDERSTATES the true gap. It is
a lower bound on how much better the top flight is, and it should be read that
way rather than as a precise constant.
"""
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingestion import soccer_data  # noqa: E402
from app.ingestion.market_matcher_soccer import canonical_team_key  # noqa: E402
from app.models.baseline.elo_soccer import SoccerRatingState, predict_and_update  # noqa: E402
from app.models.baseline.elo_service_soccer import _is_exhibition  # noqa: E402

MIN_MATCHES = 10   # a club must have really played the season in that league
MIN_MOVES = 8      # below this a league pair is reported but not trusted


def main():
    matches = [m for m in soccer_data.load_matches() if not _is_exhibition(m)]
    by_league = defaultdict(list)
    for m in matches:
        by_league[m["league"]].append(m)

    # {(league, season, team): (attack, concede)} at that season's end.
    snaps: dict[tuple, tuple[float, float]] = {}
    for league, rows in by_league.items():
        rows.sort(key=lambda m: (m.get("season") or "", m.get("match_date") or ""))
        state = SoccerRatingState()
        season = None
        # Matches played IN THE CURRENT SEASON, per club. state.get_count is
        # CUMULATIVE across seasons, so using it here snapshotted clubs that had
        # not played in this league for years -- they linger in attack_log
        # forever once added. That silently invented league memberships and lost
        # most real transitions (923 exist in the raw data; the buggy version
        # found ~200 and missed whole country pairs).
        played: dict[str, int] = {}

        def flush(season_label):
            for team, n in played.items():
                if n >= MIN_MATCHES:
                    snaps[(league, season_label, team)] = (state.get_attack(team), state.get_concede(team))

        for m in rows:
            s = m.get("season")
            if s != season and season is not None:
                flush(season)
                played = {}
            season = s
            home = canonical_team_key(m["home_team"])
            away = canonical_team_key(m["away_team"])
            played[home] = played.get(home, 0) + 1
            played[away] = played.get(away, 0) + 1
            predict_and_update(state, {**m, "home_team": home, "away_team": away})
        if season is not None:
            flush(season)

    # Season labels are NOT one format: European leagues use "1993-1994",
    # MLS uses "2018". Sorting them together put "2025-2026" immediately before
    # "2018" and broke every successor chain -- so key on the STARTING YEAR and
    # look for year+1 instead of "the next label in a sorted list".
    def start_year(season: str) -> int | None:
        try:
            return int(str(season)[:4])
        except (TypeError, ValueError):
            return None

    by_team = defaultdict(dict)
    for (league, season, team), vals in snaps.items():
        y = start_year(season)
        if y is not None:
            by_team[team][y] = (league, vals)

    moves = defaultdict(list)  # (from_league, to_league) -> [(d_attack, d_concede)]
    for team, per_season in by_team.items():
        for season, (league, (atk, con)) in per_season.items():
            if season + 1 not in per_season:
                continue
            nl, (natk, ncon) = per_season[season + 1]
            if nl == league:
                continue
            moves[(league, nl)].append((natk - atk, ncon - con))

    print(f"season-end snapshots: {len(snaps)}   clubs: {len(by_team)}\n")
    print(f"{'move':16} {'n':>4} {'d attack':>10} {'d concede':>11}")
    offsets = {}
    for (a, b), vals in sorted(moves.items(), key=lambda x: -len(x[1])):
        if len(vals) < 3:
            continue
        da = statistics.mean(v[0] for v in vals)
        dc = statistics.mean(v[1] for v in vals)
        note = "" if len(vals) >= MIN_MOVES else "   (thin)"
        print(f"{a + ' -> ' + b:16} {len(vals):4} {da:+10.4f} {dc:+11.4f}{note}")
        offsets[(a, b)] = (len(vals), da, dc)

    print("\nPAIRED (both directions seen) -- a consistent pair should be near-mirrored:")
    for (a, b) in list(offsets):
        if (b, a) in offsets and a < b:
            na, da, dca = offsets[(a, b)]
            nb, db, dcb = offsets[(b, a)]
            print(f"  {a}<->{b}: down {da:+.4f}/{dca:+.4f} (n={na})   up {db:+.4f}/{dcb:+.4f} (n={nb})"
                  f"   attack asymmetry {da + db:+.4f}")


if __name__ == "__main__":
    main()

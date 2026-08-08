"""Merge duplicate live soccer fixtures so their bets can settle.

THE BUG. find_or_create_upcoming_match joins a platform's listing onto an
existing row by TEAM NAME. When two platforms spell the same clubs differently
it fails to join and creates a second row for one real fixture. Neither row ever
gets a result -- and worse, at most one of them CAN, because refresh_soccer_
results also matches ESPN by name. Measured 2026-08-08: 13 duplicated fixtures
across 172 live rows, holding 47 pending bets.

    P1   CS Marítimo / Madeira            vs Casa Pia    37 bets
    MLS  Los Angeles FC / Los Angeles F   vs Sporting KC
    MLS  CF Montréal / Montreal           vs Inter Miami
    MLS  St. Louis City SC / Saint Louis  vs Colorado
    MLS  New York Red Bulls / New York RB vs Charlotte

Two distinct causes, one symptom. Marítimo/Madeira is a real alias (Marítimo is
the club FROM Madeira). The MLS ones are TRUNCATION -- "Los Angeles F",
"New York RB", "Los Angeles G" -- where one feed is cutting the name short and
canonical_team_key reasonably treats the fragment as a different club.

WHY THIS DOES NOT MATCH ON NAMES. Fixing it with a name-alias table would need
an entry per spelling per club and would still miss the next one. Worse, names
are exactly what is unreliable here, and a wrong alias is far more costly than a
missed one: "Madeira" is ambiguous on its face, because CD Nacional is ALSO a
Madeira club. So identity is established the same way the ESPN and Kalshi alias
maps established it earlier today -- BY FIXTURE:

    same league
    AND same kickoff DATE
    AND at least one side canonicalizing to the same club

with a UNIQUENESS requirement: a group is only merged when exactly one candidate
pairing exists. Two clubs playing twice on one date, or an ambiguous group,
is skipped and reported rather than guessed.

WHICH ROW SURVIVES. The one with the longer combined team names. That is not
cosmetic: every observed duplicate pairs a full name against a truncated or
partial one, and the fuller name is the one ESPN's own feed will match, which is
what lets the merged row actually settle. Verified against all 13 live cases --
CS Marítimo over Madeira, Los Angeles FC over Los Angeles F, St. Louis City SC
over Saint Louis.

WHAT IT CHANGES. Repoints placed_bets and markets onto the survivor, then tags
the loser's source_match_id with ":dup-of-<id>" so nothing re-attaches to it.
NOTHING IS DELETED -- the loser row stays, and the tag is reversible.

DRY RUN BY DEFAULT. Prints the full plan and writes nothing. Pass --apply to
commit. This touches the Bet Tracker, which is the app's only real yardstick,
so it does not run unattended and it does not run by accident.
"""
from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.ingestion.market_matcher_soccer import canonical_team_key  # noqa: E402


def load(conn):
    return list(conn.execute(
        "select id, league, home_team, away_team, estimated_start_time, result_ft "
        "from soccer_matches where source='live' and estimated_start_time is not null"
    ))


def find_groups(rows):
    """Fixture identity: (league, kickoff date) + a shared canonical side."""
    by_day = collections.defaultdict(list)
    for r in rows:
        by_day[(r[1], str(r[4])[:10])].append(r)

    groups, ambiguous = [], []
    for key, day_rows in by_day.items():
        used = set()
        for i, a in enumerate(day_rows):
            if a[0] in used:
                continue
            ah, aa = canonical_team_key(a[2]), canonical_team_key(a[3])
            matches = [
                b for b in day_rows[i + 1:]
                if b[0] not in used
                and (canonical_team_key(b[2]) == ah or canonical_team_key(b[3]) == aa
                     or canonical_team_key(b[2]) == aa or canonical_team_key(b[3]) == ah)
            ]
            if not matches:
                continue
            if len(matches) > 1:
                ambiguous.append((key, a, matches))  # never guess
                continue
            b = matches[0]
            used.update({a[0], b[0]})
            # Survivor = fuller names, which is what ESPN's feed will match.
            pair = sorted((a, b), key=lambda r: -(len(r[2] or "") + len(r[3] or "")))
            groups.append((key, pair[0], pair[1]))
    return groups, ambiguous


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="commit changes (default: dry run)")
    args = ap.parse_args()

    path = settings.sqlite_url().replace("sqlite:///", "")
    conn = sqlite3.connect(path if args.apply else f"file:{path}?mode=ro", uri=not args.apply)
    rows = load(conn)
    groups, ambiguous = find_groups(rows)

    print(f"{len(rows)} live soccer fixtures -> {len(groups)} duplicate pairs, "
          f"{len(ambiguous)} ambiguous (skipped)\n")

    moved_bets = moved_markets = 0
    for (league, day), keep, drop in groups:
        nb = conn.execute("select count(*) from placed_bets where soccer_match_id=?", (drop[0],)).fetchone()[0]
        nm = conn.execute("select count(*) from markets where soccer_match_id=?", (drop[0],)).fetchone()[0]
        moved_bets += nb
        moved_markets += nm
        print(f"{league} {day}")
        print(f"   KEEP {keep[0]:5d}  {keep[2]} vs {keep[3]}")
        print(f"   DROP {drop[0]:5d}  {drop[2]} vs {drop[3]}   -> {nb} bets, {nm} markets move")
        if args.apply:
            conn.execute("update placed_bets set soccer_match_id=? where soccer_match_id=?", (keep[0], drop[0]))
            conn.execute("update markets set soccer_match_id=? where soccer_match_id=?", (keep[0], drop[0]))
            conn.execute(
                "update soccer_matches set source_match_id=source_match_id||? where id=? "
                "and source_match_id not like '%:dup-of-%'",
                (f":dup-of-{keep[0]}", drop[0]),
            )

    for key, a, ms in ambiguous:
        print(f"AMBIGUOUS {key}: {a[0]} matches {[m[0] for m in ms]} -- skipped, resolve by hand")

    if args.apply:
        conn.commit()
        print(f"\nAPPLIED: {moved_bets} bets and {moved_markets} markets repointed, "
              f"{len(groups)} rows tagged as duplicates.")
    else:
        print(f"\nDRY RUN -- nothing written. Would move {moved_bets} bets and "
              f"{moved_markets} markets across {len(groups)} pairs.")
        print("Re-run with --apply to commit.")
    conn.close()


if __name__ == "__main__":
    main()

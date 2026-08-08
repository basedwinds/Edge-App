"""Build data/soccer_espn_aliases.json -- a VERIFIED ESPN -> football-data club
name map, derived from date-aligned fixtures rather than string similarity.

WHY NOT JUST FUZZY-MATCH THE NAMES. Because it was tried and it is unsafe.
check_uefa_name_gap.py scored Rangers -> Angers at 0.92 (a Scottish club onto a
French one) and Celtic -> Celta at 0.73, sitting right beside correct hits like
AS Monaco -> monaco at 1.00. There is no threshold that admits the second and
rejects the first, and this project has already shipped that exact bug once
(Espanyol -> Barcelona, via a shared "Barcelona" token). A wrong alias here does
not fail loudly -- it silently prices the wrong club and stakes money on it.

WHAT IS USED INSTEAD. Two independent feeds describe the SAME real matches.
football-data.co.uk gives (date, home, away, score) for 12 leagues; ESPN gives
the same fixtures under its own spellings. So a fixture is a natural join key
that never touches the club names: if on 2025-09-13 exactly one Serie A match
finished 2-1, then ESPN's 2-1 match that day IS football-data's, and their home
sides are the same club whatever each feed calls it.

THE SAFETY RULES, all of which must pass before an alias is written:
  * only fixtures where the (date, score) pair is UNIQUE within that league and
    date window contribute a vote -- an ambiguous day teaches nothing;
  * a pair needs MIN_VOTES independent fixtures agreeing;
  * the winning football-data name must hold >= CONSISTENCY of all votes cast
    for that ESPN name, so a club that "matches" several targets is dropped;
  * the mapping must be injective -- if two ESPN names both claim one
    football-data club, the weaker claim is dropped rather than guessed.
Anything failing these is reported as UNRESOLVED for human eyes, never written.

DATE TOLERANCE: ESPN timestamps are UTC and football-data records local match
day, so a 20:45 CET kickoff can differ by a calendar day. Fixtures are matched
within +/- 1 day, and that widened window is also why (date, score) uniqueness
is enforced over the whole window rather than a single day.
"""
from __future__ import annotations

import collections
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.base import get_json  # noqa: E402
from app.ingestion import soccer_data  # noqa: E402

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
OUT = DATA_DIR / "soccer_espn_aliases.json"
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard?dates={a}-{b}&limit=500"

# football-data league code -> ESPN slug. Only leagues this app actually rates.
LEAGUES = {
    "E0": "eng.1", "E1": "eng.2",
    "SP1": "esp.1", "SP2": "esp.2",
    "I1": "ita.1", "I2": "ita.2",
    "D1": "ger.1", "D2": "ger.2",
    "F1": "fra.1", "F2": "fra.2",
    "N1": "ned.1", "P1": "por.1",
}
# Two full seasons -- more independent fixtures per club, and it covers clubs
# that were promoted or relegated between them.
WINDOWS = [(datetime.date(2024, 7, 1), datetime.date(2025, 6, 30)),
           (datetime.date(2025, 7, 1), datetime.date(2026, 6, 30))]
MIN_VOTES = 3
CONSISTENCY = 0.8
DAY_TOLERANCE = 1


def month_chunks(start: datetime.date, end: datetime.date):
    d = start
    while d <= end:
        nxt = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        yield d, min(nxt - datetime.timedelta(days=1), end)
        d = nxt


def fetch_espn(slug: str) -> list[tuple[datetime.date, str, str, int, int]]:
    out, seen = [], set()
    for window in WINDOWS:
        for a, b in month_chunks(*window):
            url = SCOREBOARD.format(slug=slug, a=a.strftime("%Y%m%d"), b=b.strftime("%Y%m%d"))
            try:
                data = get_json(url)
            except Exception:
                continue
            for ev in data.get("events", []):
                if ev.get("id") in seen:
                    continue
                seen.add(ev.get("id"))
                try:
                    comp = ev["competitions"][0]
                    if not comp.get("status", ev.get("status", {})).get("type", {}).get("completed"):
                        continue
                    home = away = None
                    for c in comp["competitors"]:
                        side = (c["team"]["displayName"], int(c["score"]))
                        if c["homeAway"] == "home":
                            home = side
                        else:
                            away = side
                    if not home or not away:
                        continue
                    d = datetime.date.fromisoformat(ev["date"][:10])
                except (KeyError, IndexError, ValueError, TypeError):
                    continue
                out.append((d, home[0], away[0], home[1], away[1]))
    return out


def main() -> None:
    fd_all = soccer_data.load_matches()
    by_league: dict[str, list[dict]] = collections.defaultdict(list)
    for m in fd_all:
        if m.get("league") in LEAGUES and m.get("match_date") and m.get("home_goals_ft") is not None:
            by_league[m["league"]].append(m)

    aliases: dict[str, dict] = {}
    unresolved: list[tuple[str, str, int]] = []
    stats = []

    for code, slug in LEAGUES.items():
        espn = fetch_espn(slug)
        # football-data fixtures indexed by exact date
        fd_by_date: dict[datetime.date, list[dict]] = collections.defaultdict(list)
        for m in by_league[code]:
            try:
                fd_by_date[datetime.date.fromisoformat(m["match_date"])].append(m)
            except ValueError:
                continue

        votes: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        used = 0
        for d, eh, ea, hg, ag in espn:
            # every football-data fixture within tolerance with THIS exact score
            cands = []
            for off in range(-DAY_TOLERANCE, DAY_TOLERANCE + 1):
                for m in fd_by_date.get(d + datetime.timedelta(days=off), []):
                    if m["home_goals_ft"] == hg and m["away_goals_ft"] == ag:
                        cands.append(m)
            if len(cands) != 1:
                continue  # ambiguous or absent -- teaches nothing
            m = cands[0]
            votes[eh][m["home_team"]] += 1
            votes[ea][m["away_team"]] += 1
            used += 1

        resolved = 0
        claims: dict[str, tuple[str, int]] = {}  # fd name -> (espn name, votes)
        for espn_name, counter in votes.items():
            fd_name, n = counter.most_common(1)[0]
            total = sum(counter.values())
            if n < MIN_VOTES or n / total < CONSISTENCY:
                unresolved.append((espn_name, f"{fd_name}?{n}/{total}", total))
                continue
            prev = claims.get(fd_name)
            if prev and prev[1] >= n:
                unresolved.append((espn_name, f"{fd_name} taken by {prev[0]}", total))
                continue
            if prev:
                unresolved.append((prev[0], f"{fd_name} lost to {espn_name}", prev[1]))
            claims[fd_name] = (espn_name, n)

        for fd_name, (espn_name, n) in claims.items():
            aliases[espn_name] = {"team": fd_name, "league": code, "votes": n}
            resolved += 1
        stats.append((code, len(espn), used, resolved))

    print(f"{'lg':4s} {'espn fixtures':>14s} {'usable':>8s} {'aliases':>8s}")
    for code, n_espn, used, resolved in stats:
        print(f"{code:4s} {n_espn:14d} {used:8d} {resolved:8d}")
    print(f"\nTOTAL aliases: {len(aliases)}   unresolved: {len(unresolved)}")

    if unresolved:
        print("\nUNRESOLVED (not written -- review these):")
        for name, why, total in sorted(unresolved, key=lambda x: -x[2])[:25]:
            print(f"  {name[:34]:34s} {why[:44]:44s} ({total} votes)")

    OUT.write_text(json.dumps(aliases, indent=1, sort_keys=True), encoding="utf-8")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()

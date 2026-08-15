"""Build a verified Understat <-> football-data team alias map for the big five.

WHY THIS IS NEEDED (#202). The xG blend validated in #167 (w=0.50, held-out
logloss 0.99235 -> 0.98934, 4/5 leagues) cannot be wired until an Understat
fixture can be attached to the football-data match the ratings actually replay.
Only 61 of 168 canonicalised Understat team keys match directly, and the misses
are CURRENT clubs -- borussia dortmund, bayer leverkusen, eintracht frankfurt --
so this is genuine naming divergence, not just Understat's deeper history.

JOIN ON FIXTURES, NEVER ON NAME SIMILARITY. That rule is written in blood in this
repo: see project_soccer_team_name_aliases (UNIQUE != SAFE) and the ESPN<->
football-data map (#100), which had to be rebuilt the same way. Fuzzy name
matching produces confident-looking aliases that quietly rate the wrong club.

THE JOIN KEY IS (league, date, home_goals, away_goals) -- entirely name-free.
Two independent sources recording the same league on the same day with the same
scoreline are the same fixture. Where a date carries two fixtures with an
identical scoreline the pairing is AMBIGUOUS and is skipped on the first pass,
then retried once other aliases have pinned one of the two teams.

EVERY ALIAS MUST BE CORROBORATED. A pairing seen once could be a coincidence of
scorelines; the map keeps only aliases confirmed by at least MIN_SUPPORT distinct
fixtures, and refuses any Understat name that resolves to two different
football-data names (that is a merge error, not an alias).

VERIFY BY COUNTING FIXTURES, NOT BY READING THE LIST. The output reports matched
fixtures per league-season. A map that looks plausible but joins 40% of fixtures
is a failure; the alias list itself is not the deliverable, the coverage is.

Run: backend/.venv/Scripts/python.exe scripts/build_understat_alias_map.py
Writes: data/understat_alias_map.json
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingestion.soccer_data import load_football_data_matches  # noqa: E402

DATA = Path(__file__).resolve().parents[2] / "data"
XG_CACHE = DATA / "soccer_xg_cache.json"
OUT = DATA / "understat_alias_map.json"
LEAGUES = ("E0", "SP1", "D1", "I1", "F1")
MIN_SUPPORT = 3          # distinct fixtures before an alias is trusted


def load_app():
    """{(league, date): [(home, away, gh, ga)]} from what the ratings replay."""
    by = defaultdict(list)
    for m in load_football_data_matches():
        lg = m.get("league")
        if lg not in LEAGUES:
            continue
        gh, ga = m.get("home_goals_ft"), m.get("away_goals_ft")
        if gh is None or ga is None:
            continue
        d = (m.get("match_date") or "")[:10]
        if d:
            by[(lg, d)].append((m["home_team"], m["away_team"], int(gh), int(ga)))
    return by


def load_understat():
    by = defaultdict(list)
    d = json.loads(XG_CACHE.read_text(encoding="utf-8"))
    for lg, seasons in d.items():
        if lg not in LEAGUES:
            continue
        for _s, matches in seasons.items():
            for m in matches:
                gh, ga = m.get("goals_h"), m.get("goals_a")
                if gh is None or ga is None:
                    continue
                by[(lg, m["date"][:10])].append((m["home"], m["away"], int(gh), int(ga)))
    return by


def main() -> None:
    app = load_app()
    und = load_understat()
    print(f"app fixture-days {len(app)}   understat fixture-days {len(und)}")
    shared = set(app) & set(und)
    print(f"shared (league, date) keys: {len(shared)}")
    if not shared:
        print("*** no shared dates -- check date formats before going further")
        return

    # votes[(league, understat_name)][football_data_name] = count
    votes: dict[tuple, Counter] = defaultdict(Counter)

    def pass_once(alias):
        """One sweep. Returns fixtures paired this sweep."""
        paired = 0
        for key in sorted(shared):
            lg = key[0]
            a_rows, u_rows = app[key], und[key]
            # index app fixtures by scoreline
            by_score = defaultdict(list)
            for h, aw, gh, ga in a_rows:
                by_score[(gh, ga)].append((h, aw))
            for uh, ua, gh, ga in u_rows:
                cands = by_score.get((gh, ga), [])
                if len(cands) == 1:
                    ah, aa = cands[0]
                elif len(cands) > 1:
                    # AMBIGUOUS scoreline -- only resolve if an existing alias
                    # already pins one side of exactly one candidate.
                    hits = [c for c in cands
                            if alias.get((lg, uh)) == c[0] or alias.get((lg, ua)) == c[1]]
                    if len(hits) != 1:
                        continue
                    ah, aa = hits[0]
                else:
                    continue
                votes[(lg, uh)][ah] += 1
                votes[(lg, ua)][aa] += 1
                paired += 1
        return paired

    alias: dict[tuple, str] = {}
    for sweep in range(1, 4):
        n = pass_once(alias)
        # rebuild alias from votes, keeping only well-supported unambiguous ones
        alias = {}
        conflicts = 0
        for k, c in votes.items():
            (best, nb), = c.most_common(1)
            if nb < MIN_SUPPORT:
                continue
            # a name that resolves to two football-data clubs is a MERGE ERROR
            if len(c) > 1 and c.most_common(2)[1][1] >= max(2, nb * 0.25):
                conflicts += 1
                continue
            alias[k] = best
        print(f"  sweep {sweep}: paired {n} fixtures, {len(alias)} aliases, {conflicts} rejected as ambiguous")

    # ---------------- verification: coverage, not eyeballing ----------------
    print(f"\nCOVERAGE -- the real test. An alias list can look fine and still join badly.")
    print(f"{'league':>8}{'understat':>11}{'joined':>9}{'pct':>7}")
    total_u = total_j = 0
    per_season = defaultdict(lambda: [0, 0])
    for key in sorted(und):
        lg, d = key
        for uh, ua, gh, ga in und[key]:
            total_u += 1
            per_season[lg][0] += 1
            if (lg, uh) in alias and (lg, ua) in alias:
                total_j += 1
                per_season[lg][1] += 1
    for lg in LEAGUES:
        u, j = per_season[lg]
        if u:
            print(f"{lg:>8}{u:>11}{j:>9}{100*j//u:>6}%")
    print(f"{'TOTAL':>8}{total_u:>11}{total_j:>9}{100*total_j//max(total_u,1):>6}%")

    unresolved = sorted({(lg, n) for (lg, n) in
                         {(l, x) for key in und for x in (und[key][0][0],) for l in (key[0],)} } )
    missing = sorted({(lg, u) for key in und for lg in (key[0],)
                      for row in und[key] for u in (row[0], row[1])
                      if (lg, u) not in alias})
    print(f"\nunresolved understat names: {len(missing)}")
    for lg, n in missing[:15]:
        print(f"    {lg}: {n}")

    OUT.write_text(json.dumps(
        {f"{lg}|{u}": a for (lg, u), a in sorted(alias.items())},
        indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {OUT} ({len(alias)} aliases)")


if __name__ == "__main__":
    main()

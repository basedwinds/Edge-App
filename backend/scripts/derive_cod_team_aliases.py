"""Derive CoD team aliases from the match record, and REFUSE the unsafe ones.

THE BUG THIS EXISTS FOR. The Esports World Cup lists Call of Duty teams under
their global org names ("OpTic Gaming", "Team Heretics", "100 Thieves") while
the CDL season lists the same rosters as city franchises ("OpTic Texas",
"Miami Heretics", "Los Angeles Thieves"). breakingpoint.gg records both, so a
top team's history splits: OpTic sits on 257 matches as "OpTic Texas" and 5 as
"OpTic Gaming". Pricing an EWC market resolves the 5-match stub, whose rating
is still ~the 1500 default, so the model returns a coin flip against a market
that is 82/18 -- and calls the difference a +33pp edge. That is a real staked
bet on the WRONG SIDE, not an edge.

elo_service_cod.resolve_team_name used to be a documented no-op, on the stated
grounds that "breakingpoint.gg returns the SAME full names the markets use
('OpTic Gaming', 'Team Falcons')". Those two examples are precisely the ones
that broke.

WHY THIS IS A SCRIPT AND NOT A HAND-WRITTEN MAP. A shared token is NOT
evidence: this app has already merged Espanyol into Barcelona that way. Every
candidate pair here has to survive three tests, and the ones that fail are
reported so the refusal is visible:

  1. DATE-DISJOINT. The old name's last match must precede the new name's
     first. One roster cannot be in two places, so a rename shows as a clean
     handover. Overlapping ranges mean two teams, not one.
  2. NO HEAD-TO-HEAD. If the two names ever played EACH OTHER they are
     definitionally different teams. This is the single cheapest disproof and
     it is what settled the soccer alias work.
  3. NO SAME-DAY APPEARANCE. Weaker than (2) but catches an org fielding two
     concurrent rosters, which is exactly what the Falcons do.
  4. SHORT HANDOVER. A rebrand is immediate; two unrelated teams that merely
     existed in different eras also pass 1-3, so the gap between the old
     name's last match and the new name's first must be small. This test is
     what makes derivation safe rather than reckless -- the first three alone
     accepted "Las Lentejas <- Las Vegas Legion" and "New Losers <- New York
     Subliners" on the tokens "las" and "new".

     The threshold is not tuned. Measured gaps fall into two clumps with a
     wide empty band between them: 17, 18, 19, 19, 20, 26, 40 days ... then
     nothing until 170, 245, 478, 530, 531, 537, 924, 961, 1623. Any cut
     inside that band gives the same answer.

Plus a shape test: the NEW name must be a thin stub (the thing being fixed)
and the OLD name must carry real history worth inheriting.

WHAT THIS FOUND (2026-08-09, 3,615 historical + live DB matches):

  ACCEPTED
    OpTic Gaming   <- OpTic Texas          5 vs 257 matches
    Team Heretics  <- Miami Heretics       5 vs 144
    100 Thieves    <- Los Angeles Thieves  5 vs 290

  REJECTED -- and this is the important half
    Team Falcons   <- Riyadh Falcons       NOT date-disjoint: Team Falcons runs
      2024-06-05..2026-08-08, straddling Riyadh Falcons' entire 2025-07..
      2026-07 window. The Falcons org fields several concurrent rosters
      (Falcons Academy and Falcons Academy White played each other 5 times;
      Riyadh Falcons shares 10 match-days with Falcons Academy). A token match
      would have merged them. Team Falcons keeps its own thin rating and is
      flagged by the games threshold instead.

Re-run this whenever a new event introduces new branding. It rewrites
cod_team_aliases.json; do not hand-edit that file.
"""
from __future__ import annotations

import collections
import datetime
import itertools
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import SessionLocal  # noqa: E402
from app.db.models import CodMatch  # noqa: E402
from app.models.baseline import elo_service_cod as E  # noqa: E402

OUT_PATH = Path(__file__).resolve().parents[1] / "app" / "models" / "baseline" / "cod_team_aliases.json"

STUB_MAX_GAMES = 25  # the NEW name must be a thin stub -- this is what we're repairing
ESTABLISHED_MIN_GAMES = 40  # the OLD name must carry history worth inheriting
MAX_HANDOVER_DAYS = 60  # sits inside the empty 40..170 band -- see the docstring
# Tokens too generic to imply a shared identity on their own.
STOPWORDS = {"team", "esports", "gaming", "the", "club", "academy", "white", "black", "blue", "red", "gg", "e", "fc"}


def tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t and t not in STOPWORDS}


def collect() -> dict[str, list[str]]:
    """team -> sorted list of match dates, from BOTH the historical cache and
    the live table. Either alone would give a partial picture: the cache holds
    the deep history, the DB holds what has happened since."""
    dates: dict[str, list[str]] = collections.defaultdict(list)
    hist = json.loads(OUT_PATH.parent.joinpath(E.HISTORICAL_CACHE_PATH.name).read_text(encoding="utf-8")) \
        if OUT_PATH.parent.joinpath(E.HISTORICAL_CACHE_PATH.name).exists() \
        else json.loads(E.HISTORICAL_CACHE_PATH.read_text(encoding="utf-8"))
    pairs: list[tuple[str, str]] = []
    for m in hist:
        a, b, d = m.get("team_a"), m.get("team_b"), (m.get("match_date") or "")[:10]
        for t in (a, b):
            if t:
                dates[t].append(d)
        if a and b:
            pairs.append((a, b))
    s = SessionLocal()
    try:
        for m in s.query(CodMatch).all():
            d = str(m.match_date)[:10]
            for t in (m.team_a, m.team_b):
                if t:
                    dates[t].append(d)
            if m.team_a and m.team_b:
                pairs.append((m.team_a, m.team_b))
    finally:
        s.close()
    return dates, pairs


def main() -> None:
    dates, pairs = collect()
    h2h = collections.Counter(frozenset(p) for p in pairs if p[0] != p[1])
    teams = {t: sorted(d for d in ds if d) for t, ds in dates.items()}
    print(f"{sum(len(v) for v in teams.values())} team-appearances across {len(teams)} names\n")

    accepted: dict[str, str] = {}
    rejected: list[tuple[str, str, str]] = []

    for a, b in itertools.combinations(sorted(teams), 2):
        shared = tokens(a) & tokens(b)
        if not shared:
            continue
        da, db = teams[a], teams[b]
        if not da or not db:
            continue
        # orient: NEW = the thin stub, OLD = the established name
        if len(da) <= len(db):
            new, old, dn, do = a, b, da, db
        else:
            new, old, dn, do = b, a, db, da
        if len(dn) > STUB_MAX_GAMES or len(do) < ESTABLISHED_MIN_GAMES:
            continue

        label = f"{new} <- {old}"
        if h2h[frozenset((new, old))]:
            rejected.append((label, "HEAD-TO-HEAD", f"played each other {h2h[frozenset((new, old))]}x -- different teams"))
            continue
        same_day = set(dn) & set(do)
        if same_day:
            rejected.append((label, "SAME-DAY", f"{len(same_day)} shared match-days, e.g. {sorted(same_day)[:2]}"))
            continue
        if not do[-1] < dn[0]:
            rejected.append((label, "NOT DISJOINT", f"{old} runs {do[0]}..{do[-1]}, {new} starts {dn[0]}"))
            continue
        gap = (datetime.date.fromisoformat(dn[0]) - datetime.date.fromisoformat(do[-1])).days
        if gap > MAX_HANDOVER_DAYS:
            rejected.append((label, "GAP TOO LONG", f"{gap}d between {old} ending and {new} starting -- eras, not a rebrand"))
            continue
        accepted[new] = old
        print(f"ACCEPT  {label:44s} {len(dn):3d} vs {len(do):4d} matches   "
              f"{old} ends {do[-1]}, {new} starts {dn[0]}")

    print()
    for label, why, detail in rejected:
        print(f"REJECT  {label:44s} {why:14s} {detail}")

    OUT_PATH.write_text(json.dumps(accepted, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nwrote {len(accepted)} alias(es) to {OUT_PATH.name}")
    for new, old in sorted(accepted.items()):
        r_new, r_old = E.get_team_rating(new), E.get_team_rating(old)
        print(f"   {new!r} now prices off {old!r}  ({r_new} -> {r_old})")


if __name__ == "__main__":
    main()

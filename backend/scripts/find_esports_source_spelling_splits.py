"""Find teams that two INGEST SOURCES spell differently, so the app rates them twice.

THE REAL CASE (found 2026-08-12 from a user question: "NIP Elo is 1561 off 5
series -- is that including Ninjas in Pyjamas?"). It was not.

    Ninjas in Pyjamas   liquipedia   172 series   1589.3
    NIP                 live           5 series   1560.5

Same org. The liquipedia crawl spells it out, the live feed abbreviates. They
NEVER play each other, and two of their matches are provably the SAME real
match: 2026-07-21 both beat M80, 2026-07-26 both played paiN / PaiN Gaming. So
CS2 priced NiP off 5 series while ignoring 167 more sitting in the same table.

WHY THE RESOLVER CANNOT CATCH THIS. team_name_resolver and lol_team_aliases
allow only ORTHOGRAPHIC transformations -- diacritic folding and dropping a
corporate token ("NRG Esports" -> "NRG"). That discipline is deliberate and
correct (see lol_team_aliases' docstring: a wrong alias pays out the wrong
side). But "NIP" and "Ninjas in Pyjamas" share no token at all, so no
mechanical spelling rule can bridge them. An ACRONYM needs evidence, not a rule.

THE EVIDENCE USED HERE is the same shape that produced the soccer alias map:
date-aligned fixtures. Two spellings are the same team when

  1. they are fed by DIFFERENT sources (a single source does not usually spell
     one team two ways, and if it does that is a different bug);
  2. on the same DATE they each played the SAME opponent -- one real match
     ingested twice; and
  3. they NEVER appear as opponents of each other. This is the veto, and it is
     what keeps a parent org and its academy roster apart: "NIP Impact" and
     "Young Ninjas" are real separate teams that do meet real opposition on
     their own, and any pair that has ever faced each other is disqualified
     outright rather than scored.

Condition 2 alone is not quite proof -- a group stage can have one team play
twice in a day -- so this script REPORTS candidates with their evidence rather
than merging anything. Read the evidence before wiring an alias.

Run: backend/.venv/Scripts/python.exe scripts/find_esports_source_spelling_splits.py
"""
from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Team names carry characters cp1252 cannot encode; without this the run dies
# partway through with a UnicodeEncodeError and the earlier sports scroll past
# looking complete.
sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import text  # noqa: E402

from app.db.database import SessionLocal  # noqa: E402

TABLES = [
    ("cs2", "cs2_matches"),
    ("valorant", "valorant_matches"),
    ("lol", "lol_matches"),
    ("cod", "cod_matches"),
]


MIN_SHARED_FIXTURES = 2
PLACEHOLDER = re.compile(r"^(tbd|tba|bye)\b", re.I)


def _orthographic_variant(x: str, y: str) -> bool:
    """One shared fixture IS enough when the two spellings already differ only
    by a corporate token or punctuation -- 'FURIA'/'FURIA Esports',
    'Gen.G'/'Gen.G Esports'. Those need no date evidence to be believable; the
    shared fixture just confirms it. An ACRONYM like NIP gets no such
    shortcut and must clear MIN_SHARED_FIXTURES on evidence alone."""
    return norm_opponent(x) == norm_opponent(y)


def norm_opponent(name: str | None) -> str:
    """Loose key for deciding two rows share an opponent. Deliberately loose --
    it only has to recognise 'paiN' and 'PaiN Gaming' as the same third party,
    and a false merge HERE cannot create an alias on its own (the pair still
    has to clear the never-met veto)."""
    if not name:
        return ""
    s = re.sub(r"[^a-z0-9]+", "", name.lower())
    for suffix in ("esports", "esport", "gaming", "team", "club"):
        if s.endswith(suffix) and len(s) > len(suffix) + 1:
            s = s[: -len(suffix)]
    return s


def main() -> None:
    session = SessionLocal()
    for sport, table in TABLES:
        try:
            rows = session.execute(text(
                f"SELECT match_date, source, team_a, team_b FROM {table} "
                f"WHERE team_a IS NOT NULL AND team_b IS NOT NULL"
            )).fetchall()
        except Exception as exc:  # noqa: BLE001
            print(f"\n=== {sport}: table {table} unavailable ({type(exc).__name__}) ===")
            continue

        print(f"\n=== {sport}: {len(rows)} match rows ===")
        if not rows:
            continue

        # Who has ever faced whom -- the veto set.
        faced: set[tuple[str, str]] = set()
        # (date, normalised opponent) -> {spelling: source}
        slot: dict[tuple, dict[str, str]] = collections.defaultdict(dict)
        sources_for: dict[str, set[str]] = collections.defaultdict(set)
        count_for: collections.Counter = collections.Counter()

        for date, source, a, b in rows:
            faced.add((a, b))
            faced.add((b, a))
            for me, opp in ((a, b), (b, a)):
                slot[(date, norm_opponent(opp))][me] = source
                sources_for[me].add(source)
                count_for[me] += 1

        # A pair is a candidate when both spellings fill the same (date,
        # opponent) slot from different sources.
        evidence: dict[tuple[str, str], list] = collections.defaultdict(list)
        for (date, opp), spellings in slot.items():
            if len(spellings) < 2:
                continue
            names = sorted(spellings)
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    x, y = names[i], names[j]
                    if spellings[x] == spellings[y]:
                        continue  # same source -- not a cross-source split
                    evidence[(x, y)].append((date, opp))

        found = 0
        for (x, y), ev in sorted(evidence.items(), key=lambda kv: -len(kv[1])):
            if (x, y) in faced:
                continue  # they have met -- genuinely different teams
            # ONE shared fixture is not evidence, it is a group stage. The first
            # run of this script proposed 'Bilibili Gaming' == 'TYLOO' and
            # 'All Gamers' == 'FunPlus Phoenix' on exactly one shared opponent
            # each -- both plainly wrong, both from an opponent who played twice
            # that day. Two independent shared fixtures is the bar.
            if len(ev) < MIN_SHARED_FIXTURES and not _orthographic_variant(x, y):
                continue
            # "TBD" is a scheduling placeholder, not a team; it shares fixtures
            # with everyone by construction.
            if PLACEHOLDER.match(x) or PLACEHOLDER.match(y):
                continue
            found += 1
            print(f"\n  CANDIDATE: {x!r} == {y!r}   ({len(ev)} shared fixture(s))")
            print(f"     {x!r}: {count_for[x]} rows, sources {sorted(sources_for[x])}")
            print(f"     {y!r}: {count_for[y]} rows, sources {sorted(sources_for[y])}")
            for date, opp in sorted(ev)[:4]:
                print(f"     both played {opp!r} on {date}")
        if not found:
            print("  no cross-source spelling splits detected")

    session.close()


if __name__ == "__main__":
    main()

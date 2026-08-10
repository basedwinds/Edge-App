"""Keep a MATCHUP out of the `event_name` field, which the UI renders as the league.

WHY THIS EXISTS (measured live 2026-08-10). A user reported that esports bets
either showed no league at all or showed "team vs team" where the league should
be. Both came from the same place. Counting active markets:

    cs2       blank=263  matchup-as-league=150  real tournament=0
    lol       blank=464  matchup-as-league=809  real tournament=0
    valorant  blank=0    matchup-as-league=334  real tournament=90

Split by which writer produced the row, the pattern is total:

    source=liquipedia / vlr  -> ALWAYS a real tournament   (348 rows, 0 noise)
    source=live              -> ALWAYS blank or a matchup  (0 tournaments)

`live` rows are created by the pollers from platform data, and the platforms do
not publish a tournament for these markets. Kalshi's event payload carries
`sub_title` = "ENCE Prospects vs. Inner Circle Prospect" and
`product_metadata.competition` = "CS2" -- the title, not the event. So the
poller was feeding the matchup into the league slot, and the row already shows
the matchup in its own label. It rendered twice.

Blank is the honest answer when the tournament is genuinely unknown; a repeated
matchup is worse than nothing because it reads like real information. Only the
scrapers know the tournament, so raising REAL coverage is a scraper-coverage
task, not a plumbing one.
"""
from __future__ import annotations

import re

_VS = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)
# Anything that can sit BETWEEN two team names: "A vs B", "A vs. B", "A x B",
# "A - B", "A | B". Used only to test what is left after removing both names.
_SEPARATORS = re.compile(r"vs\.?|[\s\-–—:x×|/,.]", re.IGNORECASE)


def looks_like_matchup(text: str | None, team_a: str | None = None,
                       team_b: str | None = None) -> bool:
    """True if `text` is a matchup label rather than a tournament name.

    Two independent tests, because each covers the other's gap:

      * both team names appear in the text -- decisive when we know the teams,
        and catches separators this function never anticipated ("A x B", "A - B");
      * a bare "vs"/"vs." separator -- covers rows where the platform spells a
        team differently than the fixture does, which is common enough that the
        name test alone would leak.

    A real event whose NAME contains "vs" would false-positive on the second
    test. None exists in the live data (all 348 scraper-sourced names are clean,
    all 1,293 vs-containing ones are matchups), and the cost is a blank league
    on such a row -- the same as today's blank, not a regression.
    """
    if not text:
        return False
    t = text.strip()
    if not t:
        return False

    a = (team_a or "").strip().lower()
    b = (team_b or "").strip().lower()
    low = t.lower()
    if a and b and a in low and b in low:
        # Both names being PRESENT is not enough -- generic club words collide
        # with real event names. "LCK Challengers League" contains both "LCK"
        # and "League" and is a tournament, not a matchup. So require that the
        # names plus a separator are the WHOLE string: strike both names out
        # and nothing meaningful may remain.
        residue = low.replace(a, " ", 1).replace(b, " ", 1)
        residue = _SEPARATORS.sub("", residue)
        if not residue.strip():
            return True

    parts = _VS.split(t)
    return len(parts) == 2 and all(p.strip() for p in parts)


def clean_event_name(text: str | None, team_a: str | None = None,
                     team_b: str | None = None) -> str:
    """`text` if it names a tournament, else "" -- never a matchup."""
    if looks_like_matchup(text, team_a, team_b):
        return ""
    return (text or "").strip()

"""Merge esports team names that differ ONLY by case, spacing or punctuation.

WHY. The rating pools carry the same team under several spellings, and each
spelling accumulates its own independent Elo. Measured 2026-08-10 across the
four esports pools: 56 such splits, and they are not cosmetic --

    Dplus Kia 1794.1  vs  Dplus KIA 1516.8     277 points apart, same team
    Heroic    1591.5  vs  HEROIC    1741.1     150 points
    Fnatic    1532.5  vs  fnatic    1542.5

Which rating a live match gets priced off then depends on which spelling the
market happens to use. There is no lookup-time redirect covering this: both
variants were confirmed to return their own separate ratings.

WHY NOT FUZZY MATCHING, WHICH IS THE OBVIOUS APPROACH AND IS WRONG HERE.
A similarity sweep over the same pools returned 166 pairs, and MOST are
genuinely different teams:

    Evil Geniuses      vs  Evil Geniuses GC    (Game Changers: separate roster)
    DetonatioN FocusMe vs  DetonatioN FocusMe GC
    BESTIA             vs  BESTIA.A            (academy side)
    Oman (National Team) vs Romania (National Team)
    Boomers            vs  Zoomers

Merging any of those is far more damaging than leaving a split: it fuses two
teams' histories into one meaningless rating. This module therefore folds ONLY
case, whitespace and punctuation, which separates every pair above ("evilgenius"
!= "evilgeniusgc", "bestia" != "bestiaa") while still catching "Yawara E-Sports"
== "Yawara Esports" and "One More" == "OneMore".

AND EVEN THAT IS NOT TRUSTED ON ITS OWN. Two distinct teams could in principle
differ only by spacing, so every candidate group is put through a DISPROOF test
before it is allowed to merge: a team cannot play itself, so if two variants
ever appear on opposite sides of the same match they are different teams and the
group is rejected. This is the same shape as the head-to-head test that caught a
wrong soccer alias -- a unique token match is not a safe one.
"""
from __future__ import annotations

import collections
import logging
import re

log = logging.getLogger("team_name_folding")

_STRIP = re.compile(r"[^a-z0-9]")


def fold(name: str) -> str:
    """Case/whitespace/punctuation-insensitive key. Nothing else is removed --
    no suffix stripping, no token dropping, no similarity."""
    return _STRIP.sub("", (name or "").lower())


# Characters that are invisible but not whitespace -- word joiner, zero-width
# space/non-joiner/joiner, BOM. Real scraped names carry these.
_INVISIBLE = "⁠​‌‍﻿"


def _canonical_rank(counts: collections.Counter):
    """Sort key choosing which spelling represents the merged team.

    CLEANLINESS BEATS FREQUENCY, and that ordering is not cosmetic. Ranking on
    frequency alone picked 'Croatian Flair x RLX ' (trailing space) and
    '\\u2060Hooligans' (leading word-joiner) as canonical on the real data,
    simply because the dirty spelling was scraped more often. Those names then
    become what the rating pool is keyed on -- and they will never match the
    clean name an exchange lists, so fixing the split would have introduced a
    fresh pricing miss. Prefer a name that is stripped and free of invisible
    characters; only then fall back to how often it appears.
    """
    def rank(name: str):
        clean = name == name.strip() and not any(c in name for c in _INVISIBLE)
        return (clean, counts[name], name)
    return rank


def build_canonical_map(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """{variant: canonical} for the SAFE merges implied by `pairs`.

    `pairs` is every (team_a, team_b) in the training history -- used both to
    count how often each spelling appears (the most-used one becomes canonical,
    so the merged pool keeps the name the data actually favours) and to run the
    self-play disproof test.

    Groups that fail disproof are dropped entirely and logged, rather than
    partially merged: if the group is not one team, no pairing inside it is
    trustworthy either.
    """
    counts: collections.Counter = collections.Counter()
    for a, b in pairs:
        if a:
            counts[a] += 1
        if b:
            counts[b] += 1

    groups: dict[str, set[str]] = collections.defaultdict(set)
    for name in counts:
        groups[fold(name)].add(name)
    candidates = {k: v for k, v in groups.items() if len(v) > 1}
    if not candidates:
        return {}

    # DISPROOF: a team cannot play itself.
    played_each_other: set[str] = set()
    for a, b in pairs:
        if a and b and a != b and fold(a) == fold(b):
            played_each_other.add(fold(a))

    out: dict[str, str] = {}
    for key, variants in candidates.items():
        if key in played_each_other:
            log.warning(
                "REFUSING to merge %s -- these spellings have faced each other, "
                "so they are different teams despite folding identically",
                sorted(variants),
            )
            continue
        canonical = max(variants, key=_canonical_rank(counts))
        for v in variants:
            if v != canonical:
                out[v] = canonical
    return out


def apply_to_matches(matches: list[dict], key_a: str = "team_a", key_b: str = "team_b") -> int:
    """Rewrite team names in-place to their canonical spelling. Returns the
    number of NAME OCCURRENCES rewritten (not groups), so a caller can log
    whether this is doing anything.

    Applied at POOL-BUILD time on purpose: rewriting here merges the two
    histories into one rating, which is the actual fix. A lookup-time redirect
    would only paper over it -- the pools would stay split and each would keep
    training on half the matches.
    """
    pairs = [(m.get(key_a), m.get(key_b)) for m in matches]
    canon = build_canonical_map(pairs)
    if not canon:
        return 0
    n = 0
    for m in matches:
        for k in (key_a, key_b):
            v = m.get(k)
            if v in canon:
                m[k] = canon[v]
                n += 1
    return n

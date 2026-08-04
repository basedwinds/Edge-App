"""Resolves this app's LoL team names onto gol.gg's spelling of the same team.

REAL GAP this closes: after the gol.gg results pipeline landed, 39 finished
matches still could not settle purely because the two sources spell a team
differently -- "INTZ e-Sports" vs "INTZ", "Esprit Shonen" with the o-macron,
"NORTHERNGRADE ESPORTS" vs "NORTHERNGRADE". Nothing about the result was
missing; only the join failed.

Only TWO transformations are allowed here, both purely orthographic:

  1. diacritic folding (Esprit Shonen, Panelao LHC) -- reuses
     market_matcher_lol.normalize_team_name so this file cannot drift from the
     matcher the Kalshi side already uses;
  2. dropping a trailing/leading CORPORATE token (Esports, Gaming, Club, ...).

Everything else stays unmatched ON PURPOSE. The candidates that look
tempting are exactly the ones that would misgrade a real bet:

  * "T1 Academy" is NOT "T1", "OKSavingsBank BRION Challengers" is NOT
    "OK BRION", "Kiwoom DRX Challengers" is NOT "DRX". These are separate
    rosters playing separate matches, and market_matcher_lol's own docstring
    already documents this as the collision its exact-matching discipline
    exists to prevent. TIER_MARKERS are never stripped, so a sub-roster can
    never collapse into its parent.
  * "OKSavingsBank BRION" -> "OK BRION" is a SPONSOR RENAME, not a spelling
    variant. There is no mechanical rule for it, and inventing one would be
    guessing.
  * "LOS" could be "Los Ratones", "Los Heretics" or "Los Grandes" -- genuinely
    ambiguous, so it resolves to nothing.

The ambiguity rule is what makes corporate-token stripping safe in general:
if stripping makes two DIFFERENT gol.gg teams share a key (the "Fuego Esports"
vs "Fuego Gaming" shape), that key is discarded rather than resolved to
whichever was seen first. A dropped alias costs one unsettled bet; a wrong one
pays out the wrong side.
"""
from __future__ import annotations

import re

from app.ingestion.market_matcher_lol import normalize_team_name

# Tokens that describe the ORGANISATION, not which of its teams played. Safe to
# drop because they carry no roster identity: "NRG" and "NRG Esports" are the
# same five players.
CORPORATE_TOKENS = frozenset({
    "esports", "esport", "esports club", "gaming", "club", "team",
    "the", "gg", "org",
})

# Tokens that DO change which roster played. Never stripped, and any name
# carrying one may only match a name carrying the same one.
TIER_MARKERS = frozenset({
    "academy", "challengers", "challenger", "youth", "junior", "juniors",
    "jr", "b", "ii", "2", "sub", "reserve", "development", "gc", "prospects",
})


def base_key(name: str | None) -> str:
    """Diacritics folded, lowercased, everything but letters/digits removed."""
    return re.sub(r"[^a-z0-9]", "", normalize_team_name(name or ""))


def _tokens(name: str | None) -> list[str]:
    # "e-Sports" and "e Sports" normalize to the two tokens "e"+"sports", which
    # no single corporate token can strip -- that is why "INTZ e-Sports" would
    # not reach "INTZ". Rejoin them first so one rule covers every spelling.
    text = re.sub(r"\be[\s-]?sports?\b", "esports", normalize_team_name(name or ""))
    return [t for t in text.split(" ") if t]


def tier_of(name: str | None) -> frozenset[str]:
    """Which sub-roster markers this name carries, if any."""
    return frozenset(t for t in _tokens(name) if t in TIER_MARKERS)


def alias_keys(name: str | None) -> frozenset[str]:
    """Every key this name may legitimately be joined on.

    Always includes base_key. Adds the corporate-token-stripped form only when
    stripping leaves something substantial AND removes no tier marker.
    """
    keys = set()
    base = base_key(name)
    if not base:
        return frozenset()
    keys.add(base)

    toks = _tokens(name)
    kept = [t for t in toks if t not in CORPORATE_TOKENS]
    # Refuse to strip down to nothing, or to a single character, or to a bare
    # number -- "The Gaming Club" must not become a key that matches anything.
    stripped = "".join(kept)
    if kept and len(stripped) >= 2 and not stripped.isdigit() and stripped != base:
        if tier_of(name) == tier_of(" ".join(kept)):
            keys.add(stripped)
    return frozenset(keys)


def build_alias_index(names) -> tuple[dict[str, str], dict[str, str]]:
    """(exact, stripped) lookups over gol.gg's own spellings.

    `exact` maps a full normalized spelling to its name and is NEVER pruned --
    an identical spelling is identity, not an inference. That separation is
    load-bearing: gol.gg carries BOTH "Gen.G" and "Gen.G Esports", so the
    stripped form of the latter collides with the exact spelling of the former.
    Pruning that collision out of a single combined index made plain "Gen.G"
    unresolvable, which is worse than the gap being closed.

    `stripped` holds only corporate-token-stripped forms, and drops any key
    that (a) two different teams share, or (b) already names a different team
    exactly. Both are the "two orgs differing only by a corporate token" case.
    """
    exact: dict[str, str] = {}
    owners: dict[str, set[str]] = {}
    candidates: dict[str, str] = {}
    for name in names:
        base = base_key(name)
        if not base:
            continue
        exact.setdefault(base, name)
        for key in alias_keys(name) - {base}:
            owners.setdefault(key, set()).add(base)
            candidates.setdefault(key, name)
    stripped = {
        k: candidates[k] for k, bases in owners.items()
        if len(bases) == 1 and (k not in exact or base_key(exact[k]) in bases)
    }
    return exact, stripped


def resolve(name: str | None, index: tuple[dict[str, str], dict[str, str]]) -> str | None:
    """The gol.gg name for `name`, or None if unknown or ambiguous."""
    exact, stripped = index
    base = base_key(name)
    if not base:
        return None
    if base in exact:
        return exact[base]
    # Try this name's own stripped forms against gol.gg's exact spellings
    # (INTZ e-Sports -> INTZ), then against gol.gg's stripped forms
    # (PCIFIC -> PCIFIC Esports). Longest key first so the least aggressive
    # transformation wins.
    for key in sorted(alias_keys(name), key=len, reverse=True):
        hit = exact.get(key) or stripped.get(key)
        if hit is not None and tier_of(hit) == tier_of(name):
            return hit
    return None

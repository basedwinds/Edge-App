"""Resolve a market's team name onto the spelling that owns its Elo history,
WITHIN one league's rating pool.

THE GAP THIS CLOSES. Polymarket's league-title futures write full club names
while football-data writes short ones, and nothing bridged them:

    "1. FC Kaiserslautern"   vs  "kaiserslautern"
    "IFK Goteborg"           vs  "goteborg"
    "FC St. Pauli"           vs  "st pauli"
    "SpVgg Greuther Furth"   vs  "greuther furth"
    "Djurgardens IF"         vs  "djurgarden"

Measured 2026-08-12 across four leagues, only 31 of 64 teams matched: SC0 12/12,
T1 13/18, SWE1 5/16, D2 1/18. Those rows priced as nothing.

WHY NOT JUST EXTEND TEAM_ALIASES. It already holds 234 hand-written entries and
is the right tool for genuine renames ("Man United" -> "Manchester United").
This is a different problem: a mechanical CLUB-FORM difference, repeating across
every league, that would cost ~33 hand-written entries per league across two
dozen leagues and go stale on promotion/relegation. Same reasoning that made
team_name_resolver derive esports aliases rather than list them.

WHY THIS IS SAFE, given a wrong alias pays out the wrong side:

  * It resolves ONLY within a single league's pool -- ~20 candidates, not
    thousands -- so collisions are rare by construction and detectable.
  * A match must be UNIQUE. Two candidates means refuse, never pick one. This
    is the [[project_soccer_team_name_aliases]] lesson: a unique token match is
    not automatically a safe one, so uniqueness is the floor, not the proof.
  * It never invents a rating. An unresolved name returns None and the row stays
    unpriced, which is the same safe failure the pool had before.
  * Nothing here strips a TIER marker, so a reserve/academy side can never
    collapse into its parent (see RESERVED_TOKENS).
"""
from __future__ import annotations

import re
import unicodedata

# Club-form noise: legal forms, sporting-club abbreviations and founding years.
# These say what KIND of organisation it is, never WHICH one, so dropping them
# cannot merge two different clubs on its own -- and the uniqueness rule below
# is what catches the cases where it would.
CLUB_FORM_TOKENS = {
    # generic / legal
    "fc", "cf", "afc", "sc", "ac", "ss", "as", "cd", "ca", "cs", "sv", "tsv",
    "vfl", "vfb", "fsv", "msv", "sd", "ud", "rc", "rcd", "sd", "ce", "ec",
    "club", "clube", "club", "deportivo", "deportiva", "atletico", "athletic",
    # nordic / baltic
    "if", "if's", "aif", "ifk", "bk", "sk", "fk", "gif", "iff", "ff", "fh",
    # german
    "spvgg", "spvg", "tus", "tsg", "sg", "svg", "borussia",
    # anglophone
    "united", "city", "town", "rovers", "wanderers", "albion", "athletic",
    # spanish/portuguese/italian common
    "cp", "sp", "ssd", "us", "usd", "asd", "ssc",
}

# NEVER stripped: these distinguish a REAL, SEPARATE side from its parent club.
# "Austria Wien II" and "Austria Wien" are different teams with different
# results, and collapsing them is exactly the parent/academy merge that the
# esports resolver's own docstring exists to prevent.
RESERVED_TOKENS = {
    "ii", "b", "iii", "u19", "u20", "u21", "u23", "reserves", "reserve",
    "academy", "youth", "junior", "juniors", "amateure", "amateur", "sub20",
}

_PUNCT = re.compile(r"[^a-z0-9 ]")
_WS = re.compile(r"\s+")

# NFKD CANNOT DECOMPOSE THESE, so encode("ascii", "ignore") silently DELETES
# them rather than transliterating. Measured: "Brondby" with a slashed o folded
# to "brndby" and "Sonderjyske" to "snderjyske", neither of which matches
# anything -- while accented forms like Gaztepe/Basaksehir folded correctly, so
# the failure looked arbitrary until the two classes were separated.
#
# These are struck/ligature letters, not accented ones: the diacritic is part of
# the glyph, so there is no combining character to strip. They have to be
# mapped by hand. Scandinavian and Central European club names are full of them.
_TRANSLITERATE = str.maketrans({
    "ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "å": "a", "Å": "a",
    "đ": "d", "Đ": "d", "ð": "d", "Ð": "d", "ł": "l", "Ł": "l",
    "ß": "ss", "þ": "th", "Þ": "th", "œ": "oe", "Œ": "oe",
    "ı": "i", "İ": "i", "ħ": "h", "ŀ": "l",
})


def _fold(name: str) -> str:
    t = (name or "").translate(_TRANSLITERATE)
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    t = _PUNCT.sub(" ", t)
    return _WS.sub(" ", t).strip()


def core_tokens(name: str) -> frozenset[str]:
    """Identity-bearing tokens: folded, club-form and bare years dropped.

    Returns the FULL folded token set when stripping would empty it -- a club
    genuinely called "City" must not fold to nothing and then match everything.
    """
    tokens = [t for t in _fold(name).split() if t]
    kept = [t for t in tokens
            if t not in CLUB_FORM_TOKENS and not (t.isdigit() and len(t) == 4)]
    return frozenset(kept or tokens)


def _has_reserved(name: str) -> bool:
    return bool(RESERVED_TOKENS & frozenset(_fold(name).split()))


def resolve_to_pool(name: str, pool: list[str] | set[str]) -> str | None:
    """The one pool spelling this market name denotes, or None.

    Tried in order, stopping at the first tier that yields EXACTLY ONE
    candidate -- so a weaker rule can never override a stronger one:

      1. exact folded string
      2. identical core-token sets      ("Djurgardens IF" ~ "djurgarden"? no --
                                         that one falls to 3)
      3. one core is a subset of the other, which is what actually bridges
         "1. FC Kaiserslautern" -> "kaiserslautern"
      4. singular/plural-insensitive subset, for "Djurgardens" vs "Djurgarden"

    A reserve/academy side only ever matches another reserve/academy side, in
    both directions, so a parent can never absorb its own B team.
    """
    if not name:
        return None
    candidates = [p for p in pool if p]
    if not candidates:
        return None

    folded = _fold(name)
    exact = [p for p in candidates if _fold(p) == folded]
    if len(exact) == 1:
        return exact[0]

    want_reserved = _has_reserved(name)
    eligible = [p for p in candidates if _has_reserved(p) == want_reserved]
    if not eligible:
        return None

    mine = core_tokens(name)
    if not mine:
        return None

    same = [p for p in eligible if core_tokens(p) == mine]
    if len(same) == 1:
        return same[0]

    subset = [p for p in eligible
              if (core_tokens(p) <= mine or mine <= core_tokens(p))]
    if len(subset) == 1:
        return subset[0]

    # Nordic clubs decline the town name ("Djurgardens" vs "Djurgarden",
    # "Mjallby" vs "Mjallbys"). Only reached when the stricter tiers were
    # ambiguous or empty, and still required to be unique.
    def _stem(tokens: frozenset[str]) -> frozenset[str]:
        return frozenset(t[:-1] if len(t) > 4 and t.endswith("s") else t for t in tokens)

    mine_s = _stem(mine)
    loose = [p for p in eligible
             if (_stem(core_tokens(p)) <= mine_s or mine_s <= _stem(core_tokens(p)))]
    if len(loose) == 1:
        return loose[0]
    return None

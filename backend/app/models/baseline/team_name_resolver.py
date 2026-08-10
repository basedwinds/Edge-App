"""Resolves a market's spelling of an esports team onto the spelling that owns
its match history.

THE PROBLEM. Elo lookups are exact-string, and the market feed and the match
feed do not agree on how a team is written. So a market's spelling can carry a
rating built from almost no games while an equivalent spelling holds the real
history -- measured on Valorant: "Fokus" 0 games vs "FOKUS" 106, "Stallions" 0 vs
"Stallions Esports" 21, "Leviatan" 0 vs "LEVIATAN" 96, "Gen.G Esports" 3 vs
"Gen.G" 100. On a fixed team set, 32 of 127 active market teams could not form a
rated pair with ANYONE for this reason alone.

WHAT THIS DOES NOT DO: merge histories. Ratings are still trained from raw names
exactly as before; only the READ resolves. Three guards keep that safe:

  * a name that already has MIN_GAMES of its own history is never redirected, so
    no established team can be repointed at another team's rating;
  * the target must itself clear MIN_GAMES;
  * a key claimed by two or more viable targets resolves to NOTHING. Ambiguity is
    dropped rather than guessed -- the discipline lol_team_aliases.py already
    documents, for the same reason: an unresolved name costs one unpriced
    market, a wrong one prices a bet off another team's strength.

A BLANKET NORMALISE-AND-MERGE WAS TRIED AND REJECTED ON THE DATA. Two things
break it. ASCII-folding collapses names with no Latin characters (15 Korean,
Thai and Chinese teams, plus "^-^") to the EMPTY STRING, which would merge
fifteen unrelated teams into one -- hence the empty-key guard. And suffix
stripping alone pairs "BE esports" with "Be Jung-sang", and "EDward Gaming"
(119 games) with "Edward Esports" (2). A "a team cannot play itself" check was
tried as a safety net and rejected NONE of them: two different teams that never
happened to meet still pass. The game-count asymmetry is what actually makes the
redirect safe.

Keyed by NAME KEY rather than by observed source spelling, deliberately. An
earlier version mapped source->target across names seen in MATCHES and silently
did nothing at all, because the spellings that need resolving are the ones the
MARKETS use and those often appear in no match.
"""
import re
import unicodedata

# One trailing corporate token is dropped. Order matters only in that the
# longest match is tried first (see _name_key).
CORPORATE_SUFFIXES = ("esports club", "e sports", "esports", "gaming", "club")


def name_key(name: str) -> str:
    """Orthographic key: accents folded, case dropped, punctuation collapsed, one
    trailing corporate token removed.

    Returns "" for a name with no ASCII content. Callers MUST treat an empty key
    as unusable -- every CJK/Thai name folds to it, and grouping on it would
    merge every one of them together.
    """
    t = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    for suffix in sorted(CORPORATE_SUFFIXES, key=len, reverse=True):
        if t.endswith(" " + suffix):
            return t[: -len(suffix) - 1].strip()
    return t


def build_canonical_by_key(match_counts: dict[str, int], min_games: int) -> dict[str, str]:
    """{orthographic key: the one spelling under it that owns the match history}.

    A key is usable only when EXACTLY ONE spelling clears `min_games`. Zero
    targets, or two plausible ones, resolve to nothing.
    """
    by_key: dict[str, list[str]] = {}
    for name in match_counts:
        key = name_key(name)
        if key:
            by_key.setdefault(key, []).append(name)
    out: dict[str, str] = {}
    for key, names in by_key.items():
        strong = [n for n in names if match_counts.get(n, 0) >= min_games]
        if len(strong) == 1:
            out[key] = strong[0]

    # ALSO INDEX A SPACE-FREE KEY. name_key collapses punctuation INTO spaces
    # but keeps them, so "The Mongolz" and "TheMongolz" land on different keys
    # and never resolve to each other. That was survivable while both spellings
    # carried their own rating; once team_name_folding merges the pool onto one
    # spelling, a lookup on the other returns None -- an UNRATED team on a
    # market we can price, which is strictly worse than the split it replaced.
    # Confirmed live 2026-08-10: 'TheMongolz' and 'Nongshim Red Force' both went
    # to None after the merge, and "Nongshim Red Force" is the spacing an
    # exchange is likely to list.
    #
    # Added as a SECOND index rather than by changing name_key, because the
    # corporate-suffix strip matches on " " + suffix and would silently stop
    # working if spaces were removed there. Only registered where it is
    # unambiguous -- if two different canonicals collapse to the same
    # space-free key, neither is indexed.
    spaceless: dict[str, set[str]] = {}
    for key, canonical in out.items():
        spaceless.setdefault(key.replace(" ", ""), set()).add(canonical)
    for key, canonicals in spaceless.items():
        if key and key not in out and len(canonicals) == 1:
            out[key] = next(iter(canonicals))
    return out


def resolve(team: str, match_counts: dict[str, int], canonical_by_key: dict[str, str],
            min_games: int) -> str:
    """The spelling that owns this team's history, or the input unchanged."""
    if not team:
        return team
    if match_counts.get(team, 0) >= min_games:
        return team              # already has its own real history -- never redirect
    key = name_key(team)
    # Try the spaced key first, then the space-free one. BOTH halves are needed:
    # build_canonical_by_key indexes the space-free key, but the QUERY still
    # folds to a spaced key, so "Nongshim Red Force" would look up
    # "nongshim red force" and miss the "nongshimredforce" entry sitting right
    # there. Caught by testing the lookup rather than the index.
    return canonical_by_key.get(key) or canonical_by_key.get(key.replace(" ", "")) or team


def count_appearances(matches, team_a_key: str = "team_a", team_b_key: str = "team_b",
                      winner_key: str = "winner") -> dict[str, int]:
    """Settled appearances per spelling. That asymmetry is what makes the
    redirect safe, so unsettled matches are deliberately not counted."""
    counts: dict[str, int] = {}
    for m in matches:
        if m.get(winner_key) is None:
            continue
        for side in (team_a_key, team_b_key):
            name = m.get(side)
            if name:
                counts[name] = counts.get(name, 0) + 1
    return counts

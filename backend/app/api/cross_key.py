"""The canonical cross-platform identity of one soccer GAME proposition.

WHY THIS MODULE EXISTS. The frontend's crossPlatformKey() builds a game row's
key from the RAW team label, so two books spelling one club differently produce
two keys for one proposition -- it survives the cross-platform collapse, shows
up twice on the board, and can be staked twice. Measured on the live board
2026-08-24: 483 duplicate rows across 130 fixtures in 7 leagues, every one of
them cross-platform ("Fulham"/"Fulham FC", "Malaga"/"Malaga CF", "PSG"/"Paris
Saint-Germain FC"). Zero were same-book, so nothing here can collapse two real
ladder rungs from one exchange.

Only the backend can canonicalise -- the club alias map lives here and is far
too large to mirror in TypeScript -- which is why the futures rows already emit
`cross_key` for the frontend to prefer (schemas.py). This is that same
mechanism extended to game rows, and it is a SHARED function rather than a
second copy because the format has to agree between the board route and
placed_bets._cross_platform_key exactly. Three independent transcriptions of
one format string is how the whitespace and fixture-key mismatches happened.

SAFETY, MEASURED BEFORE SHIPPING. The danger is merging the two SIDES of a
match. Checked across every fixture on the live board: zero had
canonical(home) == canonical(away). The 55 pairs that are not simple suffix
variants were reviewed by hand and are all one club under two conventions
("QPR"/"Queens Park Rangers FC", "Bilbao"/"Athletic Club", "Real Racing Club"/
"Santander"). See memory project_game_market_duplicate_measured.
"""


def fmt_line(v) -> str:
    """Match JS `${line}`: whole numbers drop the decimal (4.0 -> '4'),
    fractional keep it (4.5 -> '4.5'); None -> '' (JS `line ?? ''`).

    Duplicated from placed_bets._fmt_line deliberately -- that one is part of the
    byte-identical contract with the frontend for EVERY sport and must not start
    importing from a soccer-flavoured module."""
    if v is None:
        return ""
    return str(int(v)) if float(v) == int(v) else str(v)


def soccer_game_cross_key(match_id, market_type, team, line, side) -> str:
    """Same shape as the frontend's game branch --
    `gameId|marketType|team|line|side` with soccer's bare match id -- but with
    the club name canonicalised so both books land on one key.

    Returns "" when there is no match id, so callers fall back to their existing
    key rather than grouping every id-less row together.
    """
    if not match_id:
        return ""
    # Imported lazily: market_matcher_soccer pulls in the ingestion package, and
    # placed_bets already takes this import inside its function for that reason.
    from app.ingestion.market_matcher_soccer import canonical_team_key

    canon = canonical_team_key((team or "").strip())
    return f"{match_id}|{market_type or ''}|{canon}|{fmt_line(line)}|{side or ''}"


# FUTURES WITH NO IDENTITY OF THEIR OWN.
#
# The futures cross-platform key is `sport|market_type|team ?? label`. A handful
# of futures carry NO team AND no group_label, so that key degenerates and can
# never match anything -- user-reported 2026-08-25: a recommended "any team to
# win 15+ games" row could not be cleared by marking it placed, because the
# board row keyed on `nfl|wins_any|None` while the placed bet keyed on
# `nfl|wins_any|NFL wins_any` (its stored label). The frontend made it worse by
# including `line` in its own futures key while the backend deliberately
# excludes it, so the two disagreed twice over.
#
# MEASURED BEFORE NARROWING THE FIX. Across every active market: 14,300 carry no
# team, but only 84 ALSO lack a group_label AND any link id. Of those 84, 81 are
# MMA/MLB GAME markets that failed to link to their fight or game -- a separate
# unlinked-market problem, already its own health-check category. Exactly ONE
# genuine futures family is left, and it is a nested ladder:
#
#     nfl wins_any   lines 15.0 / 16.0 / 17.0, side "over"
#
# The line is EXCLUDED from the identity on purpose: these rungs are nested, not
# independent. Any team winning 16+ has by definition won 15+, so they are one
# proposition at different thresholds and must collapse to a single row and
# clear together when one is placed.
#
# DELIBERATELY NOT a broad rule. mvp / dpoy / opoy / coach_of_year and the
# season-stat leaders are also team-less, but carry the PLAYER NAME in
# group_label -- 16 distinct MVP candidates on the live board. Keying those on
# sport|market_type would merge every candidate into one row.
TEAMLESS_LADDER_FUTURES = {("nfl", "wins_any")}


def teamless_ladder_cross_key(sport, market_type) -> str:
    """Stable identity for a futures ladder that has no team and no label.

    Returns "" when this is not such a market, so callers fall through to their
    existing key rather than collapsing unrelated rows together.
    """
    if (sport or "", market_type or "") in TEAMLESS_LADDER_FUTURES:
        return f"{sport}|{market_type}"
    return ""

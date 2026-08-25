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

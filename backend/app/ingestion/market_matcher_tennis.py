"""Matches Kalshi/Polymarket Tennis markets to this app's TennisMatch rows.
Parallel to market_matcher_mma.py, but a genuinely different name-matching
problem: ufcstats/Kalshi/Polymarket all use REAL FULL fighter names for
MMA, but Tennis's two historical sources (tennis-data.co.uk,
tennisexplorer.com) render players as "Surname I." (confirmed live
2026-07-18 on both), while Kalshi/Polymarket's live markets use full names
("Andrey Rublev", "Matyas Fule vs Federico Bondioli" -- confirmed live from
real open markets). A plain token-subset match (MMA's approach) doesn't
work across an abbreviated-vs-full-name pair.

Reuses the same surname+initial matching shape the user's separate,
earlier Kalshi/Polymarket tennis research project already proved out for
this exact problem (not ported -- re-implemented here since that was a
different, standalone codebase) -- including its documented fallback for a
compound surname getting truncated by one source (e.g. "Merida Aguilar D."
vs. a live market's "Daniel Merida": requires only the PRIMARY (first)
surname token to match, not every token, at a discounted-confidence primary
match). Known, accepted simplification, not fixed here: two different
players sharing a surname + first initial within the same tour/gender
collide -- same category of tradeoff as this app's NFL backup-QB name
matching.
"""
from __future__ import annotations

import re
import unicodedata


def _fold(name: str) -> str:
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z ]", " ", stripped.lower()).strip()


def _abbreviated_surname_and_initial(abbreviated_name: str) -> tuple[str, str] | None:
    """"Rublev A." -> ("rublev", "a"). None if the name doesn't have the
    expected "Surname(s) I." shape (defensive -- not expected in practice
    since both historical sources render names this way consistently)."""
    folded = _fold(abbreviated_name)
    tokens = folded.split()
    if len(tokens) < 2:
        return None
    *surname_tokens, initial = tokens
    if not surname_tokens or not initial:
        return None
    return " ".join(surname_tokens), initial[0]


def full_name_matches_abbreviated(full_name: str, abbreviated_name: str) -> bool:
    """full_name: a live market's real full player name ("Andrey Rublev").
    abbreviated_name: this app's TennisMatch-shaped "Surname I." key source
    (e.g. player_a_name/player_b_name from app/ingestion/tennis_data.py)."""
    full_tokens = _fold(full_name).split()
    if not full_tokens:
        return False
    abbrev = _abbreviated_surname_and_initial(abbreviated_name)
    if abbrev is None:
        return False
    abbrev_surname, abbrev_initial = abbrev

    full_first_initial = full_tokens[0][0]
    full_surname_tokens = full_tokens[1:]
    if not full_surname_tokens:
        return False
    full_surname = " ".join(full_surname_tokens)

    if full_first_initial != abbrev_initial:
        return False
    if full_surname == abbrev_surname:
        return True
    # Compound-surname fallback: require only the PRIMARY (first) surname
    # token to match, at a discounted confidence -- same tradeoff as the
    # separate Kalshi/Polymarket tennis project's own documented fix for
    # "Merida Aguilar D." vs "Daniel Merida" (one source truncates a
    # multi-word surname, the other doesn't).
    return full_surname_tokens[0] == abbrev_surname.split()[0]


def full_names_match(name_a: str, name_b: str) -> bool:
    """Both sides are real FULL names here (e.g. matching a fresh Polymarket
    listing against a TennisMatch row this app already created FROM an
    earlier Kalshi listing -- see market_catalog_tennis.py's docstring on
    why live-created rows store full names, not the "Surname I." shape
    tennis-data.co.uk/tennisexplorer use). Plain token-subset match, same
    shape as market_matcher_mma.py::fighter_names_match -- this is NOT the
    same problem full_name_matches_abbreviated solves (one abbreviated, one
    full), so it needs its own function rather than reusing that one."""
    tokens_a, tokens_b = set(_fold(name_a).split()), set(_fold(name_b).split())
    if not tokens_a or not tokens_b:
        return False
    if tokens_a == tokens_b:
        return True
    return tokens_a.issubset(tokens_b) or tokens_b.issubset(tokens_a)


def _match_pair(match: dict, name_a: str, name_b: str) -> bool:
    m_a, m_b = match["player_a_name"], match["player_b_name"]
    return (
        (full_names_match(name_a, m_a) and full_names_match(name_b, m_b))
        or (full_names_match(name_a, m_b) and full_names_match(name_b, m_a))
    )


def full_name_to_abbreviated_key(full_name: str) -> str | None:
    """"Andrey Rublev" -> "rublev a." -- reconstructs the SAME normalized
    key app/ingestion/tennis_data.py::normalize_player_key() would produce
    from tennis-data.co.uk/tennisexplorer's own "Surname I." rendering, so a
    live market's full player name can look up that player's offline-trained
    Elo rating. Assumes the surname is every token after the first (correct
    for the common case; tennis-data.co.uk generally keeps a player's real
    surname in full, only the GIVEN name gets abbreviated to an initial --
    see market_matcher_tennis.py's module docstring for the compound-surname
    edge case this can still miss)."""
    tokens = _fold(full_name).split()
    if len(tokens) < 2:
        return None
    given, *surname_tokens = tokens
    return f"{' '.join(surname_tokens)} {given[0]}."


_KALSHI_TENNIS_SERIES_PREFIXES = (
    "KXATPMATCH-", "KXWTAMATCH-", "KXATPCHALLENGERMATCH-", "KXWTACHALLENGERMATCH-",
    "KXITFMATCH-", "KXITFWMATCH-",
    "KXATPSETWINNER-", "KXWTASETWINNER-", "KXATPGSPREAD-", "KXATPGTOTAL-", "KXATPEXACTMATCH-",
    # KXWTAGTOTAL launched after the original ATP-only survey (re-probed
    # 2026-08-11). It MUST be listed here as well as in the client: the client
    # decides what gets fetched, this tuple decides what can be joined to a
    # fixture, and a series present in one but not the other ingests rows that
    # resolve to nothing.
    "KXWTAGTOTAL-",
)


def kalshi_match_suffix(event_ticker: str) -> str | None:
    """"KXATPMATCH-26JUL19RUBDAR" -> "26JUL19RUBDAR". Same date+player-code
    suffix is shared across every series for the same real match (confirmed
    live -- moneyline, set winner, game spread/total, and exact-match-score
    events all carry the identical suffix for the same match), same "cross-
    series join key" role as market_matcher_mma.py::kalshi_fight_suffix.
    KXATPSETWINNER's own event ticker appends a further "-{set_number}"
    segment AFTER this suffix (one event per set, not one per match) --
    strip that separately, see kalshi_tennis_client.py's set-winner parser."""
    for prefix in _KALSHI_TENNIS_SERIES_PREFIXES:
        if event_ticker.startswith(prefix):
            return event_ticker[len(prefix):]
    return None


def kalshi_set_winner_event_parts(event_ticker: str) -> tuple[str, int] | None:
    """KXATPSETWINNER/KXWTASETWINNER structure a real Kalshi quirk: ONE EVENT
    PER SET, not one event per match (confirmed live: "KXATPSETWINNER-
    26JUL19RUBDAR-2" is set 2 of that match, a SEPARATE event from set 1's
    "KXATPSETWINNER-26JUL19RUBDAR-1"). Returns (match_suffix, set_number) or
    None if the ticker doesn't match the expected shape."""
    for prefix in ("KXATPSETWINNER-", "KXWTASETWINNER-"):
        if event_ticker.startswith(prefix):
            rest = event_ticker[len(prefix):]
            if "-" not in rest:
                return None
            match_suffix, set_str = rest.rsplit("-", 1)
            if not set_str.isdigit():
                return None
            return match_suffix, int(set_str)
    return None


def match_upcoming_tennis_match(
    player_a_name: str, player_b_name: str, upcoming_matches: list[dict],
) -> dict | None:
    """upcoming_matches: TennisMatch-shaped dicts (real name fields, not yet
    played) -- see app/ingestion/market_catalog_tennis.py for how these get
    populated live. No date filter needed, same reasoning as
    match_fight_by_names_only: this app only ever holds a small number of
    genuinely upcoming matches at once, not a full historical archive."""
    if not player_a_name or not player_b_name:
        return None
    for match in upcoming_matches:
        if _match_pair(match, player_a_name, player_b_name):
            return match
    return None

"""Matches Kalshi/Polymarket UFC markets to this app's ufcstats.com-sourced
MmaFight rows (see app/ingestion/ufc_data.py). Parallel to
market_matcher_mlb.py/market_matcher_nba.py, but a structurally different
matching problem: there's no fixed ~30-team roster to build an abbreviation
map from -- thousands of unique fighters, constantly changing. Matching is
by NORMALIZED FIGHTER NAME + event date instead.

Confirmed live 2026-07-17: both platforms and ufcstats.com use each
fighter's real full name (not a nickname or abbreviation) -- "Dricus Du
Plessis"/"Kamaru Usman" match verbatim (modulo case/whitespace) across
ufcstats' fighter pages, Kalshi's yes_sub_title fields, and Polymarket's
outcome labels for the same real card. Accent-folding is still applied
defensively (unicodedata NFKD strip) since UFC's international roster
includes plenty of diacritics (e.g. "Rodolfo Bellato").

Kalshi's own event-ticker SUFFIX (e.g. "26JUL18DUUSM") is shared across
every series for the same fight (KXUFCFIGHT-26JUL18DUUSM,
KXUFCDISTANCE-26JUL18DUUSM, KXUFCMOV-26JUL18DUUSM, ...) -- confirmed live --
so it's used as the cross-series join key within a single Kalshi refresh
cycle, resolved to real fighter names via KXUFCFIGHT's own yes_sub_title
fields (see kalshi_mma_client.py), rather than trying to decode the
suffix's ad-hoc abbreviation scheme directly.
"""
import re
import unicodedata

KALSHI_SERIES_PREFIXES = (
    "KXUFCFIGHT-", "KXUFCDISTANCE-", "KXUFCMOV-", "KXUFCMOF-",
    "KXUFCROUNDS-", "KXUFCVICROUND-",
)


def normalize_fighter_name(name: str) -> str:
    if not name:
        return ""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    folded = re.sub(r"[^a-z0-9 ]", "", folded.lower())
    return re.sub(r"\s+", " ", folded).strip()


def fighter_names_match(name_a: str, name_b: str) -> bool:
    """Real bug caught via live testing (2026-07-17): an exact normalized
    match drops real fights (e.g. Kalshi's "Christian Duncan" vs ufcstats'
    "Christian Leroy Duncan"; "Levi Rodrigues Jr" vs "Levi Rodrigues Jr.") --
    platforms and ufcstats don't always agree on whether to include a middle
    name or a Jr./Sr. suffix. Falls back to token-SUBSET matching (one
    name's word set contained in the other's) after an exact check, which
    handles a dropped middle name/suffix without risking a false match
    between two DIFFERENT people who merely share a last name (a bare
    last-name-only fallback would risk exactly that on a crowded card)."""
    tokens_a = set(normalize_fighter_name(name_a).split())
    tokens_b = set(normalize_fighter_name(name_b).split())
    if not tokens_a or not tokens_b:
        return False
    if tokens_a == tokens_b:
        return True
    return tokens_a.issubset(tokens_b) or tokens_b.issubset(tokens_a)


def kalshi_fight_suffix(event_ticker: str) -> str | None:
    """"KXUFCFIGHT-26JUL18DUUSM" -> "26JUL18DUUSM". None if the ticker
    doesn't match a known UFC series prefix (defensive, not expected to
    silently swallow a real parsing failure elsewhere)."""
    for prefix in KALSHI_SERIES_PREFIXES:
        if event_ticker.startswith(prefix):
            return event_ticker[len(prefix):]
    return None


def _fight_matches_pair(fight: dict, fighter_a_name: str, fighter_b_name: str) -> bool:
    a, b = fight["fighter_a_name"], fight["fighter_b_name"]
    return (
        (fighter_names_match(fighter_a_name, a) and fighter_names_match(fighter_b_name, b))
        or (fighter_names_match(fighter_a_name, b) and fighter_names_match(fighter_b_name, a))
    )


def match_fight(
    fighter_a_name: str,
    fighter_b_name: str,
    event_date: str | None,
    fights_by_date: dict[str, list[dict]],
) -> dict | None:
    """fights_by_date: MmaFight-shaped dicts (see ufc_data.py) grouped by
    event_date (ISO string). Tries the exact date first, then +/-1 day
    (real-world posting/timezone slack between a platform's listed date and
    ufcstats' own event_date), same tolerance category as this app's other
    sports' date-based joins."""
    if not fighter_a_name or not fighter_b_name or not event_date:
        return None

    candidate_dates = [event_date]
    try:
        import datetime as dt
        base = dt.date.fromisoformat(event_date)
        candidate_dates += [(base + dt.timedelta(days=d)).isoformat() for d in (-1, 1)]
    except ValueError:
        pass

    for d in candidate_dates:
        for fight in fights_by_date.get(d, []):
            if _fight_matches_pair(fight, fighter_a_name, fighter_b_name):
                return fight
    return None


def match_fight_by_names_only(
    fighter_a_name: str,
    fighter_b_name: str,
    all_fights: list[dict],
) -> dict | None:
    """No date filter -- safe here because this app's MmaFight table only
    ever holds a handful of scheduled-ahead cards (~60-90 fights, see
    poller_mma.py::refresh_mma_fights) at once, unlike NFL/NBA/MLB's much
    larger always-loaded schedules where a date filter is needed to avoid
    ambiguity. Used by poller_mma.py so both Kalshi (no reliable per-market
    event date) and Polymarket clients can resolve a fight_id the same way."""
    if not fighter_a_name or not fighter_b_name:
        return None
    for fight in all_fights:
        if _fight_matches_pair(fight, fighter_a_name, fighter_b_name):
            return fight
    return None


def group_fights_by_date(fights: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for f in fights:
        if f.get("event_date"):
            grouped.setdefault(f["event_date"], []).append(f)
    return grouped

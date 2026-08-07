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


def _spaceless(name: str) -> str:
    return normalize_fighter_name(name).replace(" ", "")


def _edit_distance_le1(a: str, b: str) -> bool:
    """True if a and b differ by at most one substitution/insertion/deletion.
    Cheap early-outs only -- never builds the full DP table."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    short, long = (a, b) if la < lb else (b, a)
    for i in range(len(long)):
        if long[:i] + long[i + 1:] == short:
            return True
    return False


def fighter_names_match_loose(name_a: str, name_b: str) -> bool:
    """Deliberately weaker than fighter_names_match, and NEVER used on its own --
    only inside the uniqueness-gated second pass below.

    Covers the three real mismatch shapes found live 2026-08-06 on the UFC 329/330
    cards, which between them left 48 Kalshi markets unlinked:

      * surname-particle spacing -- Kalshi "Yadier Delvalle" vs ufcstats
        "Yadier del Valle". Caught by comparing the whitespace-stripped
        normalisations, which is still an EXACT string equality, so it carries
        essentially no false-match risk.
      * transliteration drift + a dropped patronymic -- Kalshi "Myktybek Orolbay
        Uulu" vs ufcstats "Myktybek Orolbai" (y/i, and "Uulu" absent).
      * a diminutive standing in for the legal first name -- Kalshi "Giovanna
        Canuto"/"Caroline Foro Antunes" vs ufcstats "Gigi Canuto"/"Carol Foro".
        No normalisation reaches Gigi<-Giovanna; the only thing the two records
        share is the surname.

    So the last two tiers fall back to a single shared distinctive token. That is
    far too weak a test to trust by itself -- "Silva" alone would match half a
    Brazilian card -- which is exactly why match_fight_by_names_only requires
    BOTH fighters to hit AND requires the qualifying fight to be unique in the
    whole table before it will use this.
    """
    la = normalize_fighter_name(name_a).split()
    lb = normalize_fighter_name(name_b).split()
    if not la or not lb:
        return False
    if _spaceless(name_a) == _spaceless(name_b):
        return True

    # SURNAME TOKENS ONLY -- i.e. everything but the first token of each name.
    # This restriction is not cosmetic. Allowing the first token in made the rule
    # match on GIVEN names, and a cross-pair sweep over the whole table
    # (132 fights, 8,646 synthetic pairs) turned up 12 wrong resolutions, every
    # single one of them driven by a shared or near-shared first name:
    # "Jose Montanha"/"Jose Delgado", "Kevin Borjas"/"Kevin Holland",
    # "Dustin Poirier"/"Austin Bashi". Dropping token 0 takes that to 0 while
    # keeping all three real cases, whose evidence is a surname
    # (canuto/canuto, foro/foro, orolbay/orolbai).
    sa, sb = set(la[1:]), set(lb[1:])
    if not sa or not sb:
        return False
    # A shared surname token of >=4 chars. The length floor drops the particles
    # and suffixes ("de", "da", "do", "jr", "al") that are shared by many
    # unrelated fighters and carry no identifying information.
    if any(len(t) >= 4 for t in sa & sb):
        return True
    # One-character drift on a long surname token (orolbay/orolbai).
    return any(
        len(x) >= 5 and len(y) >= 5 and _edit_distance_le1(x, y)
        for x in sa for y in sb
    )


def kalshi_fight_suffix(event_ticker: str) -> str | None:
    """"KXUFCFIGHT-26JUL18DUUSM" -> "26JUL18DUUSM". None if the ticker
    doesn't match a known UFC series prefix (defensive, not expected to
    silently swallow a real parsing failure elsewhere)."""
    for prefix in KALSHI_SERIES_PREFIXES:
        if event_ticker.startswith(prefix):
            return event_ticker[len(prefix):]
    return None


_MONTHS = {m: i + 1 for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
)}


def date_from_fight_suffix(suffix: str) -> str | None:
    """"26AUG08CANFOR" -> "2026-08-08". Kalshi's fight suffix leads with the
    card date, which is the only per-market date signal the MMA path has (the
    markets themselves carry no reliable event date). Used ONLY to narrow the
    loose second pass in match_fight_by_names_only -- never to reject a strict
    match, so a suffix that doesn't parse costs nothing."""
    m = re.match(r"^(\d{2})([A-Z]{3})(\d{2})", suffix or "")
    if not m or m.group(2) not in _MONTHS:
        return None
    return f"20{m.group(1)}-{_MONTHS[m.group(2)]:02d}-{int(m.group(3)):02d}"


def date_from_polymarket_slug(slug: str) -> str | None:
    """"ufc-hdasil-lou2-2026-08-08" -> "2026-08-08". Polymarket's UFC event
    slugs end in the card date. Same purpose and same "advisory only" status as
    date_from_fight_suffix."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})$", slug or "")
    return m.group(0) if m else None


def _fight_matches_pair(fight: dict, fighter_a_name: str, fighter_b_name: str, match=None) -> bool:
    match = match or fighter_names_match
    a, b = fight["fighter_a_name"], fight["fighter_b_name"]
    return (
        (match(fighter_a_name, a) and match(fighter_b_name, b))
        or (match(fighter_a_name, b) and match(fighter_b_name, a))
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
    event_date: str | None = None,
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

    # Second pass, reached only when the strict rules found nothing at all.
    # fighter_names_match_loose on its own is far too weak to trust, so it gets
    # two guards. First, when the caller knows the card date (Kalshi does --
    # it's the head of the fight suffix), only that card is considered; the
    # remaining wrong resolutions in the cross-pair sweep were all pairs of
    # same-surname fighters from DIFFERENT cards (Oban Elliott/Michael Oliveira
    # standing in for Tim Elliott/Ravena Oliveira), which a card restriction
    # removes outright. Second, the loose rule is only allowed to decide when it
    # picks out EXACTLY ONE fight -- two or more candidates means the evidence
    # doesn't identify a fight and we leave the markets unlinked rather than
    # guess. Unlinked markets are merely unpriced; a wrong link would price the
    # wrong fight, which is the failure that actually costs money.
    pool = all_fights
    if event_date:
        # +/-1 day for the same posting/timezone slack match_fight allows. Cards
        # are a week apart, so this never merges two of them -- it only absorbs a
        # platform listing a Saturday-night US card as the Sunday UTC date.
        window = {event_date}
        try:
            import datetime as dt
            base = dt.date.fromisoformat(event_date)
            window |= {(base + dt.timedelta(days=d)).isoformat() for d in (-1, 1)}
        except ValueError:
            pass
        pool = [f for f in all_fights if f.get("event_date") in window]
        if not pool:  # card not in the table at all -> nothing to match against
            return None
    loose = [
        f for f in pool
        if _fight_matches_pair(f, fighter_a_name, fighter_b_name, fighter_names_match_loose)
    ]
    return loose[0] if len(loose) == 1 else None


def resolve_fight_side(team: str | None, fighter_a_name: str, fighter_b_name: str) -> str | None:
    """Which side of an ALREADY-MATCHED fight a market/bet's team name refers
    to: "a", "b", or None if the name doesn't pick out exactly one.

    Once the fight is known this is a two-way choice, which is why it can safely
    fall back to fighter_names_match_loose: the loose rule's danger is picking a
    stranger out of a big pool, and here the pool is two people who are fighting
    each other. Ambiguity (a name that fits both) and silence (fits neither)
    both return None -- never a guess.

    This exists because linking a fight and reading a fighter's SIDE were two
    separate name comparisons with different strictness, so a market could be
    correctly attached to a fight and still fail to price. The moneyline pricer
    hit exactly that, and its old comment ("market.team is always one of the
    fight's two real names") stopped being true the moment the fight matcher
    learned to bridge "Yadier Delvalle" -> "Yadier del Valle".
    """
    if not team:
        return None
    for match in (fighter_names_match, fighter_names_match_loose):
        hits = [s for s, n in (("a", fighter_a_name), ("b", fighter_b_name)) if match(team, n)]
        if len(hits) == 1:
            return hits[0]
    return None


def group_fights_by_date(fights: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for f in fights:
        if f.get("event_date"):
            grouped.setdefault(f["event_date"], []).append(f)
    return grouped

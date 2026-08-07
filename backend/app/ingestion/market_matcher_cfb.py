"""Matches Kalshi KXNCAAFGAME markets to canonical (ESPN-abbreviation) college
football games -- parallel to market_matcher_wnba.py.

Two things make this EASIER and safer than the WNBA matcher, and both are used:

1. Kalshi puts the team abbreviation in the MARKET ticker's own suffix
   (KXNCAAFGAME-26SEP19MSUND-ND -> "ND"), so the two teams of an event can be
   read by grouping its markets, rather than by splitting the event ticker's
   teams-blob. That matters here: WNBA's blob-splitting works because 15 team
   codes are short and unambiguous, but "MSUND" over ~130 FBS teams has real
   ambiguity ("MS"+"UND"? "MSU"+"ND"?). Reading the suffixes removes the guess
   entirely. `split_teams_blob` is kept only as a fallback.

2. Each market carries yes_sub_title as the display name ("Notre Dame"), which
   gives a second, independent way to resolve a team when its abbreviation is
   unknown -- important because the alias table below cannot be complete for a
   130-team sport listed a few games at a time.

Abbreviation compatibility measured live 2026-08-02 against the 30 open
KXNCAAFGAME markets vs the 257 distinct ESPN abbreviations in
data/cfb_game_cache.json: 24 of 26 matched outright (92%). The two that did not
are below. Expect this table to GROW as more of the season lists -- that is why
an unknown abbreviation falls back to the display name instead of failing.
"""
import re
from datetime import date

# Kalshi abbreviation -> ESPN abbreviation. Confirmed live, not guessed:
# ESPN uses "MIZ" for Missouri and "OU" for Oklahoma (both present in the game
# cache), while Kalshi writes "MIZZ" and "OKLA".
KALSHI_TO_ESPN_ABBR = {
    "MIZZ": "MIZ",
    "OKLA": "OU",
    # NC State: found by the end-to-end link test, not by the abbreviation
    # comparison -- it only surfaced once markets were matched against the LIVE
    # ESPN schedule, because the historical cache and the current schedule spell
    # it differently.
    "NCST": "NCSU",
    # These five surfaced only on the KXNCAAFWINS ladders, where the
    # display-name fallback CANNOT help: yes_sub_title there is "9+ wins", not a
    # team name, so abbreviation resolution is the only route. Confirmed against
    # ESPN's own /teams endpoint rather than guessed. Any future series that
    # labels markets by threshold instead of team will depend on this table
    # being complete for the teams it lists.
    "BSU": "BOIS",    # Boise State
    "IND": "IU",      # Indiana
    "NW": "NU",       # Northwestern
    "SCAR": "SC",     # South Carolina
    "TXAM": "TA&M",   # Texas A&M
}

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# KXNCAAFGAME-26SEP19MSUND -> (2026, 9, 19, "MSUND")
# Kept permissive on the series prefix (KXNCAAF[A-Z]*) for the same reason the
# WNBA matcher had to be widened: when spread/total series list (KXNCAAFSPREAD,
# KXNCAAFTOTAL -- both exist but are empty today), they use the same event-suffix
# shape, and a prefix pinned to KXNCAAFGAME would silently leave every one of
# those markets unlinked, unpriceable and unsettleable.
_EVENT_TICKER_RE = re.compile(r"^KXNCAAF[A-Z]*-(\d{2})([A-Z]{3})(\d{2})([A-Z0-9]+)$")


def to_espn_abbr(kalshi_abbr: str) -> str:
    return KALSHI_TO_ESPN_ABBR.get((kalshi_abbr or "").upper(), (kalshi_abbr or "").upper())


def parse_kalshi_event_ticker(event_ticker: str):
    m = _EVENT_TICKER_RE.match(event_ticker or "")
    if not m:
        return None
    yy, mon, dd, teams = m.groups()
    month = _MONTHS.get(mon)
    if not month:
        return None
    try:
        game_date = date(2000 + int(yy), month, int(dd))
    except ValueError:
        return None
    return {"date": game_date, "teams_blob": teams}


def team_abbr_from_market_ticker(market_ticker: str) -> str | None:
    """"KXNCAAFGAME-26SEP19MSUND-ND" -> "ND". The suffix after the LAST hyphen is
    the team code; returns None if the ticker has no suffix segment."""
    if not market_ticker or "-" not in market_ticker:
        return None
    suffix = market_ticker.rsplit("-", 1)[-1].strip()
    return suffix or None


def split_teams_blob(teams_blob: str, known_abbrs: set[str]):
    """FALLBACK ONLY -- prefer team_abbr_from_market_ticker. Ambiguous over a
    130-team field, so it requires a UNIQUE split and returns None otherwise
    rather than picking the first that happens to parse."""
    matches = []
    for split_at in range(2, len(teams_blob) - 1):
        away, home = teams_blob[:split_at], teams_blob[split_at:]
        if away in known_abbrs and home in known_abbrs:
            matches.append((away, home))
    return matches[0] if len(matches) == 1 else None


def build_game_index(cfb_games: list[dict]) -> dict:
    """(season, away_abbr, home_abbr) -> [game, ...]"""
    index: dict[tuple, list[dict]] = {}
    for g in cfb_games:
        index.setdefault((g["season"], g["away_abbr"], g["home_abbr"]), []).append(g)
    return index


# Spellings no live ESPN row produces, so build_name_index below can't learn
# them. Found 2026-08-07 by resolving all 513 real Polymarket CFB team labels
# against the index: 3 distinct names failed, everything else that failed was a
# placeholder, a conference, or the "Other" bucket.
#
# Keys are ALREADY normalized the way _norm_name normalizes ("Miami (FL)" and
# "Miami FL" both fold to "miamifl", so one entry covers both spellings).
#
# "mississippirebels" is keyed on the FULL string on purpose. Ole Miss is MISS
# and Mississippi State is MSST -- aliasing the bare token "mississippi" would
# merge two different schools. The mascot is what disambiguates them (Rebels vs
# Bulldogs), so the mascot has to stay in the key. Same reasoning that killed
# the Espanyol/Barcelona and cs maritimo/madeira merges on the soccer side: a
# unique-looking token match is not automatically a safe one.
_EXTRA_NAME_ALIASES = {
    "miamifl": "MIA",              # ESPN: "Miami Hurricanes"; Miami (OH) is M-OH
    "northcarolinast": "NCSU",     # ESPN: "NC State Wolfpack"
    "northcarolinastate": "NCSU",  # the _name_variants St./State expansion
    "mississippirebels": "MISS",   # Ole Miss, NOT Mississippi State (MSST)
}


def build_name_index(cfb_games: list[dict]) -> dict:
    """Normalised display name -> ESPN abbreviation, for resolving a team whose
    Kalshi abbreviation isn't in the alias table.

    Feed this the LIVE schedule rows from espn_cfb_client.parse_event, which
    carry `home_name`/`away_name`. Do NOT feed it data/cfb_game_cache.json: that
    file's "home"/"away" fields are numeric ESPN team IDs, not names, so an index
    built from it maps "158" -> "NEB" and the fallback silently never fires.
    Found exactly that way -- the fallback looked like it was working because
    every test case happened to resolve on abbreviation alone."""
    out: dict[str, str] = dict(_EXTRA_NAME_ALIASES)
    for g in cfb_games:
        for name_key, abbr_key in (("home_name", "home_team"), ("away_name", "away_team"),
                                   ("home_short", "home_team"), ("away_short", "away_team"),
                                   ("home_name", "home_abbr"), ("away_name", "away_abbr"),
                                   ("home_short", "home_abbr"), ("away_short", "away_abbr")):
            name, abbr = g.get(name_key), g.get(abbr_key)
            if isinstance(name, str) and name and isinstance(abbr, str) and abbr:
                for variant in _name_variants(name):
                    out.setdefault(variant, abbr)
    return out


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _name_variants(name: str) -> set[str]:
    """Every normalised spelling a college name might arrive as.

    The St./State split is endemic to college sports and bit this immediately:
    Kalshi writes "Michigan St." while ESPN writes "Michigan State", which
    normalise to "michiganst" vs "michiganstate" and never match. Only a TRAILING
    "st" is expanded -- "St. John's" and "St. Bonaventure" carry it as a prefix
    meaning Saint, and rewriting those would map different schools together."""
    base = _norm_name(name)
    if not base:
        return set()
    out = {base}
    if base.endswith("state"):
        out.add(base[: -len("state")] + "st")
    elif base.endswith("st"):
        out.add(base[: -len("st")] + "state")
    return out


def resolve_team(kalshi_abbr: str | None, display_name: str | None, name_index: dict,
                 known_abbrs: set[str]) -> str | None:
    """Abbreviation first, display name second. Returns None rather than guessing
    -- an unlinked market is recoverable, a market linked to the WRONG game
    silently misprices and missettles."""
    abbr = to_espn_abbr(kalshi_abbr) if kalshi_abbr else None
    if abbr and abbr in known_abbrs:
        return abbr
    if display_name:
        for variant in _name_variants(display_name):
            hit = name_index.get(variant)
            if hit:
                return hit
    return None


def _season_for(game_date: date) -> int:
    """CFB seasons span one calendar year but run into January (bowls, playoff).
    A January game belongs to the PREVIOUS season -- e.g. the 2026-01-19 title
    game is part of the 2025 season, which is how the game cache labels it."""
    return game_date.year - 1 if game_date.month == 1 else game_date.year


def match_game(away_abbr: str, home_abbr: str, game_date: date, game_index: dict) -> str | None:
    season = _season_for(game_date)
    # Unordered pair: neutral-site games (bowls, kickoff classics) have no
    # reliable home/away ground truth between the two sources.
    candidates = (game_index.get((season, away_abbr, home_abbr), [])
                  + game_index.get((season, home_abbr, away_abbr), []))
    if not candidates:
        return None
    best = min(candidates, key=lambda g: abs((date.fromisoformat(g["date"]) - game_date).days))
    # CFB is weekly, so a real listing is within a couple of days of the true
    # date; anything further apart is a different meeting of the same two teams.
    if abs((date.fromisoformat(best["date"]) - game_date).days) > 3:
        return None
    return best["id"]

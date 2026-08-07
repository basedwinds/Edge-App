"""Matches Kalshi/Polymarket Soccer markets to this app's SoccerMatch rows.
Parallel to market_matcher_tennis.py/market_matcher_mma.py, but a THIRD
different name-matching problem: unlike Tennis (abbreviated-vs-full-name)
or MMA (both sides already render full fighter names), soccer club names
have genuine SHORTHAND/NICKNAME variants that aren't decomposable by any
truncation rule -- football-data.co.uk's own historical CSVs use short
in-house names ("Man United", "Wolves", "Spurs"), while Kalshi/Polymarket's
live listings render full or semi-full club names ("Manchester United",
"Wolverhampton Wanderers", "Tottenham") -- confirmed live 2026-07-19 from
real open Kalshi markets ("Liverpool vs Brentford", "San Jose vs Los Angeles
G" for MLS, where "Los Angeles G" is Kalshi's own disambiguation of LA
Galaxy from LAFC).

A plain token-subset match (MMA's/Tennis's full_names_match approach) fails
on these pairs outright ({"man","united"} is not a subset of {"manchester",
"united"} or vice versa) -- so this needs a hardcoded ALIAS TABLE normalized
BEFORE the token-subset comparison. This alias table is NOT exhaustive (only
the well-documented, stable EPL/football-data.co.uk shorthand set below is
covered at ship time) -- same "known, accepted gap" category as Tennis's
surname+initial collision risk or NFL's backup-QB matching: a genuinely new
club-name variant this table hasn't seen will simply fail to match and show
up as a real, visible miss (no SoccerMatch found), not a silent
mismatch -- extend TEAM_ALIASES as real gaps are found live, don't try to
guess every league's shorthand upfront."""
from __future__ import annotations

from app.ingestion.soccer_data import normalize_team_name

# football-data.co.uk's own shorthand -> a canonical full-ish name, chosen to
# match how Kalshi/Polymarket tend to render the same club (not necessarily
# the club's full legal name). Keys are ALREADY normalize_team_name()'d.
# EPL-only at ship time (the league this app's audit spent the most time
# confirming both sides' real naming) -- La Liga/Serie A/Bundesliga/Ligue 1/
# MLS entries added as real mismatches are found live, not guessed upfront.
TEAM_ALIASES: dict[str, str] = {
    "man united": "manchester united",
    "man utd": "manchester united",
    "man city": "manchester city",
    "spurs": "tottenham",
    "tottenham hotspur": "tottenham",
    "wolves": "wolverhampton wanderers",
    "nottm forest": "nottingham forest",
    "nott'm forest": "nottingham forest",
    "leicester": "leicester city",
    "newcastle": "newcastle united",
    "west brom": "west bromwich albion",
    "west ham": "west ham united",
    "brighton": "brighton hove albion",
    "sheffield united": "sheffield united",
    "wolverhampton": "wolverhampton wanderers",
    # MLS: confirmed live 2026-07-19 -- this app's own first live poller run
    # against real Kalshi/Polymarket data found these two real mismatches
    # (ESPN's own names, the training-data source, on the right):
    # Kalshi's "Los Angeles G" (its own disambiguation of Galaxy vs LAFC) and
    # Polymarket's "Los Angeles FC" both fail a plain token-subset match
    # against ESPN's "LA Galaxy"/"LAFC" outright (no shared tokens at all,
    # unlike e.g. "San Jose" vs ESPN's "San Jose Earthquakes", which already
    # matches via token-subset with NO alias needed). NOT exhaustive for
    # every MLS/other-league naming gap -- same "extend as found live" policy
    # as the EPL set above.
    # MLS: confirmed live 2026-07-19 via a systematic scan (compared every
    # real team name Kalshi/Polymarket had actually produced in this app's
    # own DB, across the first live poller run, against every team name
    # ESPN's own training cache uses) -- Kalshi in particular renders MLS
    # teams as a bare CITY name in its market titles ("Houston" not "Houston
    # Dynamo FC"), which canonical_team_key() (an EXACT alias lookup, unlike
    # the fuzzy token-subset team_names_match() uses for cross-platform
    # listing matches) cannot resolve without an explicit entry per club.
    # Every value below is ESPN's own name for that club, normalized the
    # same way canonical_team_key() normalizes everything else.
    "atlanta": "atlanta united fc",
    "austin": "austin fc",
    "charlotte": "charlotte fc",
    "chicago fire": "chicago fire fc",
    "cincinnati": "fc cincinnati",
    "colorado": "colorado rapids",
    "colorado rapids sc": "colorado rapids",
    "columbus": "columbus crew",
    "dc united sc": "dc united",
    "dallas": "fc dallas",
    "houston": "houston dynamo fc",
    "houston dynamo": "houston dynamo fc",
    "kansas city": "sporting kansas city",
    "los angeles f": "lafc",
    "los angeles fc": "lafc",
    "los angeles g": "la galaxy",
    "los angeles galaxy": "la galaxy",
    "miami": "inter miami cf",
    "minnesota": "minnesota united fc",
    "montreal": "cf montreal",
    "nashville": "nashville sc",
    "new england": "new england revolution",
    "new york city": "new york city fc",
    "new york rb": "red bull new york",
    "new york red bulls": "red bull new york",
    "orlando": "orlando city sc",
    "philadelphia": "philadelphia union",
    "portland": "portland timbers",
    "saint louis": "st louis city sc",
    "salt lake": "real salt lake",
    "san jose": "san jose earthquakes",
    "seattle": "seattle sounders fc",
    "toronto": "toronto fc",
    "vancouver": "vancouver whitecaps",
    "vancouver whitecaps fc": "vancouver whitecaps",
    # Big-5 league-winner FUTURES: confirmed live 2026-07-19 via a systematic
    # scan of every real team Kalshi's own KXPREMIERLEAGUE/KXLALIGA/KXSERIEA/
    # KXBUNDESLIGA/KXLIGUE1-27 winner markets list, compared against every
    # team football-data.co.uk's own historical top-flight cache uses. This
    # is the SAME real bug class as the MLS block above (a live market's own
    # naming doesn't byte-match the training-data source's naming) -- caught
    # here because a Poisson season Monte Carlo (season_sim_soccer.py)
    # showed the real EPL/Ligue 1 market FAVORITE (Arsenal/Man City's real
    # rival, PSG at a real 44% market price) landing at an impossible EXACT
    # 0.0 simulated-champion probability out of 3,000 seasons -- a team that
    # good landing at literally zero is a stronger tell than a merely-low
    # number, which is what made this worth chasing down as a real bug
    # rather than "the model just disagrees with the market."
    "hull city": "hull",
    "coventry city": "coventry",
    "leeds united": "leeds",
    "ipswich town": "ipswich",
    "athletic bilbao": "ath bilbao",
    "rayo vallecano": "vallecano",
    "atletico madrid": "ath madrid",
    "deportivo de la coruna": "la coruna",
    "celta vigo": "celta",
    "real sociedad": "sociedad",
    "racing santander": "santander",
    "espanyol": "espanol",
    "parma calcio": "parma",
    "frankfurt": "ein frankfurt",
    "schalke": "schalke 04",
    # Kalshi's own mangled-encoding rendering of "Mönchengladbach" (confirmed
    # live: the raw API response literally contains U+00B4 ACUTE ACCENT in
    # place of "ö", not a transcription choice on this app's side). The
    # alias KEY here is the ALREADY-NORMALIZED form ("m gladbach", with a
    # real space -- confirmed via normalize_team_name() directly, not typed
    # from how the raw string displays, since canonical_team_key() looks up
    # aliases AFTER normalizing, not before) -- football-data.co.uk's own
    # two apostrophe variants ("M'Gladbach"/"M'gladbach") already
    # canonicalize to "mgladbach" (no space) with NO alias needed, so this
    # only needs to redirect Kalshi's own broken form to that same target.
    "m gladbach": "mgladbach",
    "fc cologne": "fc koln",
    "bremen": "werder bremen",
    "strasbourg alsace": "strasbourg",
    "psg": "paris sg",
    "stade brest 29": "brest",
    "stade rennes": "rennes",
    # Kalshi lists "PSG" and "Paris" as two SEPARATE real markets (confirmed
    # live) -- football-data.co.uk's own cache also has two separate real
    # clubs, "Paris SG" and "Paris FC". Since "PSG" unambiguously means Paris
    # Saint-Germain (aliased above), Kalshi's bare "Paris" is the OTHER real
    # club by elimination, not a duplicate or a guess.
    "paris": "paris fc",
    # ---- La Liga (SP1), 2026-08-06 -------------------------------------------
    # DERIVED FROM REAL LISTINGS, not typed from football knowledge -- see
    # scripts/derive_soccer_team_aliases.py, which is kept so this can be re-run
    # for any league as new clubs appear.
    #
    # The gap these close: Polymarket lists La Liga under full official names
    # ("RC Celta de Vigo"), Kalshi under football-data.co.uk-style short names
    # ("Celta Vigo"), and the RATINGS are keyed on football-data's shortest form
    # ("celta"). With none of it bridged, 8 of 12 SP1 fixtures carrying active
    # markets had BOTH teams reading as unrated -- the league was effectively
    # unpriced -- and three fixtures had been ingested TWICE, once per platform's
    # spelling, because the same failure defeats match_upcoming_soccer_match.
    #
    # The evidence: both platforms list the SAME fixtures, so pairing them on
    # (division, date) via the side that already matches yields the other side's
    # two names as an OBSERVED pair. "Real Racing Club" is learned to be Kalshi's
    # "Santander" because its opponent Villarreal CF/Villarreal pins the fixture.
    #
    # Why that mattered more than it sounds: a plain token rule proposed
    # "rcd espanyol de barcelona" -> "barcelona" -- one candidate, unique, and
    # the WRONG CLUB, because Espanyol's official name contains its city and that
    # city is a rival club. Fixture alignment overruled it. Uniqueness alone is
    # not safety when the name contains another club's name.
    "atletico": "ath madrid",                    # Kalshi's bare "Atletico" -- ambiguous
                                                 # on its own (Madrid or Bilbao?), pinned by
                                                 # its Polymarket twin below
    "club atletico de madrid": "ath madrid",
    "ca osasuna": "osasuna",
    "deportivo alaves": "alaves",
    "elche cf": "elche",
    "getafe cf": "getafe",
    "levante ud": "levante",
    "malaga cf": "malaga",
    "rayo vallecano de madrid": "vallecano",
    "rc celta de vigo": "celta",
    "rc deportivo a coruna": "la coruna",
    "rcd espanyol de barcelona": "espanol",
    "real racing club": "santander",
    "sevilla fc": "sevilla",
    "villarreal cf": "villarreal",
    # The one entry NOT from cross-platform alignment: Real Betis is currently
    # listed by Kalshi only, so it has no twin to learn from. Accepted on four
    # independent checks instead: team_names_match("Real Betis", "Betis") ALREADY
    # returns True in shipped code (so this only makes canonical_team_key agree
    # with a judgement the module was already making), "betis" is the sole rated
    # SP1 key that is a strict token-subset of {real, betis}, it is in the
    # 2025-26 club set with 1,061 rated matches, and no other rated key contains
    # the token "betis".
    "real betis": "betis",
}


def canonical_team_key(name: str) -> str:
    """Alias-normalized canonical key for a team name -- used both here (to
    match a live listing against an existing SoccerMatch row) AND by
    elo_service_soccer.py (to look up/train a team's rating), so the SAME
    real club always hits the SAME rating-dict key regardless of which
    platform's own spelling produced it. REAL BUG this fixes (caught live
    2026-07-19, this app's own first end-to-end poller run): elo_service_soccer
    previously did a raw exact-string dict lookup with no canonicalization at
    all -- ESPN's own training data says "Houston Dynamo FC", but a live
    Polymarket listing says "Houston Dynamo" (no "FC"), so EVERY MLS team
    whose platform-rendered name didn't byte-for-byte match ESPN's own name
    silently looked like a 0-history team (falling into the NO_HISTORY_REASON
    gate) even though real training data existed for it."""
    normalized = normalize_team_name(name) or ""
    return TEAM_ALIASES.get(normalized, normalized)


def team_names_match(name_a: str, name_b: str) -> bool:
    """Alias-normalized token-subset match -- same subset shape as
    market_matcher_tennis.py::full_names_match, but through the alias table
    first so e.g. "Man United" (football-data.co.uk) and "Manchester United"
    (a live Kalshi/Polymarket listing) resolve to the identical canonical
    string before comparing tokens at all."""
    canon_a, canon_b = canonical_team_key(name_a), canonical_team_key(name_b)
    if not canon_a or not canon_b:
        return False
    if canon_a == canon_b:
        return True
    tokens_a, tokens_b = set(canon_a.split()), set(canon_b.split())
    return tokens_a.issubset(tokens_b) or tokens_b.issubset(tokens_a)


def _match_pair(match: dict, home_name: str, away_name: str) -> bool:
    return team_names_match(home_name, match["home_team"]) and team_names_match(away_name, match["away_team"])


def match_upcoming_soccer_match(
    home_team_name: str, away_team_name: str, upcoming_matches: list[dict],
) -> dict | None:
    """upcoming_matches: SoccerMatch-shaped dicts (real name fields, not yet
    played) -- see app/ingestion/market_catalog_soccer.py for how these get
    populated live. Home/away order is meaningful here (unlike Tennis's
    player_a/player_b, which has no home-field concept) -- a swapped-order
    match is NOT accepted, since that would silently mislabel which side
    gets the real home-advantage rating bump."""
    if not home_team_name or not away_team_name:
        return None
    for match in upcoming_matches:
        if _match_pair(match, home_team_name, away_team_name):
            return match
    return None


# Kalshi's own per-league series-ticker prefix (confirmed live 2026-07-19,
# see market_catalog_soccer.py's kickoff audit) -> this app's
# SoccerMatch.league value (football-data.co.uk's division code, or "MLS").
#
# REAL BUG this fixes (caught live 2026-07-19, same day, while auditing the
# whole catalog for missing market types): this dict used to be a HAND-
# MAINTAINED, hardcoded GAME/SPREAD/TOTAL-only list -- every per-match
# series type added to kalshi_soccer_client.py AFTER that (BTTS first, then
# a much larger second batch: First Half/Second Half Winner/Spread/Total/
# BTTS, FTTS, Correct Score, Team Total) never got a matching entry here,
# so kalshi_match_suffix() silently returned None for every one of those
# series' real tickers -- confirmed live: EVERY tracked KXMLSBTTS market in
# the DB had soccer_match_id=NULL, meaning BTTS has been completely
# unmodeled (no match, no team names, no model_prob) since it shipped,
# without ever throwing an error or showing up as obviously broken. Rebuilt
# PROGRAMMATICALLY from kalshi_soccer_client.py's own per-market-type SERIES
# dicts instead of a second hand-maintained list, so this exact bug class
# (a new series type added to the client but never mirrored here) cannot
# recur -- adding a market type to the client's own SERIES dict is now the
# only thing needed for its matches to resolve here too.
def _build_prefix_to_division() -> dict[str, str]:
    from app.clients import kalshi_soccer_client as _kc

    series_dicts = [
        _kc.MONEYLINE_SERIES, _kc.SPREAD_SERIES, _kc.TOTAL_SERIES, _kc.BTTS_SERIES,
        _kc.FIRST_HALF_SERIES, _kc.FIRST_HALF_SPREAD_SERIES, _kc.FIRST_HALF_TOTAL_SERIES, _kc.FIRST_HALF_BTTS_SERIES,
        _kc.SECOND_HALF_SERIES, _kc.SECOND_HALF_SPREAD_SERIES, _kc.SECOND_HALF_TOTAL_SERIES, _kc.SECOND_HALF_BTTS_SERIES,
        _kc.FTTS_SERIES, _kc.SCORE_SERIES, _kc.TEAMTOTAL_SERIES,
    ]
    out = {}
    for series_dict in series_dicts:
        for division, ticker_prefix in series_dict.items():
            out[f"{ticker_prefix}-"] = division
    return out


_KALSHI_SOCCER_PREFIX_TO_DIVISION = _build_prefix_to_division()


def kalshi_match_suffix(event_ticker: str) -> tuple[str, str] | None:
    """"KXEPLGAME-26MAY24LFCBRE" -> ("E0", "26MAY24LFCBRE"). Same date+team-
    code suffix is shared across a league's GAME/SPREAD/TOTAL series for the
    same real match (confirmed live -- KXEPLGAME-26MAY24LFCBRE-TIE and any
    matching KXEPLSPREAD-26MAY24LFCBRE-... market share the identical
    suffix), same "cross-series join key" role as
    market_matcher_tennis.py::kalshi_match_suffix. Returns (division_code,
    suffix) so the caller doesn't need its own separate league-code lookup."""
    for prefix, division in _KALSHI_SOCCER_PREFIX_TO_DIVISION.items():
        if event_ticker.startswith(prefix):
            return division, event_ticker[len(prefix):]
    return None

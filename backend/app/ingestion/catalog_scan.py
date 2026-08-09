"""Flags Kalshi series / Polymarket events that show up in either
platform's live catalog but this app has never seen before -- a cheap
early-warning so a new market type doesn't sit unnoticed for weeks the way
the original full-catalog audits (see project memory) had to be done by
hand.

Covers all 9 sports this app tracks (expanded 2026-07-18 from NFL-only to
add NBA/MLB/MMA; expanded 2026-07-19 to add Tennis, then again the same day
to add Soccer; expanded 2026-07-20 to add Valorant/CS2/LoL -- a real gap
found live, user-reported: the 3 esports titles were built well after this
module's own last expansion and simply never got added here, meaning a
brand-new esports market type could open on either platform with zero
automated way to notice) -- each newly-added sport previously had ZERO
new-market detection, a real blind spot: a brand-new market type in any of
them could show up and this app would never even surface it for review. Each
sport's
own client modules already established a reliable identifying pattern for
its own real markets -- reused here rather than re-deriving:
  - NFL: ticker/title keyword match ("NFL"/"PRO FOOTBALL") -- kalshi_client.py's
    FUTURES_SERIES docstring, no single reliable ticker prefix confirmed for NFL.
  - NBA: Kalshi ticker prefix "KXNBA" (kalshi_nba_client.py), Polymarket
    tag_slug=nba + tag_slug=nba-summer-league (polymarket_nba_client.py).
  - MLB: Kalshi ticker prefix "KXMLB" (kalshi_mlb_client.py), Polymarket
    tag_slug=mlb (polymarket_mlb_client.py).
  - MMA: Kalshi ticker prefix "KXUFC" (kalshi_mma_client.py), Polymarket
    series_slug=ufc (polymarket_mma_client.py).
  - Tennis: Kalshi ticker prefix "KXATP"/"KXWTA"/"KXITF" (three tour/
    federation prefixes rather than one -- see kalshi_tennis_client.py's own
    MONEYLINE_SERIES/SET_WINNER_SERIES/etc. dicts for the 11 series already
    confirmed live under these prefixes), Polymarket series_slug=atp/wta/itf
    (polymarket_tennis_client.py's SERIES_SLUGS, same series_slug shape as
    MMA, not MLB/NBA's tag_slug).
  - Soccer: Kalshi ticker prefix "KXEPL"/"KXLALIGA"/"KXSERIEA"/
    "KXBUNDESLIGA"/"KXLIGUE1"/"KXMLS"/"KXPREMIERLEAGUE" (seven prefixes --
    six per-league prefixes covering GAME/SPREAD/TOTAL/BTTS/RELEGATION plus
    4 of 5 LEAGUE_WINNER_SERIES entries, and a 7th ("KXPREMIERLEAGUE") for
    EPL's own league-winner series specifically, which does NOT share the
    "KXEPL" prefix its other 5 series use -- confirmed live, not guessed),
    Polymarket tag_slug=epl/la-liga/serie-a/bundesliga/ligue-1/mls
    (polymarket_soccer_client.py's own TAG_SLUGS, same tag_slug shape as
    MLB/NBA, not MMA/Tennis's series_slug).
  - Valorant: Kalshi ticker prefix "KXVALORANT" (covers both KXVALORANTMAP
    and the tournament-winner series itself, confirmed live), Polymarket
    tag_slug=valorant.
  - CS2: Kalshi ticker prefix "KXCS2" (covers all 4 of KXCS2GAME/
    KXCS2MAPWINNER/KXCS2TOTALMAPS/KXCS2 itself, confirmed live), Polymarket
    tag_slug=counter-strike-2 + cs2 (see polymarket_cs2_client.py's own
    CS2_TAG_SLUGS -- `counter-strike-2` is where the real match-outcome
    inventory lives; `cs2` is props only).
  - LoL: Kalshi ticker prefixes "KXLOL"/"KXLEAGUEWORLDS" (LoL's own
    tournament-winner series doesn't share its other two series' "KXLOL"
    prefix, same one-sport-two-ticker-families exception as Soccer's own
    KXPREMIERLEAGUE), Polymarket tag_slug=league-of-legends + lol (the
    former carries the real per-match inventory polymarket_lol_client.py
    already ingests, the latter only futures/props).

Plus a catch-all "other" bucket (added 2026-08-02) for every Kalshi Sports
series that matches NONE of the 9 sports above. REAL GAP this closes: the
per-sport MATCHING above is prefix/keyword-based and needs no upkeep as a
tracked sport adds series, but the SPORT LIST itself was hardcoded -- so a
series for a sport this app doesn't track could never be scanned at all, no
matter how live it was. Confirmed live 2026-08-02: KXCODGAME (Call of Duty,
140 settled markets), KXDOTA2MAP and KXOWGAME (Dota 2 / Overwatch, both with
open markets right now), the 5 Rocket League series, and every WNBA and
motorsport series outside the handful this app already ingests were ALL
absent from catalog_entries entirely -- not dismissed, never even looked at.
The same bucket covers Polymarket (fetch_polymarket_other_events), where the
identical blind spot existed. Its first run paid for itself: it found 62 live
CS2 match events carrying $2.7M of liquidity that this app could not see,
because the CS2 client queried tag_slug=cs2 (props only) instead of
counter-strike-2 (the real head-to-head tag). That gap is now CLOSED --
ingestion built (polymarket_cs2_client.py) and both tags registered in
_POLYMARKET_SLUGS -- and chasing it turned up two more: LoL's own scan slug
had the same wrong-tag problem (ingestion was fine, only this scan was blind),
and all three esports Polymarket clients were silently dropping ~90% of their
handicap markets (see polymarket_client.handicap_lines_in_outcome_order).
See both fetchers for how the bucket is gated.

Deliberately NOT a hardcoded "known series" registry -- that would need
constant manual upkeep as this app's own coverage grows (240+ Kalshi series,
78+ Polymarket events already exist for NFL alone). Instead this diffs each
scan against what was seen in the PREVIOUS scan (persisted in CatalogEntry):
the very first scan just records the current catalog as the accepted
baseline (nothing flagged -- an empty table would otherwise incorrectly
flag the ~300+ series/events this app already deliberately scoped in/out of
during earlier build rounds), and every scan after that only flags
identifiers that are genuinely new since last time. Dismissing a flagged
entry (via POST /catalog/{id}/dismiss) records a disposition (bootstrapped
vs. flagged -- see catalog.py) so it never re-flags.
"""
import datetime
import logging
import re
import time

from sqlalchemy.orm import Session

from app.clients.base import get_json, paginate
from app.db.models import CatalogEntry, Market

# A full ISO calendar date embedded in an identifier marks a per-GAME instance
# (Polymarket slugs like "atp-rodiono-sachko-2026-07-19", "nba-lal-bos-2026-..."),
# NOT a new market TYPE. Those games are already handled automatically by each
# sport's own ingestion pipeline, so flagging them here is exactly the noise
# this scanner was NOT meant to surface (user intent 2026-07-22: flag genuinely
# new market types to come back to, never new games in an existing market).
# Kalshi series tickers (KXNFLGAME, ...) and Polymarket futures/prop slugs
# (…-receiving-yards-leader-<timestamp>) never contain a bare YYYY-MM-DD, so
# they still flag correctly.
_PER_GAME_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _is_per_game(identifier: str) -> bool:
    return bool(_PER_GAME_RE.search(identifier or ""))

log = logging.getLogger("catalog_scan")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_BASE = "https://gamma-api.polymarket.com"

# One scan calls every Kalshi fetcher below, and each of them needs the SAME
# full Sports-category series list (3,077 series live 2026-08-02). Without
# this the scan re-downloaded that identical list once per sport -- now 10
# times over, with the "other" bucket added. TTL is per-scan, not a real
# cache: the scan is daily, so this only ever de-dupes calls within one run.
_SERIES_CACHE_TTL_SECONDS = 300
_series_cache: tuple[float, list[dict]] | None = None


def _fetch_kalshi_sports_series() -> list[dict]:
    global _series_cache
    now = time.monotonic()
    if _series_cache is not None and now - _series_cache[0] < _SERIES_CACHE_TTL_SECONDS:
        return _series_cache[1]
    data = get_json(f"{KALSHI_BASE}/series?category=Sports", follow_redirects=True)
    series = data.get("series", [])
    _series_cache = (now, series)
    return series


def _prefix_matcher(*prefixes: str):
    return lambda ticker, _title: ticker.upper().startswith(prefixes)


def _nfl_matcher(ticker: str, title: str) -> bool:
    return "NFL" in ticker.upper() or "NFL" in title.upper() or "PRO FOOTBALL" in title.upper()


# The single source of truth for "which Kalshi series belongs to which
# tracked sport". Each entry is exactly the rule that sport's own client
# module already established (see the module docstring for the per-sport
# derivation and why several sports need more than one prefix). This lives
# in one dict rather than inline in 9 separate fetchers specifically so the
# "other" catch-all can be defined as the exact COMPLEMENT of it -- if a
# sport's matcher is ever widened here, the catch-all narrows in the same
# commit automatically instead of double-reporting that sport's series.
_KALSHI_SPORT_MATCHERS: dict[str, callable] = {
    "nfl": _nfl_matcher,
    "nba": _prefix_matcher("KXNBA"),
    "mlb": _prefix_matcher("KXMLB"),
    "mma": _prefix_matcher("KXUFC"),
    "tennis": _prefix_matcher("KXATP", "KXWTA", "KXITF"),
    "soccer": _prefix_matcher(
        "KXEPL", "KXLALIGA", "KXSERIEA", "KXBUNDESLIGA", "KXLIGUE1", "KXMLS", "KXPREMIERLEAGUE"
    ),
    "valorant": _prefix_matcher("KXVALORANT"),
    "cs2": _prefix_matcher("KXCS2"),
    "lol": _prefix_matcher("KXLOL", "KXLEAGUEWORLDS"),
    # CFB added 2026-08-02 when college football was integrated. Without this,
    # every KXNCAAF* series classified as "other" via the catch-all -- which is
    # how 45 of them (including six the app now actively prices) ended up bulk-
    # dismissed as not_relevant in a sweep of untracked sports.
    "cfb": _prefix_matcher("KXNCAAF"),
    # CoD added 2026-08-09, and it is the THIRD time this exact omission has
    # bitten (NCAAF above, CS2/LoL in the Polymarket map below). Call of Duty
    # shipped as a full sport without being added here, so all four KXCOD*
    # series fell to the "other" catch-all and were bulk-dismissed as
    # not_relevant -- including KXCODGAME, which the app ACTIVELY PRICES, and
    # KXCOD (COD Tournament Winner), whose dismissal is why a later check
    # concluded Call of Duty had no futures at all. It does.
    "cod": _prefix_matcher("KXCOD", "KXEWCCALLOFDUTY", "KXWCCOD"),
    # WNBA and racing, added 2026-08-09 by the same audit -- BOTH were already
    # priced and staked while every one of their series sat under "other".
    # Measured at the time: 44 KXWNBA* entries, 7 KXNASCAR*, 6 KXF1*, all
    # sport="other", several dispositioned not_relevant. Found by the new
    # registry guard in app/sports.check_registry_consistency rather than by
    # anyone noticing, which is the whole argument for that guard.
    "wnba": _prefix_matcher("KXWNBA"),
    "racing": _prefix_matcher("KXF1", "KXNASCAR", "KXINDYCAR"),
}


def _fetch_kalshi_series_for_sport(sport: str) -> list[dict]:
    match = _KALSHI_SPORT_MATCHERS[sport]
    return [
        {"identifier": s.get("ticker", ""), "title": s.get("title") or s.get("ticker", "")}
        for s in _fetch_kalshi_sports_series()
        if s.get("ticker") and match(s.get("ticker", ""), s.get("title") or "")
    ]


def fetch_kalshi_wnba_series() -> list[dict]:
    """Every KXWNBA* series -- moneyline/spread/total plus the season ladders
    and bracket families kalshi_wnba_client already reads."""
    return _fetch_kalshi_series_for_sport("wnba")


def fetch_polymarket_wnba_events() -> list[dict]:
    return _fetch_polymarket_events_for_sport("wnba")


def fetch_kalshi_racing_series() -> list[dict]:
    """F1, NASCAR and IndyCar together, matching the app's single `racing`
    registry key (the routers split by series underneath)."""
    return _fetch_kalshi_series_for_sport("racing")


def fetch_polymarket_racing_events() -> list[dict]:
    return _fetch_polymarket_events_for_sport("racing")


def fetch_kalshi_cod_series() -> list[dict]:
    """Every KXCOD* series plus the Esports-World-Cup-branded CoD families.
    Deliberately the whole family, same reasoning as CFB below: the tournament
    and map series list little or nothing most weeks, and the point is that
    they appear here the moment they do."""
    return _fetch_kalshi_series_for_sport("cod")


def fetch_polymarket_cod_events() -> list[dict]:
    """The same `call-of-duty` tag polymarket_cod_client.py already ingests --
    kept in step with it deliberately, since a catch-all that does not know the
    real tag reports already-ingested events as untracked."""
    return _fetch_polymarket_events_for_sport("cod")


def fetch_kalshi_cfb_series() -> list[dict]:
    """Every KXNCAAF* series. Note this deliberately catches the whole family --
    spread and total series list nothing today but will appear here the moment
    they do, which is the point."""
    return _fetch_kalshi_series_for_sport("cfb")


def fetch_kalshi_nfl_series() -> list[dict]:
    """Every Sports-category series whose ticker or title marks it as NFL --
    same broad filter used for the original manual catalog audit (see
    kalshi_client.py's FUTURES_SERIES docstring), just automated here."""
    return _fetch_kalshi_series_for_sport("nfl")


# Every Polymarket lookup slug this app's own per-sport fetchers use, in ONE
# place. Gamma exposes two different lookup shapes and this app uses both --
# tag_slug for most sports, series_slug for MMA/Tennis (see those fetchers'
# own docstrings for why) -- so both are recorded per sport.
#
# The catch-all takes the union of these as its exclusion set. Keeping a
# SECOND hardcoded copy of these strings is exactly the drift that
# _KALSHI_SPORT_MATCHERS exists to prevent on the other platform, and this
# gives Polymarket the same guarantee: widen a sport here and the catch-all
# narrows in the same edit. That matters concretely right now -- CS2 is
# tracked under "cs2" (props only) while its real match events are tagged
# "counter-strike-2", so the day that ingestion is fixed, adding the tag
# here is what stops the catch-all still reporting CS2 as untracked.
_POLYMARKET_SLUGS: dict[str, dict[str, tuple[str, ...]]] = {
    "nfl": {"tag": ("nfl",)},
    "nba": {"tag": ("nba", "nba-summer-league")},
    "mlb": {"tag": ("mlb",)},
    "mma": {"series": ("ufc",)},
    "tennis": {"series": ("atp", "wta", "itf")},
    "soccer": {"tag": ("epl", "la-liga", "serie-a", "bundesliga", "ligue-1", "mls")},
    "valorant": {"tag": ("valorant",)},
    # Matches polymarket_cod_client.COD_TAG_SLUGS exactly.
    "cod": {"tag": ("call-of-duty",)},
    # Matches polymarket_wnba_client.TAG_SLUG and polymarket_racing_client._TAGS.
    "wnba": {"tag": ("wnba",)},
    "racing": {"tag": ("f1", "nascar", "indycar")},
    # counter-strike-2 added 2026-08-02, in the same change that taught
    # ingestion to read those events (polymarket_cs2_client.py) -- exactly the
    # paired edit the comment above anticipated. `cs2` is kept alongside it:
    # it is props-only, but dropping it would stop excluding those props from
    # the catch-all. Confirmed live that CS2 match events carry the
    # `counter-strike-2` tag and NOT `cs2`, so before this the catch-all had
    # no way to know they were tracked.
    "cs2": {"tag": ("counter-strike-2", "cs2")},
    # league-of-legends added in the same audit for the same reason -- LoL
    # match events carry `league-of-legends` and NOT `lol`, and
    # polymarket_lol_client.py has ALWAYS ingested that tag, so the catch-all
    # was reporting 150 already-ingested LoL match events as untracked. This
    # one was purely a scan-side blind spot, not a missing-inventory bug.
    "lol": {"tag": ("league-of-legends", "lol")},
}

_TRACKED_POLYMARKET_SLUGS = {
    slug
    for shapes in _POLYMARKET_SLUGS.values()
    for slugs in shapes.values()
    for slug in slugs
}


def _fetch_polymarket_events_for_sport(sport: str) -> list[dict]:
    shapes = _POLYMARKET_SLUGS[sport]
    out: dict[str, dict] = {}
    for shape, slugs in shapes.items():
        param = "tag_slug" if shape == "tag" else "series_slug"
        for slug in slugs:
            events = paginate(
                lambda offset, p=param, s=slug: f"{GAMMA_BASE}/events?{p}={s}&closed=false&limit=200&offset={offset}",
                list_key=None,
                cursor_style="offset",
            )
            for e in events:
                if e.get("slug"):
                    out[e["slug"]] = {"identifier": e["slug"], "title": e.get("title") or e["slug"]}
    return list(out.values())


def fetch_polymarket_nfl_events() -> list[dict]:
    return _fetch_polymarket_events_for_sport("nfl")


def fetch_kalshi_nba_series() -> list[dict]:
    """Unlike NFL, NBA/MLB/MMA each have a single reliable ticker prefix
    confirmed live by that sport's own client module (see module docstring),
    so a prefix match is more precise than NFL's keyword search."""
    return _fetch_kalshi_series_for_sport("nba")


def fetch_kalshi_mlb_series() -> list[dict]:
    return _fetch_kalshi_series_for_sport("mlb")


def fetch_kalshi_mma_series() -> list[dict]:
    return _fetch_kalshi_series_for_sport("mma")


def fetch_polymarket_nba_events() -> list[dict]:
    return _fetch_polymarket_events_for_sport("nba")


def fetch_polymarket_mlb_events() -> list[dict]:
    return _fetch_polymarket_events_for_sport("mlb")


def fetch_polymarket_mma_events() -> list[dict]:
    """series_slug shape, not tag_slug -- see polymarket_mma_client.py."""
    return _fetch_polymarket_events_for_sport("mma")


def fetch_kalshi_tennis_series() -> list[dict]:
    """Tennis has THREE distinct tour/federation ticker prefixes (ATP/WTA/
    ITF), unlike NBA/MLB/MMA's single prefix -- matching on all three rather
    than hardcoding each of the 11 series tickers kalshi_tennis_client.py
    already tracks means a brand-new tennis series (e.g. doubles, or a
    futures market -- both real but unbuilt, see that module's docstring)
    surfaces here automatically instead of needing this scan updated every
    time that client adds a series."""
    return _fetch_kalshi_series_for_sport("tennis")


def fetch_kalshi_soccer_series() -> list[dict]:
    """Soccer (added 2026-07-19) has SEVEN real ticker prefixes confirmed
    live across this app's own build (kalshi_soccer_client.py's own
    MONEYLINE_SERIES/SPREAD_SERIES/TOTAL_SERIES/BTTS_SERIES/
    RELEGATION_SERIES dicts plus LEAGUE_WINNER_SERIES, which uses a
    DIFFERENT prefix for EPL specifically -- "KXPREMIERLEAGUE", not
    "KXEPL...", confirmed live, not a guess): KXEPL/KXLALIGA/KXSERIEA/
    KXBUNDESLIGA/KXLIGUE1/KXMLS cover every per-match and per-league-minus-
    EPL-winner series (GAME/SPREAD/TOTAL/BTTS/RELEGATION + 4 of 5
    LEAGUE_WINNER_SERIES), KXPREMIERLEAGUE covers the 5th. A brand-new
    Soccer series under any of these 7 prefixes (e.g. a real Top-4 market
    once Kalshi opens one, see season_sim_soccer.py's own docstring on why
    that's not built yet) surfaces here automatically."""
    return _fetch_kalshi_series_for_sport("soccer")


def fetch_polymarket_soccer_events() -> list[dict]:
    """Same tag_slug shape as NBA/MLB (not series_slug like MMA/Tennis) --
    see polymarket_soccer_client.py's own TAG_SLUGS."""
    return _fetch_polymarket_events_for_sport("soccer")


def fetch_kalshi_valorant_series() -> list[dict]:
    """REAL GAP this closes (found live 2026-07-20, user-reported: "keep
    pushing esports... covering everything all markets"): the 3 esports
    titles (Valorant/CS2/LoL) were added to this app well after this
    module's own last expansion (2026-07-19, Soccer) and never got added
    here -- meaning a brand-new esports market type could open on either
    platform and this app would have zero automated way to notice, the
    exact blind spot this module exists to close for every other sport.
    "KXVALORANT" is a real prefix of BOTH of Valorant's own real Kalshi
    series (KXVALORANTMAP for map winner, and KXVALORANT itself for
    tournament-winner futures -- confirmed live, see
    kalshi_valorant_client.py's own get_tournament_winner_markets)."""
    return _fetch_kalshi_series_for_sport("valorant")


def fetch_kalshi_cs2_series() -> list[dict]:
    """"KXCS2" is a real prefix of all 4 of CS2's own Kalshi series --
    KXCS2GAME (series winner), KXCS2MAPWINNER, KXCS2TOTALMAPS, and KXCS2
    itself (tournament-winner futures) -- confirmed live, see
    kalshi_cs2_client.py's own SERIES_WINNER_SERIES/MAP_WINNER_SERIES/
    TOTAL_MAPS_SERIES/TOURNAMENT_WINNER_SERIES constants."""
    return _fetch_kalshi_series_for_sport("cs2")


def fetch_kalshi_lol_series() -> list[dict]:
    """LoL's own tournament-winner futures series (KXLEAGUEWORLDS) does NOT
    share LoL's other two series' own "KXLOL" prefix (KXLOLMAP/
    KXLOLTOTALMAPS) -- confirmed live, same "one sport, two unrelated
    ticker families" exception Soccer's own KXPREMIERLEAGUE already needed
    for EPL's league-winner series (see fetch_kalshi_soccer_series's own
    docstring)."""
    return _fetch_kalshi_series_for_sport("lol")


def fetch_polymarket_valorant_events() -> list[dict]:
    return _fetch_polymarket_events_for_sport("valorant")


def fetch_polymarket_cs2_events() -> list[dict]:
    """CS2's Polymarket inventory spans BOTH of its tags: `cs2` carries props
    only (a FaZe tier-1-event prop, map-pool-change props, roster-change
    props), while `counter-strike-2` carries the real head-to-head match
    events (62 live, ~$2.7M liquidity).

    WRONG-SLUG GAP NOW CLOSED (2026-08-02): the note that used to sit here
    flagged tag_slug=cs2 as the wrong slug and deliberately held off adding
    counter-strike-2 until ingestion could handle it -- both halves of that
    are now done in one change. Ingestion is polymarket_cs2_client.py (which
    also handles the ~98 finished-but-still-open events that query returns,
    via a server-side end_date_min filter plus a gameStartTime staleness
    gate), and the tag is registered in _POLYMARKET_SLUGS above so the
    catch-all stops reporting CS2 as untracked."""
    return _fetch_polymarket_events_for_sport("cs2")


def fetch_polymarket_lol_events() -> list[dict]:
    """CORRECTED 2026-08-02, found by the same audit as the CS2 slug above:
    this used to fetch `lol` alone, on the recorded grounds that
    "league-of-legends returns the identical event set". It does not --
    confirmed live, `lol` returns 6 futures/prop events while
    `league-of-legends` returns 150 real per-match events, and LoL match
    events carry ONLY the latter tag.

    Unlike CS2 this was never an ingestion gap: polymarket_lol_client.py has
    always queried `league-of-legends` and has always ingested those matches.
    The damage was confined to this scan -- the catch-all bucket had no way to
    tell those 150 events were already tracked, so it reported them as
    untracked. Both tags are queried now (see _POLYMARKET_SLUGS)."""
    return _fetch_polymarket_events_for_sport("lol")


def fetch_polymarket_tennis_events() -> list[dict]:
    """series_slug shape (same as MMA's fetch above), not tag_slug -- see
    polymarket_tennis_client.py's own SERIES_SLUGS."""
    return _fetch_polymarket_events_for_sport("tennis")


OTHER_SPORT = "other"

# Safety stop for the open-events sweep below. Measured live 2026-08-02: 45
# pages at limit=200 (8,952 open events). The cap only exists so a broken
# Kalshi cursor can't spin this daily job forever -- a truncated sweep can
# only UNDER-report which series are live (a missed flag, retried tomorrow),
# never invent a dead one, since entries are only ever added here.
_OPEN_SWEEP_PAGE_CAP = 400


def _fetch_kalshi_open_series() -> set[str]:
    """Every Kalshi series ticker that currently has at least one OPEN
    market, from one sweep of /events?status=open.

    Why the event sweep and not the obvious per-series call: gating the
    catch-all needs a live/dead verdict for ~2,200 untracked series, and
    /markets?series_ticker=X&status=open answers that one series at a time
    -- 2,200 requests, which Kalshi rate-limits into the tens of minutes.
    This sweep answers all of them in 45 requests / 5.3 MB / ~34s.

    Validated against that expensive path rather than assumed (2026-08-02):
    the per-series probe found 565 of the 2,221 untracked series live, and
    this sweep agreed on every one of them (one extra, KXINTLPLAYAGAIN,
    which opened mid-run). A third signal -- sweeping all 539,071 open
    markets and grouping by series -- agreed with this one exactly, and is
    NOT used: it costs 540 requests and ~86% of what it downloads is
    KXMVE* multi-game parlay markets this scan has no use for.

    NOT volume-based on purpose: Kalshi's bulk /markets endpoint reports
    volume=0 for every market it returns, including series known to trade
    (KXDOTA2MAP's 26 open markets all report 0), so volume is not a usable
    liveness signal here. Open market COUNT is.
    """
    open_series: set[str] = set()
    cursor = ""
    pages = 0
    while True:
        url = f"{KALSHI_BASE}/events?status=open&limit=200"
        if cursor:
            url += f"&cursor={cursor}"
        data = get_json(url, follow_redirects=True)
        events = data.get("events", [])
        for event in events:
            ticker = event.get("series_ticker")
            if ticker:
                open_series.add(ticker.upper())
        pages += 1
        cursor = data.get("cursor") or ""
        if not cursor or not events:
            break
        if pages >= _OPEN_SWEEP_PAGE_CAP:
            log.warning("open-events sweep hit the %d-page cap; live-series set may be partial", _OPEN_SWEEP_PAGE_CAP)
            break
    return open_series


def _ingested_kalshi_series(session: Session) -> set[str]:
    """Series this app already prices, derived from the markets it has
    actually ingested rather than from a hand-kept list -- Market.source_ticker
    is a Kalshi market ticker shaped SERIES-EVENT-OUTCOME.

    This is what keeps the catch-all from re-reporting the sports that ARE
    tracked but have no matcher above, because they were never scanned:
    WNBA (KXWNBAGAME/SPREAD/TOTAL) and motorsport (KXF1*/KXNASCAR*/
    KXINDYCAR*) are ingested by their own pollers, so they drop out here
    while their genuinely-unbuilt siblings (KXWNBA1QTOTAL, KXWNBAMVP, ...)
    still surface. 146 distinct series were ingested live 2026-08-02.

    Every prefix of a ticker is added, not just the first dash-delimited
    segment, because a few real series tickers contain a dash themselves
    (KXWO-CURL) -- a first-segment split would mis-key those. Extra prefixes
    that aren't real series tickers are harmless: the result is only ever
    intersected against the live Sports series list.
    """
    ingested: set[str] = set()
    for (ticker,) in session.query(Market.source_ticker).filter(Market.source == "kalshi").distinct():
        parts = (ticker or "").upper().split("-")
        for i in range(1, len(parts)):
            ingested.add("-".join(parts[:i]))
    return ingested


def fetch_kalshi_other_series(session: Session) -> list[dict]:
    """The catch-all: Sports series that belong to NO sport this app tracks.

    Three gates, all required (each measured live 2026-08-02, from 3,077
    Sports-category series):
      1. Matches none of _KALSHI_SPORT_MATCHERS -> 2,221 left. This is the
         complement of the 9 tracked sports by construction, so it can't
         drift out of sync with them.
      2. Has at least one OPEN market -> 566 left. THE important gate:
         Kalshi's catalog is full of dead shells, and without this the tab
         would flood with series that will never trade again. Rocket League
         is the clean example -- all 5 of its series (KXRLGAME/KXRLMAP/
         KXRLTOTALMAPS/KXROCKETLEAGUE/KXROCKETLEAGUEGAME) have 0 open AND 0
         ever-settled markets, and Call of Duty (KXCODGAME) is the softer
         case: 140 markets settled, 0 open, i.e. a real series between
         seasons. Both are correctly withheld until they actually reopen,
         and re-checked on every later scan since nothing is recorded for
         them now.
      3. Isn't already ingested by one of this app's own pollers -> 558.
         See _ingested_kalshi_series.

    See fetch_polymarket_other_events for the same bucket on the other
    platform -- both feed sport="other".
    """
    tracked = list(_KALSHI_SPORT_MATCHERS.values())
    candidates = [
        s for s in _fetch_kalshi_sports_series()
        if s.get("ticker") and not any(m(s["ticker"], s.get("title") or "") for m in tracked)
    ]
    if not candidates:
        return []

    open_series = _fetch_kalshi_open_series()
    ingested = _ingested_kalshi_series(session)
    out = [
        {"identifier": s["ticker"], "title": s.get("title") or s["ticker"]}
        for s in candidates
        if s["ticker"].upper() in open_series and s["ticker"].upper() not in ingested
    ]
    log.info(
        "catalog scan (other): %d untracked series, %d with open markets, %d not already ingested",
        len(candidates),
        sum(1 for s in candidates if s["ticker"].upper() in open_series),
        len(out),
    )
    return out


# Gamma's /events?offset= 422s past 500 ("offset too large, use /events/keyset
# for deeper pagination"), so the catch-all uses the keyset path. Its cursor
# parameter is `after_cursor` -- NOT the `cursor`/`next_cursor` the response
# field is named, which silently returns page 1 forever rather than erroring
# (caught live: every derived count came back an exact multiple of the page
# count). limit caps at 100 regardless of what's asked for. Measured live
# 2026-08-02: 144 pages / ~60s for the whole live sports catalog.
_GAMMA_KEYSET_PAGE_CAP = 400


def _fetch_polymarket_live_sports_events() -> list[dict]:
    """Every open, future-dated Polymarket event carrying a sports tag.

    `end_date_min=<now>` is a server-side filter and does the single most
    important job here: Gamma's `closed=false` is NOT a liveness signal on
    its own. Confirmed live 2026-08-02 -- tag_slug=counter-strike-2 returns
    100 `closed=false`/`active=true` events of which 98 ended months ago,
    exactly the stale-market trap this app has already had to fix on Kalshi
    (dead-market price bug) and in MLB/Tennis. Filtering on end date cuts
    those before they can ever reach a CatalogEntry row.

    Sports-ness comes from Gamma's own /sports registry (388 sport codes,
    386 distinct tag ids) rather than a "sports" tag slug -- the plain
    `sports` slug is carried by only half the real sports events (201 of
    402 in a sample sweep), so keying off it would silently miss the rest.
    """
    sports = get_json(f"{GAMMA_BASE}/sports")
    sport_tag_ids = {
        t.strip() for s in sports for t in str(s.get("tags") or "").split(",") if t.strip()
    }
    now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    out: list[dict] = []
    cursor = ""
    pages = 0
    while True:
        url = f"{GAMMA_BASE}/events/keyset?closed=false&end_date_min={now_iso}&limit=100"
        if cursor:
            url += f"&after_cursor={cursor}"
        data = get_json(url)
        events = data.get("events") or []
        for event in events:
            tag_ids = {
                str(t.get("id")) for t in (event.get("tags") or [])
                if isinstance(t, dict) and t.get("id")
            }
            if tag_ids & sport_tag_ids:
                out.append(event)
        pages += 1
        cursor = data.get("next_cursor") or ""
        if not cursor or not events:
            break
        if pages >= _GAMMA_KEYSET_PAGE_CAP:
            log.warning("gamma keyset sweep hit the %d-page cap; live-event set may be partial", _GAMMA_KEYSET_PAGE_CAP)
            break
    return out


def _ingested_polymarket_events(session: Session) -> set[str]:
    """Polymarket event slugs this app already ingests. `source_event_id` IS
    the event slug for Polymarket rows (unlike source_ticker, which holds the
    CLOB token id), so this is a direct match rather than the prefix
    reconstruction the Kalshi side needs."""
    return {
        slug for (slug,) in session.query(Market.source_event_id)
        .filter(Market.source == "polymarket").distinct() if slug
    }


def fetch_polymarket_other_events(session: Session) -> list[dict]:
    """Polymarket half of the catch-all -- same three gates as the Kalshi
    one, measured live 2026-08-02:
      1. Open, future-dated, carrying a /sports tag -> 8,338 of 14,306.
      2. Carries none of _TRACKED_POLYMARKET_SLUGS -> 7,862, of which 7,818
         have real liquidity. (Polymarket DOES report usable volume and
         liquidity, unlike Kalshi's bulk endpoints -- see
         _fetch_kalshi_open_series -- so liquidity is a valid gate here.)
      3. Not already ingested by one of this app's own pollers.
    scan_catalog's own _is_per_game filter then drops the ~7,700 individual
    dated matches, leaving ~129 genuine market TYPES -- comparable in size to
    the Kalshi bucket, and the reason this doesn't need its own extra gate.

    REAL GAP this found on its first run, now CLOSED (2026-08-02): this app
    pulled Polymarket CS2 under `tag_slug=cs2`, which returns props only
    (roster changes, Valve sticker trade-ups) -- which is where the old
    "Polymarket has no CS2 match-outcome market type" note in
    market_catalog_cs2.py/poller_cs2.py came from. The actual head-to-head
    events are tagged `counter-strike-2`: 62 live dated matches carrying $2.7M
    of liquidity, in a sport this app already models with Elo and already
    prices on Kalshi.

    That gap was deliberately left surfacing through this bucket rather than
    fixed by silently widening the CS2 slug, on the grounds that wiring the
    tag in is a real ingestion change needing its own date/status gating
    reviewed first. That review happened and the change shipped: see
    polymarket_cs2_client.py for the gating (the naive slug swap really would
    have injected ~98 months-dead markets -- `closed=false` is not a liveness
    signal on Gamma) and _POLYMARKET_SLUGS above for the paired registration
    that keeps this bucket from now reporting the same events as untracked.
    Kept as the worked example of what this tab is for.
    """
    events = _fetch_polymarket_live_sports_events()
    ingested = _ingested_polymarket_events(session)
    out: list[dict] = []
    for event in events:
        slug = event.get("slug")
        if not slug or slug in ingested:
            continue
        tag_slugs = {
            t.get("slug") for t in (event.get("tags") or [])
            if isinstance(t, dict) and t.get("slug")
        }
        if tag_slugs & _TRACKED_POLYMARKET_SLUGS:
            continue
        if float(event.get("liquidity") or 0) <= 0:
            continue
        out.append({"identifier": slug, "title": event.get("title") or slug})
    log.info(
        "catalog scan (other/polymarket): %d live sports events, %d untracked+liquid+uningested",
        len(events), len(out),
    )
    return out


# One (platform-fetcher) pair per sport -- scan_catalog loops over this
# rather than hardcoding 5 near-identical try/except blocks.
_SPORT_FETCHERS: dict[str, list[tuple[str, callable]]] = {
    "nfl": [("kalshi", fetch_kalshi_nfl_series), ("polymarket", fetch_polymarket_nfl_events)],
    "nba": [("kalshi", fetch_kalshi_nba_series), ("polymarket", fetch_polymarket_nba_events)],
    "mlb": [("kalshi", fetch_kalshi_mlb_series), ("polymarket", fetch_polymarket_mlb_events)],
    "mma": [("kalshi", fetch_kalshi_mma_series), ("polymarket", fetch_polymarket_mma_events)],
    "tennis": [("kalshi", fetch_kalshi_tennis_series), ("polymarket", fetch_polymarket_tennis_events)],
    "soccer": [("kalshi", fetch_kalshi_soccer_series), ("polymarket", fetch_polymarket_soccer_events)],
    "valorant": [("kalshi", fetch_kalshi_valorant_series), ("polymarket", fetch_polymarket_valorant_events)],
    "cs2": [("kalshi", fetch_kalshi_cs2_series), ("polymarket", fetch_polymarket_cs2_events)],
    "lol": [("kalshi", fetch_kalshi_lol_series), ("polymarket", fetch_polymarket_lol_events)],
    # Kalshi only -- Polymarket lists no college football. MUST sit ABOVE the
    # catch-all so KXNCAAF* series classify as "cfb"; the catch-all is defined as
    # the complement of the named sports and would otherwise swallow them.
    "cfb": [("kalshi", fetch_kalshi_cfb_series)],
    "cod": [("kalshi", fetch_kalshi_cod_series), ("polymarket", fetch_polymarket_cod_events)],
    "wnba": [("kalshi", fetch_kalshi_wnba_series), ("polymarket", fetch_polymarket_wnba_events)],
    "racing": [("kalshi", fetch_kalshi_racing_series), ("polymarket", fetch_polymarket_racing_events)],
    OTHER_SPORT: [("kalshi", fetch_kalshi_other_series),
                  ("polymarket", fetch_polymarket_other_events)],
}

# Both catch-all fetchers need the session (they read the markets table to
# tell "already ingested" from "genuinely untracked"); the other 18 are pure
# HTTP. Listing the exceptions beats threading an unused `session` argument
# through every one of them.
_SESSION_AWARE_FETCHERS = {fetch_kalshi_other_series, fetch_polymarket_other_events}

# Sports exempt from the per-sport bootstrap below. "other" is the only one:
# bootstrap exists so a newly-scanned sport doesn't flag the series this app
# already deliberately scoped in or out during earlier build rounds -- but
# NOTHING in this bucket has ever been scoped either way. That's its
# definition (matches no tracked sport, isn't ingested), so silently
# recording its contents as an accepted baseline would dismiss the entire
# find on the one scan that discovers it and surface nothing, ever. The
# open-markets gate is what keeps that first batch reviewable.
_NO_BOOTSTRAP_SPORTS = {OTHER_SPORT}


def scan_catalog(session: Session) -> list[CatalogEntry]:
    """Runs the diff for every sport x platform, upserts CatalogEntry rows,
    and returns any entries newly flagged THIS scan (empty on each sport's
    own bootstrap scan, by design). Errors from any single sport/platform
    are logged and skipped rather than raised -- a transient Kalshi/
    Polymarket outage shouldn't crash the scheduled job or take down every
    OTHER sport's scan, same tolerance pattern as the rest of the poller.

    REAL BUG caught live (2026-07-18, immediately after expanding this past
    NFL-only): bootstrap was computed ONCE for the whole table, not per
    sport -- so the FIRST scan after adding NBA/MLB/MMA coverage flagged
    every one of their existing series/events (517 rows) as "new," even
    though this app already deliberately ingests most of them (KXNBAGAME,
    KXMLBGAME, etc. via their own pollers) -- they were just never SCANNED
    by this module before. Bootstrap must be per-sport: a sport with zero
    existing CatalogEntry rows records its current catalog as the accepted
    baseline (same as the original whole-table bootstrap did for NFL),
    independent of whether OTHER sports already have history."""
    newly_flagged: list[CatalogEntry] = []
    counted = 0
    bootstrapped_sports: list[str] = []

    for sport, fetchers in _SPORT_FETCHERS.items():
        is_bootstrap = (
            sport not in _NO_BOOTSTRAP_SPORTS
            and session.query(CatalogEntry).filter_by(sport=sport).count() == 0
        )
        if is_bootstrap:
            bootstrapped_sports.append(sport)
        for platform, fetch_fn in fetchers:
            try:
                items = fetch_fn(session) if fetch_fn in _SESSION_AWARE_FETCHERS else fetch_fn()
            except Exception:
                log.exception("%s %s catalog scan failed", sport, platform)
                continue
            counted += len(items)

            for item in items:
                # Skip per-game event instances (a scheduled game in a market
                # type we already handle) -- only genuinely-new market TYPES
                # belong in this flag list.
                if _is_per_game(item["identifier"]):
                    continue
                existing = (
                    session.query(CatalogEntry)
                    .filter_by(platform=platform, identifier=item["identifier"])
                    .one_or_none()
                )
                if existing is None:
                    entry = CatalogEntry(
                        platform=platform,
                        identifier=item["identifier"],
                        title=item["title"],
                        sport=sport,
                        dismissed=1 if is_bootstrap else 0,
                    )
                    session.add(entry)
                    if not is_bootstrap:
                        newly_flagged.append(entry)
                else:
                    existing.last_seen = datetime.datetime.utcnow()
                    existing.title = item["title"]
                    # A pre-existing NFL-only-era row has sport="nfl" as a
                    # column default, not a real classification -- backfill
                    # it to the sport that's actually re-scanning it now.
                    if existing.sport != sport:
                        existing.sport = sport

    session.commit()
    if bootstrapped_sports:
        log.info("catalog scan bootstrap: recorded %d series/events as baseline for %s", counted, bootstrapped_sports)
    if newly_flagged:
        log.info("catalog scan: %d new series/events flagged for review", len(newly_flagged))
    return newly_flagged

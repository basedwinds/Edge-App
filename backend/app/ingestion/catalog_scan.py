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
    tag_slug=cs2 (real inventory here is prop/futures markets only, no
    match-outcome market type -- see market_catalog_cs2.py's own docstring).
  - LoL: Kalshi ticker prefixes "KXLOL"/"KXLEAGUEWORLDS" (LoL's own
    tournament-winner series doesn't share its other two series' "KXLOL"
    prefix, same one-sport-two-ticker-families exception as Soccer's own
    KXPREMIERLEAGUE), Polymarket tag_slug=lol (season-winner futures only,
    no match-outcome market type -- see market_catalog_lol.py's own
    docstring).

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

from sqlalchemy.orm import Session

from app.clients.base import get_json, paginate
from app.db.models import CatalogEntry

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


def fetch_kalshi_nfl_series() -> list[dict]:
    """Every Sports-category series whose ticker or title marks it as NFL --
    same broad filter used for the original manual catalog audit (see
    kalshi_client.py's FUTURES_SERIES docstring), just automated here."""
    data = get_json(f"{KALSHI_BASE}/series?category=Sports")
    series = data.get("series", [])
    out = []
    for s in series:
        ticker = s.get("ticker", "")
        title = s.get("title", "")
        if "NFL" in ticker.upper() or "NFL" in title.upper() or "PRO FOOTBALL" in title.upper():
            out.append({"identifier": ticker, "title": title or ticker})
    return out


def fetch_polymarket_nfl_events() -> list[dict]:
    events = paginate(
        lambda offset: f"{GAMMA_BASE}/events?tag_slug=nfl&closed=false&limit=200&offset={offset}",
        list_key=None,
        cursor_style="offset",
    )
    return [{"identifier": e.get("slug", ""), "title": e.get("title") or e.get("slug", "")} for e in events if e.get("slug")]


def _fetch_kalshi_series_by_prefix(prefix: str) -> list[dict]:
    """Shared for NBA/MLB/MMA -- unlike NFL, each of these has a single
    reliable ticker prefix confirmed live by that sport's own client module
    (see module docstring), so a prefix match is more precise than NFL's
    keyword search."""
    data = get_json(f"{KALSHI_BASE}/series?category=Sports")
    series = data.get("series", [])
    return [
        {"identifier": s.get("ticker", ""), "title": s.get("title") or s.get("ticker", "")}
        for s in series if s.get("ticker", "").upper().startswith(prefix)
    ]


def fetch_kalshi_nba_series() -> list[dict]:
    return _fetch_kalshi_series_by_prefix("KXNBA")


def fetch_kalshi_mlb_series() -> list[dict]:
    return _fetch_kalshi_series_by_prefix("KXMLB")


def fetch_kalshi_mma_series() -> list[dict]:
    return _fetch_kalshi_series_by_prefix("KXUFC")


def _fetch_polymarket_events_by_tag_slugs(tag_slugs: list[str]) -> list[dict]:
    out: dict[str, dict] = {}
    for tag_slug in tag_slugs:
        events = paginate(
            lambda offset, ts=tag_slug: f"{GAMMA_BASE}/events?tag_slug={ts}&closed=false&limit=200&offset={offset}",
            list_key=None,
            cursor_style="offset",
        )
        for e in events:
            slug = e.get("slug")
            if slug:
                out[slug] = {"identifier": slug, "title": e.get("title") or slug}
    return list(out.values())


def fetch_polymarket_nba_events() -> list[dict]:
    return _fetch_polymarket_events_by_tag_slugs(["nba", "nba-summer-league"])


def fetch_polymarket_mlb_events() -> list[dict]:
    return _fetch_polymarket_events_by_tag_slugs(["mlb"])


def fetch_polymarket_mma_events() -> list[dict]:
    events = paginate(
        lambda offset: f"{GAMMA_BASE}/events?series_slug=ufc&closed=false&limit=200&offset={offset}",
        list_key=None,
        cursor_style="offset",
    )
    return [{"identifier": e.get("slug", ""), "title": e.get("title") or e.get("slug", "")} for e in events if e.get("slug")]


def fetch_kalshi_tennis_series() -> list[dict]:
    """Tennis has THREE distinct tour/federation ticker prefixes (ATP/WTA/
    ITF), unlike NBA/MLB/MMA's single prefix -- matching on all three rather
    than hardcoding each of the 11 series tickers kalshi_tennis_client.py
    already tracks means a brand-new tennis series (e.g. doubles, or a
    futures market -- both real but unbuilt, see that module's docstring)
    surfaces here automatically instead of needing this scan updated every
    time that client adds a series."""
    data = get_json(f"{KALSHI_BASE}/series?category=Sports")
    series = data.get("series", [])
    prefixes = ("KXATP", "KXWTA", "KXITF")
    return [
        {"identifier": s.get("ticker", ""), "title": s.get("title") or s.get("ticker", "")}
        for s in series if s.get("ticker", "").upper().startswith(prefixes)
    ]


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
    data = get_json(f"{KALSHI_BASE}/series?category=Sports")
    series = data.get("series", [])
    prefixes = ("KXEPL", "KXLALIGA", "KXSERIEA", "KXBUNDESLIGA", "KXLIGUE1", "KXMLS", "KXPREMIERLEAGUE")
    return [
        {"identifier": s.get("ticker", ""), "title": s.get("title") or s.get("ticker", "")}
        for s in series if s.get("ticker", "").upper().startswith(prefixes)
    ]


def fetch_polymarket_soccer_events() -> list[dict]:
    """Same tag_slug shape as NBA/MLB (not series_slug like MMA/Tennis) --
    see polymarket_soccer_client.py's own TAG_SLUGS."""
    return _fetch_polymarket_events_by_tag_slugs(["epl", "la-liga", "serie-a", "bundesliga", "ligue-1", "mls"])


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
    return _fetch_kalshi_series_by_prefix("KXVALORANT")


def fetch_kalshi_cs2_series() -> list[dict]:
    """"KXCS2" is a real prefix of all 4 of CS2's own Kalshi series --
    KXCS2GAME (series winner), KXCS2MAPWINNER, KXCS2TOTALMAPS, and KXCS2
    itself (tournament-winner futures) -- confirmed live, see
    kalshi_cs2_client.py's own SERIES_WINNER_SERIES/MAP_WINNER_SERIES/
    TOTAL_MAPS_SERIES/TOURNAMENT_WINNER_SERIES constants."""
    return _fetch_kalshi_series_by_prefix("KXCS2")


def fetch_kalshi_lol_series() -> list[dict]:
    """LoL's own tournament-winner futures series (KXLEAGUEWORLDS) does NOT
    share LoL's other two series' own "KXLOL" prefix (KXLOLMAP/
    KXLOLTOTALMAPS) -- confirmed live, same "one sport, two unrelated
    ticker families" exception Soccer's own KXPREMIERLEAGUE already needed
    for EPL's league-winner series (see fetch_kalshi_soccer_series's own
    docstring)."""
    data = get_json(f"{KALSHI_BASE}/series?category=Sports")
    series = data.get("series", [])
    prefixes = ("KXLOL", "KXLEAGUEWORLDS")
    return [
        {"identifier": s.get("ticker", ""), "title": s.get("title") or s.get("ticker", "")}
        for s in series if s.get("ticker", "").upper().startswith(prefixes)
    ]


def fetch_polymarket_valorant_events() -> list[dict]:
    return _fetch_polymarket_events_by_tag_slugs(["valorant"])


def fetch_polymarket_cs2_events() -> list[dict]:
    """CS2 has real Polymarket inventory (confirmed live: a FaZe tier-1-event
    prop, map-pool-change props, roster-change props -- see
    market_catalog_cs2.py's own docstring on why none of these are real
    match-outcome markets this app builds), all under tag_slug=cs2
    (confirmed live -- "counter-strike"/"csgo" return the same or fewer
    real events, not used)."""
    return _fetch_polymarket_events_by_tag_slugs(["cs2"])


def fetch_polymarket_lol_events() -> list[dict]:
    """LoL's only real Polymarket inventory is season-winner futures
    (LCK/LPL/Worlds -- see market_catalog_lol.py's own docstring), under
    tag_slug=lol (confirmed live -- "league-of-legends" returns the
    identical event set, not used since every other sport's own tag_slug
    here is the short form)."""
    return _fetch_polymarket_events_by_tag_slugs(["lol"])


def fetch_polymarket_tennis_events() -> list[dict]:
    """series_slug shape (same as MMA's fetch above), not tag_slug -- see
    polymarket_tennis_client.py's own SERIES_SLUGS."""
    out: dict[str, dict] = {}
    for series_slug in ("atp", "wta", "itf"):
        events = paginate(
            lambda offset, ss=series_slug: f"{GAMMA_BASE}/events?series_slug={ss}&closed=false&limit=200&offset={offset}",
            list_key=None,
            cursor_style="offset",
        )
        for e in events:
            slug = e.get("slug")
            if slug:
                out[slug] = {"identifier": slug, "title": e.get("title") or slug}
    return list(out.values())


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
}


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
        is_bootstrap = session.query(CatalogEntry).filter_by(sport=sport).count() == 0
        if is_bootstrap:
            bootstrapped_sports.append(sport)
        for platform, fetch_fn in fetchers:
            try:
                items = fetch_fn()
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

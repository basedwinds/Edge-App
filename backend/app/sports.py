"""Single source of truth for the sports this app tracks.

WHY THIS EXISTS. Adding a sport used to mean editing several independent lists,
and every one of them failed SILENTLY when missed. In one session CFB was left
out of three separate places, each with a different symptom and none raising:

  * paper_logger._ENDPOINTS   -> the sport never alerted and never accrued
                                 forward CLV. It looked quiet, not broken, which
                                 is the worst failure mode this app has.
  * frontend rowGameId        -> the per-game bet cap silently did not apply, so
                                 one game could surface several correlated bets.
  * frontend Sidebar          -> the sub-nav showed NFL's links on CFB's pages.

None of those are type errors, so neither mypy nor tsc could catch them. The fix
is to derive the lists from one registry rather than trusting memory, and to make
a missing entry LOUD via check_registry_consistency().

The frontend has its own mirror of this in src/lib/sports.ts -- the two are kept
deliberately parallel, and check_registry_consistency covers the backend half.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Sport:
    key: str
    label: str
    #: markets endpoint the paper logger, alerts and cache warmer all read
    markets_path: str
    #: Market/PlacedBet column linking a row to its real-world event, if any.
    #: None for sports whose markets are not game-tied (racing uses race_event_id
    #: through its own builder).
    game_id_column: str | None
    #: True where the sport exposes a separate /<sport>/futures endpoint.
    #:
    #: DO NOT set this from memory -- check_registry_consistency() now verifies
    #: it against the app's real routing table, because every one of these flags
    #: except CoD's was WRONG. wnba, cfb, mma and racing were all False while
    #: their routers declared /futures, and the comment that used to sit here
    #: asserted CFB's futures were "market TYPES inside /cfb/markets, not a
    #: separate endpoint" -- /cfb/futures returns 1,380 priced rows.
    #:
    #: That drift was not cosmetic. FUTURES_PATHS below feeds the response-cache
    #: warmer, so a sport marked False was never warmed: its futures computed
    #: live on the user's request (racing measured 21.9s), and whatever it
    #: computed -- including all-unpriced, if the model cache was still cold --
    #: was then cached and served for the full TTL.
    has_futures_endpoint: bool
    #: Season-based sports gate their season-long futures on a readiness window
    #: (see paper_logger._SEASON_TABLES). Event-based sports (tennis, mma,
    #: esports, racing) are excluded on purpose -- their futures are
    #: tournament-scoped and only listed once the event is imminent.
    season_table: tuple[str, str] | None = None
    #: Name of the app.db.models class holding this sport's real-world event
    #: (the row game_id_column points at). Stored as a NAME, not the class, so
    #: this module stays import-free of models -- app.db.models imports the DB
    #: layer and a real class here would make the registry unimportable from
    #: anywhere early in startup.
    #:
    #: Consumed by cross_platform_divergence, which needs to look the event up
    #: to decide whether a market is still pre-game. None where the sport has
    #: no single event model (racing keys by RaceEvent through its own branch).
    entity_model: str | None = None


SPORTS: tuple[Sport, ...] = (
    Sport("nfl", "NFL", "/markets", "nfl_game_id", True, ("NflGame", "gameday"), entity_model="NflGame"),
    Sport("nba", "NBA", "/nba/markets", "nba_game_id", True, ("NbaGame", "gameday"), entity_model="NbaGame"),
    Sport("wnba", "WNBA", "/wnba/markets", "wnba_game_id", True, ("WnbaGame", "gameday"), entity_model="WnbaGame"),
    Sport("cfb", "College Football", "/cfb/markets", "cfb_game_id", True, ("CfbGame", "gameday"), entity_model="CfbGame"),
    Sport("mlb", "MLB", "/mlb/markets", "mlb_game_id", True, ("MlbGame", "gameday"), entity_model="MlbGame"),
    Sport("soccer", "Soccer", "/soccer/markets", "soccer_match_id", True, ("SoccerMatch", "match_date"), entity_model="SoccerMatch"),
    # MMA has NO entity_model ON PURPOSE. cross_platform_divergence treats it as
    # never-pregame-safe (a card has no single kickoff instant, the same
    # exclusion clv.py makes), so it must appear in _entity_id but NOT in
    # _ENTITY_MODEL. Leaving entity_model unset is what encodes that.
    Sport("mma", "MMA", "/mma/markets", "mma_fight_id", True),
    Sport("tennis", "Tennis", "/tennis/markets", "tennis_match_id", True, entity_model="TennisMatch"),
    Sport("valorant", "Valorant", "/valorant/markets", "valorant_match_id", True, entity_model="ValorantMatch"),
    Sport("cs2", "CS2", "/cs2/markets", "cs2_match_id", True, entity_model="Cs2Match"),
    Sport("lol", "LoL", "/lol/markets", "lol_match_id", True, entity_model="LolMatch"),
    # has_futures_endpoint FALSE: neither platform lists CoD futures, and no
    # /cod/futures route exists. Declaring True made the bankroll audit query a
    # 404 -- caught the same day, by the audit itself.
    Sport("cod", "Call of Duty", "/cod/markets", "cod_match_id", False, entity_model="CodMatch"),
    # Racing is one markets endpoint covering f1/irl/nascar, and its rows link by
    # race_event_id through racing_markets' own builder rather than the shared
    # game-id path -- hence no game_id_column here.
    Sport("racing", "Racing", "/racing/markets", None, True),
)

BY_KEY: dict[str, Sport] = {s.key: s for s in SPORTS}

#: Every sport's markets endpoint. Derive from this rather than retyping it --
#: paper_logger and the cache warmer both did, and both drifted.
MARKETS_PATHS: tuple[str, ...] = tuple(s.markets_path for s in SPORTS)

#: Every sport's FUTURES endpoint, same reasoning as MARKETS_PATHS and the same
#: failure: the cache warmer hand-listed seven of them and silently omitted
#: wnba, cfb, lol, mma and racing.
#:
#: NFL is the root dashboard: it has no /nfl prefix, so its futures hang off the
#: bare markets path as /markets/futures while every other sport swaps the last
#: segment (/nba/markets -> /nba/futures). Both forms are asserted against the
#: real routing table by check_registry_consistency, so a wrong guess here fails
#: loudly at startup instead of quietly dropping a sport from the warmer.
def _futures_path(markets_path: str) -> str:
    if markets_path == "/markets":
        return "/markets/futures"
    return markets_path.rsplit("/", 1)[0] + "/futures"


FUTURES_PATHS: tuple[str, ...] = tuple(
    _futures_path(s.markets_path) for s in SPORTS if s.has_futures_endpoint
)

#: Season-gated sports -> their (table, date column), for the futures readiness
#: window.
SEASON_TABLES: dict[str, tuple[str, str]] = {
    s.key: s.season_table for s in SPORTS if s.season_table
}

#: sport key -> (game-id column, event model NAME), for every sport whose
#: markets tie to one real-world event. cross_platform_divergence derives both
#: of its per-sport lookups from this instead of hand-listing them -- CoD was
#: missed in exactly that way and the scanner reported a silent zero.
ENTITY_SPORTS: dict[str, tuple[str, str]] = {
    s.key: (s.game_id_column, s.entity_model)
    for s in SPORTS if s.game_id_column and s.entity_model
}

#: sport key -> game-id column, for EVERY game-tied sport (broader than
#: ENTITY_SPORTS: MMA is here but has no entity_model, so it gets a divergence
#: entity id while still being dropped as never-pregame-safe).
ENTITY_ID_COLUMNS: dict[str, str] = {
    s.key: s.game_id_column for s in SPORTS if s.game_id_column
}

#: Columns that tie a Market/PlacedBet row to a real-world event.
GAME_ID_COLUMNS: tuple[str, ...] = tuple(s.game_id_column for s in SPORTS if s.game_id_column)


def check_registry_consistency() -> list[str]:
    """Return a list of problems, empty when the registry agrees with the code
    that consumes it. Called at startup so a half-added sport is LOUD rather
    than silently inert -- the whole point of this module.

    Deliberately checks the things that actually broke, not everything
    imaginable: that every sport's markets endpoint is routed, and that every
    declared game-id column exists on both Market and PlacedBet."""
    problems: list[str] = []
    try:
        from app.db import models as models_module
        from app.db.models import Market, PlacedBet
        market_cols = {c.name for c in Market.__table__.columns}
        bet_cols = {c.name for c in PlacedBet.__table__.columns}
        for s in SPORTS:
            if s.game_id_column and s.game_id_column not in market_cols:
                problems.append(f"{s.key}: Market has no column {s.game_id_column!r}")
            if s.game_id_column and s.game_id_column not in bet_cols:
                problems.append(f"{s.key}: PlacedBet has no column {s.game_id_column!r}")
            # A declared entity_model that does not exist would make the
            # divergence scanner silently skip the sport -- the exact failure
            # this registry exists to make loud.
            if s.entity_model and not hasattr(models_module, s.entity_model):
                problems.append(f"{s.key}: app.db.models has no {s.entity_model!r}")
    except Exception as exc:  # pragma: no cover - defensive only
        problems.append(f"registry check could not inspect models: {exc}")

    # A sport missing from the catalog scanner is INVISIBLE, not broken, which
    # is why this keeps happening -- three times now. Its series fall to the
    # "other" catch-all and get bulk-dismissed as not_relevant in a sweep of
    # untracked sports:
    #   NCAAF  45 series dismissed, six of them actively priced
    #   CS2/LoL  150 already-ingested Polymarket events reported as untracked
    #   CoD    all four KXCOD* series dismissed -- including KXCODGAME, which
    #          is priced, and KXCOD, whose dismissal is the reason a later
    #          check concluded Call of Duty had no futures market at all
    # Nothing errors in any of those cases; the sport simply never appears.
    try:
        from app.ingestion.catalog_scan import _SPORT_FETCHERS
        for s in SPORTS:
            if s.key not in _SPORT_FETCHERS:
                problems.append(
                    f"{s.key}: no catalog_scan fetcher -- its series will fall to the "
                    f"'other' catch-all and be dismissed as not_relevant"
                )
    except Exception as exc:  # pragma: no cover - defensive only
        problems.append(f"registry check could not inspect catalog_scan: {exc}")

    # A matcher that has fallen behind its own ingester hides live, priced
    # markets under the "other" catch-all -- see catalog_scan.client_matcher_drift
    # for the two cases this found on the day it was written.
    try:
        from app.ingestion.catalog_scan import client_matcher_drift
        problems.extend(client_matcher_drift())
    except Exception as exc:  # pragma: no cover - defensive only
        problems.append(f"registry check could not audit matcher drift: {exc}")

    # has_futures_endpoint vs the REAL routing table.
    #
    # Checked against routes rather than trusted, because the hand-maintained
    # version was wrong for four of the five sports that carried it: wnba, cfb,
    # mma and racing all declared /futures in their routers while the registry
    # said False. Nothing raised -- the flag's only consumer was FUTURES_PATHS,
    # which feeds the cache warmer, so being missing just meant "never warmed".
    # Those endpoints then computed live on the user's request (racing measured
    # 21.9s) and cached whatever came back, including an all-unpriced payload
    # built while the model cache was still cold. That is the mechanism behind
    # "futures show up blank": not a pricing failure, a warming gap.
    #
    # Direction matters both ways. A route with no flag is a sport silently
    # dropped from the warmer; a flag with no route makes the warmer self-HTTP a
    # 404 every 90s (exactly what a wrongly-True CoD did once already).
    # Walk the routers PACKAGE rather than app.main's include_router calls:
    # app.main imports this module, so importing it back would be circular, and
    # a hand-listed module map would be one more thing to drift. pkgutil finds
    # every router whether or not main remembered to include it.
    try:
        import importlib
        import pkgutil

        from app.api import routers as routers_pkg

        routed: set[str] = set()
        for mod_info in pkgutil.iter_modules(routers_pkg.__path__):
            mod = importlib.import_module(f"{routers_pkg.__name__}.{mod_info.name}")
            r = getattr(mod, "router", None)
            if r is None:
                continue
            # APIRouter applies its prefix when the route is DECORATED, so
            # route.path is already the full path -- prepending r.prefix here
            # yields /racing/racing/futures and matches nothing.
            for route in getattr(r, "routes", []):
                path = getattr(route, "path", "")
                if path.endswith("/futures") and "GET" in (getattr(route, "methods", None) or set()):
                    routed.add(path)
        for s in SPORTS:
            path = _futures_path(s.markets_path)
            if s.has_futures_endpoint and path not in routed:
                problems.append(
                    f"{s.key}: has_futures_endpoint=True but no GET {path} route -- "
                    f"the cache warmer will self-HTTP a 404 every 90s"
                )
            if not s.has_futures_endpoint and path in routed:
                problems.append(
                    f"{s.key}: GET {path} exists but has_futures_endpoint=False -- "
                    f"it is missing from FUTURES_PATHS, so it is never cache-warmed "
                    f"and computes live (and may cache an unpriced payload)"
                )
    except Exception as exc:  # pragma: no cover - defensive only
        problems.append(f"registry check could not inspect routers: {exc}")
    return problems

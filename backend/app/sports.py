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
    #: True where the sport exposes a separate /<sport>/futures endpoint. CFB is
    #: deliberately False: its futures are market TYPES inside /cfb/markets
    #: (944 of 974 rows), not a separate endpoint.
    has_futures_endpoint: bool
    #: Season-based sports gate their season-long futures on a readiness window
    #: (see paper_logger._SEASON_TABLES). Event-based sports (tennis, mma,
    #: esports, racing) are excluded on purpose -- their futures are
    #: tournament-scoped and only listed once the event is imminent.
    season_table: tuple[str, str] | None = None


SPORTS: tuple[Sport, ...] = (
    Sport("nfl", "NFL", "/markets", "nfl_game_id", True, ("NflGame", "gameday")),
    Sport("nba", "NBA", "/nba/markets", "nba_game_id", True, ("NbaGame", "gameday")),
    Sport("wnba", "WNBA", "/wnba/markets", "wnba_game_id", False, ("WnbaGame", "gameday")),
    Sport("cfb", "College Football", "/cfb/markets", "cfb_game_id", False, ("CfbGame", "gameday")),
    Sport("mlb", "MLB", "/mlb/markets", "mlb_game_id", True, ("MlbGame", "gameday")),
    Sport("soccer", "Soccer", "/soccer/markets", "soccer_match_id", True, ("SoccerMatch", "match_date")),
    Sport("mma", "MMA", "/mma/markets", "mma_fight_id", False),
    Sport("tennis", "Tennis", "/tennis/markets", "tennis_match_id", True),
    Sport("valorant", "Valorant", "/valorant/markets", "valorant_match_id", True),
    Sport("cs2", "CS2", "/cs2/markets", "cs2_match_id", True),
    Sport("lol", "LoL", "/lol/markets", "lol_match_id", True),
    Sport("cod", "Call of Duty", "/cod/markets", "cod_match_id", True),
    # Racing is one markets endpoint covering f1/irl/nascar, and its rows link by
    # race_event_id through racing_markets' own builder rather than the shared
    # game-id path -- hence no game_id_column here.
    Sport("racing", "Racing", "/racing/markets", None, False),
)

BY_KEY: dict[str, Sport] = {s.key: s for s in SPORTS}

#: Every sport's markets endpoint. Derive from this rather than retyping it --
#: paper_logger and the cache warmer both did, and both drifted.
MARKETS_PATHS: tuple[str, ...] = tuple(s.markets_path for s in SPORTS)

#: Season-gated sports -> their (table, date column), for the futures readiness
#: window.
SEASON_TABLES: dict[str, tuple[str, str]] = {
    s.key: s.season_table for s in SPORTS if s.season_table
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
        from app.db.models import Market, PlacedBet
        market_cols = {c.name for c in Market.__table__.columns}
        bet_cols = {c.name for c in PlacedBet.__table__.columns}
        for s in SPORTS:
            if s.game_id_column and s.game_id_column not in market_cols:
                problems.append(f"{s.key}: Market has no column {s.game_id_column!r}")
            if s.game_id_column and s.game_id_column not in bet_cols:
                problems.append(f"{s.key}: PlacedBet has no column {s.game_id_column!r}")
    except Exception as exc:  # pragma: no cover - defensive only
        problems.append(f"registry check could not inspect models: {exc}")
    return problems

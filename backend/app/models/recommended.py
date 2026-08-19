"""Reproduce the frontend Recommended-bets tab (markets.ts buildRecommendedBets
pipeline) in the backend, so the Discord alert can fire on EXACTLY the set the
tab shows. This mirrors the "All Recommended Bets" cross-sport GAMES view
(pages/Combined.tsx loadCombined): every sport's /markets rows, games only
(futures live on their own pages), each sport sized against its own weekly pool.

MUST stay byte-identical to markets.ts -- verified by diffing the /recommended
endpoint against the live tab, sport by sport (see scripts/verify_recommended).

Pipeline per sport (identical order to buildRecommendedBets):
  1. candidates: rows the app actually staked (suggested_stake_dollars set)
  2. ladder-collapse (best-edge rung per ladder / per game-ladder, per source)
  3. cross-platform collapse (higher volume, then higher edge)
  4. sort by edge desc
  5. per-player cap (best stat line per player) -- no-op for game markets
  6. per-game cap (one best-edge row per real game)
  7. per-pool budget cap (stop once weekly pool * 0.6 is committed)
"""
import logging

import httpx

from app.api.routers.placed_bets import _cross_platform_key

log = logging.getLogger("recommended")

_BASE = "http://127.0.0.1:8756"
_CEILING_PCT = 0.6  # markets.ts PORTFOLIO_CEILING_PCT

_LADDER = {"win_total", "wins_any", "division_wins", "season_pass_yds", "season_rush_yds",
           "season_rec_yds", "season_rush_tds", "season_rec_tds", "season_rec"}
_GAME_LADDER = {"spread", "total", "team_total", "spread_1h", "spread_2h", "total_1h", "total_2h"}
# Esports SERIES ladders, per title -- mirrors markets.ts's VALORANT/CS2/LOL/
# COD_LADDER_TYPES. Polymarket lists "O/U N.5 maps" at several lines on one
# series and those rungs are the same real proposition, so the frontend
# collapses them; this file did not, because series_total is in neither _LADDER
# nor _GAME_LADDER and so fell through to the never-collapse branch. That made
# the mirror a SUPERSET of the board for cs2/lol/valorant -- the permissive
# direction, but still drift.
_ESPORTS_LADDER = {
    "valorant": {"series_total", "series_handicap"},   # only Valorant has a handicap type
    "cs2": {"series_total"},
    "lol": {"series_total"},
    "cod": {"series_total"},
}
_PLAYER_STAT = {"season_pass_yds", "season_rush_yds", "season_rec_yds", "season_rush_tds",
                "season_rec_tds", "season_rec"}
# Season champion futures belong on the Futures page, not the cross-sport games
# view -- buildRacingRecommendedBets filters them out (CHAMP).
_RACING_CHAMP = {"drivers_champion", "constructors_champion"}
# Mirrors frontend MAX_RACING_BETS_PER_EVENT -- keep the two in lockstep.
_MAX_RACING_BETS_PER_EVENT = 3

# (sport, /markets endpoint, weekly-pool settings field). Games view = weekly pool
# only (Combined passes futures=[]), so no futures pool needed. Order + fields
# mirror loadCombined's 11 builder calls exactly.
# (sport, endpoint, weekly pool key, futures pool key or None).
#
# THE FUTURES KEY EXISTS BECAUSE CFB NEEDED IT (2026-08-19). The frontend
# builders that receive season rows cap weekly and futures against SEPARATE
# ceilings -- "capping them against the WEEKLY ceiling would let a slate of
# games crowd out every futures row, or vice versa" (markets.ts). This mirror
# had one ceiling for everything, which was harmless only while no sport put
# staked futures rows on its /markets endpoint. CFB now does: 31 of its 39
# staked rows are stake_pool "futures".
#
# None means the frontend builder for that sport takes no futures pool
# (mma/tennis/soccer/racing), and futures rows are then capped as weekly --
# exactly what a single-ceiling builder does.
_SPORTS = [
    ("nfl", "/markets", "weekly_pool_dollars", "futures_pool_dollars"),
    ("nba", "/nba/markets", "nba_weekly_pool_dollars", "nba_futures_pool_dollars"),
    ("wnba", "/wnba/markets", "wnba_weekly_pool_dollars", "wnba_futures_pool_dollars"),
    # CFB WAS MISSING ENTIRELY until 2026-08-19, while Combined.tsx's
    # loadCombined has always included it -- so every CFB bet the app showed and
    # staked was scored was_recommended=False and never reached the Discord
    # alert. Invisible while CFB was tracked-not-staked (#208); the moment that
    # lifted it would have mislabelled exactly the evidence the lift exists to
    # collect.
    ("cfb", "/cfb/markets", "cfb_weekly_pool_dollars", "cfb_futures_pool_dollars"),
    ("mlb", "/mlb/markets", "mlb_weekly_pool_dollars", "mlb_futures_pool_dollars"),
    ("mma", "/mma/markets", "mma_weekly_pool_dollars", None),
    ("tennis", "/tennis/markets", "tennis_weekly_pool_dollars", None),
    ("soccer", "/soccer/markets", "soccer_weekly_pool_dollars", None),
    ("valorant", "/valorant/markets", "valorant_weekly_pool_dollars", "valorant_futures_pool_dollars"),
    ("cs2", "/cs2/markets", "cs2_weekly_pool_dollars", "cs2_futures_pool_dollars"),
    ("lol", "/lol/markets", "lol_weekly_pool_dollars", "lol_futures_pool_dollars"),
    ("racing", "/racing/markets", "racing_weekly_pool_dollars", None),
    # CoD, added 2026-08-19 once its pipeline was VERIFIED rather than guessed:
    # buildCodRecommendedBets delegates to the SAME
    # buildEsportsTitleRecommendedBets as valorant/cs2/lol, with
    # COD_LADDER_TYPES = {"series_total"} -- so it takes the shared builder,
    # exactly like its three siblings.
    ("cod", "/cod/markets", "cod_weekly_pool_dollars", "cod_futures_pool_dollars"),
]
# per-sport JSON key that carries the game/match id
_GID_KEY = {
    "nfl": "nfl_game_id", "nba": "nba_game_id", "wnba": "wnba_game_id", "mlb": "mlb_game_id",
    "cfb": "cfb_game_id", "cod": "cod_match_id",
    "mma": "mma_fight_id", "tennis": "tennis_match_id", "soccer": "soccer_match_id",
    "valorant": "valorant_match_id", "cs2": "cs2_match_id", "lol": "lol_match_id",
    "racing": "race_event_id",
}


class _Row:
    """A priced /markets row + its sport, exposing the attribute names the shared
    key functions (_cross_platform_key etc.) read."""

    __slots__ = ("id", "sport", "market_type", "source", "team", "line", "side",
                 "label", "edge", "volume", "stake", "stake_pool", "gameday", "event", "model_prob",
                 "nfl_game_id", "nba_game_id", "wnba_game_id", "cfb_game_id", "mlb_game_id", "mma_fight_id",
                 "tennis_match_id", "soccer_match_id", "valorant_match_id", "cs2_match_id",
                 "lol_match_id", "cod_match_id", "race_event_id")

    def __init__(self, sport: str, d: dict):
        self.id = d.get("id")
        self.sport = sport
        self.market_type = d.get("market_type") or ""
        self.source = d.get("source") or ""
        self.team = d.get("team")
        self.line = d.get("line")
        self.side = d.get("side")
        self.label = d.get("game_label") or d.get("group_label") or self.market_type
        self.edge = d.get("edge")
        self.model_prob = d.get("model_prob")
        self.volume = d.get("volume")
        self.stake = d.get("suggested_stake_dollars")
        self.stake_pool = d.get("stake_pool")
        self.gameday = d.get("gameday")
        if sport == "racing":
            # buildRacingRecommendedBets uses its OWN row shape: team=driver,
            # sport=series (f1/nascar/irl), gameday=close_time date, and it never
            # touches the shared ladder/cross-platform/game-cap passes.
            self.team = d.get("driver")
            self.sport = d.get("series") or "f1"
            ct = d.get("close_time")
            self.gameday = ct[:10] if ct else None
            self.event = d.get("race_event_id") if d.get("race_event_id") is not None else (d.get("event") or "")
        for a in ("nfl_game_id", "nba_game_id", "wnba_game_id", "cfb_game_id", "mlb_game_id", "mma_fight_id",
                  "tennis_match_id", "soccer_match_id", "valorant_match_id", "cs2_match_id",
                  "lol_match_id", "cod_match_id", "race_event_id"):
            setattr(self, a, d.get(a))


def _edge(r: _Row) -> float:
    return r.edge or 0.0


def _ladder_key(r: _Row) -> str:
    esports = _ESPORTS_LADDER.get(r.sport)
    if esports and r.market_type in esports:           # esports series ladder
        mid = (r.valorant_match_id or r.cs2_match_id or r.lol_match_id
               or r.cod_match_id or "")
        return f"{r.market_type}|{r.source}|{mid}|{r.team or ''}|{r.side or ''}"
    if r.market_type in _LADDER:                       # season ladder -> recommendedKey
        return f"{r.market_type}|{r.source}|{r.team if r.team is not None else r.label}"
    if r.market_type in _GAME_LADDER:                  # game ladder -> gameLadderKey
        gid = r.nfl_game_id or r.nba_game_id or r.mlb_game_id or r.label
        return f"{r.market_type}|{r.source}|{gid}|{r.team or ''}"
    return f"market-{r.id}"                             # unique -> never collapses


def _prefer(cand: _Row, exist: _Row) -> bool:
    cv, ev = cand.volume or 0, exist.volume or 0
    if cv != ev:
        return cv > ev
    return _edge(cand) > _edge(exist)


def _game_cap_id(r: _Row):
    # capToOneRowPerGame: NOTE wnba + racing are intentionally excluded (pass through)
    # cfb_game_id ADDED 2026-08-19. This is the hand-kept chain form, and the
    # frontend's own rowGameId comment records that "the chain form is what
    # silently dropped CFB: a sport missing from it skipped the per-game cap
    # entirely, so one game could surface several correlated bets as if they
    # were independent". The frontend fixed that by deriving from its sport
    # registry; this copy still had the original defect.
    return (r.nfl_game_id or r.nba_game_id or r.cfb_game_id or r.mlb_game_id
            or r.mma_fight_id or r.tennis_match_id
            or r.soccer_match_id
            or (f"valorant:{r.valorant_match_id}" if r.valorant_match_id else None)
            or (f"cs2:{r.cs2_match_id}" if r.cs2_match_id else None)
            or (f"lol:{r.lol_match_id}" if r.lol_match_id else None)
            or (f"cod:{r.cod_match_id}" if r.cod_match_id else None))


def _pool_cap(rows: list[_Row], weekly_pool: float,
              futures_pool: float | None) -> list[_Row]:
    """The frontend's portfolio cap, with SEPARATE weekly and futures ceilings.

    futures_pool None means this sport's frontend builder takes no futures pool,
    so everything is capped as weekly -- identical to the single-ceiling form
    this file used for every sport before CFB needed the split. Passing a real
    number turns on the two-ceiling behaviour markets.ts uses, whose own comment
    is the reason it exists: capping season rows against the weekly ceiling
    "would let a slate of games crowd out every futures row, or vice versa"."""
    two = futures_pool is not None
    ceil = {"weekly": max(0.0, (weekly_pool or 0.0) * _CEILING_PCT),
            "futures": max(0.0, (futures_pool or 0.0) * _CEILING_PCT)}
    cum = {"weekly": 0.0, "futures": 0.0}
    shown: list[_Row] = []
    for r in rows:
        pool = "futures" if (two and r.stake_pool == "futures") else "weekly"
        stake = r.stake or 0.0
        if cum[pool] + stake > ceil[pool]:
            continue
        cum[pool] += stake
        shown.append(r)
    return shown


def _build_cfb(rows: list[_Row], weekly_pool: float,
               futures_pool: float | None = None) -> list[_Row]:
    """Mirror buildCfbRecommendedBets, which matches NEITHER existing builder.

    Like WNBA's it skips the ladder-collapse and per-player passes entirely
    (markets.ts says so outright: "ladder or player-stat markets yet"), so
    running _build_sport here would over-collapse. UNLIKE WNBA's it keeps the
    REAL line -- WNBA forces line and side to null so every rung of a ladder
    collapses into one key by construction, whereas CFB carries m.line and only
    side is null. And unlike either, it caps weekly and futures separately.

    So: cross-platform collapse -> per-game cap -> two-pool budget cap."""
    for r in rows:                      # the builder sets side: null, keeps line
        r.side = None
    cands = [r for r in rows if r.stake]
    xp: dict[str, _Row] = {}
    for r in cands:
        k = _cross_platform_key(r)
        if k not in xp or _prefer(r, xp[k]):
            xp[k] = r
    deduped = sorted(xp.values(), key=_edge, reverse=True)
    game_best: dict[str, _Row] = {}
    non_game: list[_Row] = []
    for r in deduped:
        gid = _game_cap_id(r)
        if gid is None:
            non_game.append(r)
            continue
        gid = str(gid)
        if gid not in game_best or _edge(r) > _edge(game_best[gid]):
            game_best[gid] = r
    after_game = sorted(non_game + list(game_best.values()), key=_edge, reverse=True)
    return _pool_cap(after_game, weekly_pool, futures_pool)


def _build_racing(rows: list[_Row], weekly_pool: float, futures_pool: float | None = None) -> list[_Row]:
    """Mirror buildRacingRecommendedBets, which does NOT use the shared pipeline:
    staked rows -> ONE bet per race event (best stake wins, ties by first) ->
    budget cap that always keeps the first row (`&& rows.length > 0`)."""
    # JS filter: PER-RACE markets only -- season champion futures belong on the
    # Futures page -- and both model_prob + edge must be real.
    staked = [r for r in rows
              if r.market_type not in _RACING_CHAMP and (r.stake or 0) > 0
              and r.model_prob is not None and r.edge is not None]
    # Mirrors the frontend byStakeThenEdge + per-DRIVER / per-race caps (see
    # buildRacingRecommendedBets for why racing doesn't use the one-row-per-game
    # rule: podium bets on different drivers can all win, so they diversify; the
    # same driver across markets is the real correlation). Both sorts are stable,
    # so ties keep the original /racing order in Python and JS alike.
    staked = sorted(staked, key=lambda r: (-(r.stake or 0.0), -(r.edge or 0.0)))
    per_event: dict[str, int] = {}
    seen_driver: set[str] = set()
    deduped: list[_Row] = []
    for r in staked:
        event_key = f"{r.sport}|{r.event if r.event is not None else ''}"
        driver_key = f"{event_key}|{r.team or ''}"          # _Row.team is the driver for racing
        if driver_key in seen_driver:
            continue
        if per_event.get(event_key, 0) >= _MAX_RACING_BETS_PER_EVENT:
            continue
        seen_driver.add(driver_key)
        per_event[event_key] = per_event.get(event_key, 0) + 1
        deduped.append(r)
    ceiling = max(0.0, (weekly_pool or 0.0) * _CEILING_PCT)
    out: list[_Row] = []
    running = 0.0
    for r in deduped:
        stake = r.stake or 0.0
        if running + stake > ceiling and out:              # JS keeps the first row regardless
            continue
        running += stake
        out.append(r)
    return out


def _build_wnba(rows: list[_Row], weekly_pool: float, futures_pool: float | None = None) -> list[_Row]:
    """Mirror buildWnbaRecommendedBets, which is a LEANER variant of the shared
    pipeline: it builds candidates with `line` and `side` forced to null (so every
    rung of a game's ladder collapses into one cross-platform key by construction)
    and runs ONLY cross-platform collapse -> per-game cap -> budget cap, skipping
    the ladder-collapse and per-player passes entirely. Keeping the real line here
    would split rows the frontend merges, so alerts and the app would disagree the
    moment WNBA has staked markets."""
    for r in rows:                      # match the builder's null line/side
        r.line = None
        r.side = None
    cands = [r for r in rows if r.stake]
    xp: dict[str, _Row] = {}
    for r in cands:
        k = _cross_platform_key(r)
        if k not in xp or _prefer(r, xp[k]):
            xp[k] = r
    deduped = sorted(xp.values(), key=_edge, reverse=True)
    game_best: dict[str, _Row] = {}
    non_game: list[_Row] = []
    for r in deduped:
        gid = _game_cap_id(r)           # wnba_game_id is NOT in it -> all pass through
        if gid is None:
            non_game.append(r)
            continue
        gid = str(gid)
        if gid not in game_best or _edge(r) > _edge(game_best[gid]):
            game_best[gid] = r
    after_game = sorted(non_game + list(game_best.values()), key=_edge, reverse=True)
    return _pool_cap(after_game, weekly_pool, futures_pool)


def _build_sport(rows: list[_Row], weekly_pool: float, futures_pool: float | None = None) -> list[_Row]:
    # 1. candidates = staked rows
    cands = [r for r in rows if r.stake]
    # 2. ladder-collapse (best edge per ladder key)
    ladder: dict[str, _Row] = {}
    for r in cands:
        k = _ladder_key(r)
        if k not in ladder or _edge(r) > _edge(ladder[k]):
            ladder[k] = r
    # 3. cross-platform collapse
    xp: dict[str, _Row] = {}
    for r in ladder.values():
        k = _cross_platform_key(r)
        if k not in xp or _prefer(r, xp[k]):
            xp[k] = r
    deduped = sorted(xp.values(), key=_edge, reverse=True)
    # 5. per-player cap
    player_best: dict[str, _Row] = {}
    non_player: list[_Row] = []
    for r in deduped:
        if r.market_type not in _PLAYER_STAT:
            non_player.append(r)
            continue
        pk = r.team if r.team is not None else r.label
        if pk not in player_best or _edge(r) > _edge(player_best[pk]):
            player_best[pk] = r
    after_player = sorted(non_player + list(player_best.values()), key=_edge, reverse=True)
    # 6. per-game cap
    game_best: dict[str, _Row] = {}
    non_game: list[_Row] = []
    for r in after_player:
        gid = _game_cap_id(r)
        if gid is None:
            non_game.append(r)
            continue
        gid = str(gid)
        if gid not in game_best or _edge(r) > _edge(game_best[gid]):
            game_best[gid] = r
    after_game = sorted(non_game + list(game_best.values()), key=_edge, reverse=True)
    # 7. weekly-pool budget cap (games view: everything is the weekly pool)
    return _pool_cap(after_game, weekly_pool, futures_pool)


def _builder_for(sport: str):
    """Not every sport uses the shared pipeline -- racing and WNBA have their own
    leaner frontend builders (see each function). Dispatching in ONE place means
    the live path and the snapshot/verification path can't drift apart, which they
    silently did once (the live path kept calling _build_sport for racing)."""
    return {"racing": _build_racing, "wnba": _build_wnba,
            "cfb": _build_cfb}.get(sport, _build_sport)


def _fetch(client: httpx.Client, ep: str, failures: list | None = None) -> list[dict]:
    try:
        r = client.get(f"{_BASE}{ep}")
        return r.json() if r.status_code == 200 else []
    except Exception:
        log.exception("recommended fetch failed for %s", ep)
        # REPORT IT, do not just log it. Returning [] alone makes a PARTIAL
        # set indistinguishable from a genuinely small one, and callers that
        # record membership (paper_logger.was_recommended) would then mark
        # genuinely-recommended bets as False. Observed live: one sport
        # timed out and the set came back short with no signal at all.
        if failures is not None:
            failures.append(ep)
        return []


def _fetch_readiness(client: httpx.Client) -> dict:
    try:
        r = client.get(f"{_BASE}/markets/readiness")
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def _not_ready(r: _Row, rd: dict) -> bool:
    """Mirror frontend isRowNotReady (RecommendedBetsTable filters `data` with it,
    AFTER the budget cap): a far-future game (kickoff beyond game_window_days) or a
    season-sport future whose season isn't active/near. Fails open when unknown."""
    if not rd:
        return False
    if r.gameday:
        try:
            from datetime import datetime
            days = (datetime.fromisoformat(r.gameday) - datetime.now()).total_seconds() / 86400.0
        except (ValueError, TypeError):
            return False
        return days > rd.get("game_window_days", 14)
    if r.sport in rd.get("season_sports", []):
        return rd.get("season_active", {}).get(r.sport) is not True
    return False


def compute_recommended(settings: dict, snapshot: dict | None = None,
                        failures: list | None = None) -> list[_Row]:
    """The full cross-sport Recommended GAMES set (the 'All Recommended Bets'
    tab), as a flat list sorted by stake desc -- identical to loadCombined +
    RecommendedBetsTable's readiness filter.

    `snapshot` (verification only): {sport: [market rows], "readiness": {...}} to
    compute from FROZEN data instead of live endpoints, so a diff against the
    frontend measures logic differences rather than 5-min price drift.

    `failures` (optional): pass a list to be told WHICH endpoints failed. A
    caller that records membership must treat a non-empty list as "unknown"
    rather than "not recommended" -- see paper_logger.was_recommended."""
    out: list[_Row] = []
    if snapshot is not None:
        rd = snapshot.get("readiness") or {}
        for sport, _ep, pool_key, fut_key in _SPORTS:
            rows = [_Row(sport, d) for d in snapshot.get(sport, [])]
            out.extend(_builder_for(sport)(
                rows, settings.get(pool_key) or 0.0,
                (settings.get(fut_key) or 0.0) if fut_key else None))
    else:
        from app.shutdown import is_shutting_down

        with httpx.Client(timeout=90.0) as client:
            rd = _fetch_readiness(client)
            for sport, ep, pool_key, fut_key in _SPORTS:
                if is_shutting_down():  # see app/shutdown.py -- unkillable worker
                    break
                rows = [_Row(sport, d) for d in _fetch(client, ep, failures)]
                out.extend(_builder_for(sport)(
                    rows, settings.get(pool_key) or 0.0,
                    (settings.get(fut_key) or 0.0) if fut_key else None))
    out = [r for r in out if not _not_ready(r, rd)]  # readiness runs at display, after budget cap
    out.sort(key=lambda r: -(r.stake or 0.0))
    return out


def recommended_keys(settings: dict) -> set[str]:
    """Cross-platform keys of every bet in the Recommended tab -- the set the
    Discord alert should match."""
    return {_cross_platform_key(r) for r in compute_recommended(settings)}

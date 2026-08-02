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
_PLAYER_STAT = {"season_pass_yds", "season_rush_yds", "season_rec_yds", "season_rush_tds",
                "season_rec_tds", "season_rec"}
# Season champion futures belong on the Futures page, not the cross-sport games
# view -- buildRacingRecommendedBets filters them out (CHAMP).
_RACING_CHAMP = {"drivers_champion", "constructors_champion"}

# (sport, /markets endpoint, weekly-pool settings field). Games view = weekly pool
# only (Combined passes futures=[]), so no futures pool needed. Order + fields
# mirror loadCombined's 11 builder calls exactly.
_SPORTS = [
    ("nfl", "/markets", "weekly_pool_dollars"),
    ("nba", "/nba/markets", "nba_weekly_pool_dollars"),
    ("wnba", "/wnba/markets", "wnba_weekly_pool_dollars"),
    ("mlb", "/mlb/markets", "mlb_weekly_pool_dollars"),
    ("mma", "/mma/markets", "mma_weekly_pool_dollars"),
    ("tennis", "/tennis/markets", "tennis_weekly_pool_dollars"),
    ("soccer", "/soccer/markets", "soccer_weekly_pool_dollars"),
    ("valorant", "/valorant/markets", "valorant_weekly_pool_dollars"),
    ("cs2", "/cs2/markets", "cs2_weekly_pool_dollars"),
    ("lol", "/lol/markets", "lol_weekly_pool_dollars"),
    ("racing", "/racing/markets", "racing_weekly_pool_dollars"),
]
# per-sport JSON key that carries the game/match id
_GID_KEY = {
    "nfl": "nfl_game_id", "nba": "nba_game_id", "wnba": "wnba_game_id", "mlb": "mlb_game_id",
    "mma": "mma_fight_id", "tennis": "tennis_match_id", "soccer": "soccer_match_id",
    "valorant": "valorant_match_id", "cs2": "cs2_match_id", "lol": "lol_match_id",
    "racing": "race_event_id",
}


class _Row:
    """A priced /markets row + its sport, exposing the attribute names the shared
    key functions (_cross_platform_key etc.) read."""

    __slots__ = ("id", "sport", "market_type", "source", "team", "line", "side",
                 "label", "edge", "volume", "stake", "stake_pool", "gameday", "event", "model_prob",
                 "nfl_game_id", "nba_game_id", "wnba_game_id", "mlb_game_id", "mma_fight_id",
                 "tennis_match_id", "soccer_match_id", "valorant_match_id", "cs2_match_id",
                 "lol_match_id", "race_event_id")

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
        for a in ("nfl_game_id", "nba_game_id", "wnba_game_id", "mlb_game_id", "mma_fight_id",
                  "tennis_match_id", "soccer_match_id", "valorant_match_id", "cs2_match_id",
                  "lol_match_id", "race_event_id"):
            setattr(self, a, d.get(a))


def _edge(r: _Row) -> float:
    return r.edge or 0.0


def _ladder_key(r: _Row) -> str:
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
    return (r.nfl_game_id or r.nba_game_id or r.mlb_game_id or r.mma_fight_id or r.tennis_match_id
            or r.soccer_match_id
            or (f"valorant:{r.valorant_match_id}" if r.valorant_match_id else None)
            or (f"cs2:{r.cs2_match_id}" if r.cs2_match_id else None)
            or (f"lol:{r.lol_match_id}" if r.lol_match_id else None))


def _build_racing(rows: list[_Row], weekly_pool: float) -> list[_Row]:
    """Mirror buildRacingRecommendedBets, which does NOT use the shared pipeline:
    staked rows -> ONE bet per race event (best stake wins, ties by first) ->
    budget cap that always keeps the first row (`&& rows.length > 0`)."""
    # JS filter: PER-RACE markets only -- season champion futures belong on the
    # Futures page -- and both model_prob + edge must be real.
    staked = [r for r in rows
              if r.market_type not in _RACING_CHAMP and (r.stake or 0) > 0
              and r.model_prob is not None and r.edge is not None]
    # JS: [...staked].sort(byStake) -- Array.sort is STABLE, so rows tied on stake
    # keep their original /racing order; the first such row wins the event. Python's
    # sort is stable too, so sorting the rows in their original order matches.
    staked = sorted(staked, key=lambda r: -(r.stake or 0.0))
    best: dict[str, _Row] = {}
    for r in staked:
        k = f"{r.sport}|{r.event if r.event is not None else ''}"
        if k not in best:
            best[k] = r
    # JS: deduped.sort(byStake) on the Map's insertion order (= the order events
    # were first seen above), then the cap walks that order.
    deduped = sorted(best.values(), key=lambda r: -(r.stake or 0.0))
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


def _build_wnba(rows: list[_Row], weekly_pool: float) -> list[_Row]:
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
    ceiling = max(0.0, (weekly_pool or 0.0) * _CEILING_PCT)
    cumulative = 0.0
    shown: list[_Row] = []
    for r in after_game:
        if cumulative + (r.stake or 0.0) > ceiling:
            continue
        cumulative += r.stake or 0.0
        shown.append(r)
    return shown


def _build_sport(rows: list[_Row], weekly_pool: float) -> list[_Row]:
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
    ceiling = max(0.0, (weekly_pool or 0.0) * _CEILING_PCT)
    cumulative = 0.0
    shown: list[_Row] = []
    for r in after_game:
        if cumulative + (r.stake or 0.0) > ceiling:
            continue
        cumulative += r.stake or 0.0
        shown.append(r)
    return shown


def _builder_for(sport: str):
    """Not every sport uses the shared pipeline -- racing and WNBA have their own
    leaner frontend builders (see each function). Dispatching in ONE place means
    the live path and the snapshot/verification path can't drift apart, which they
    silently did once (the live path kept calling _build_sport for racing)."""
    return {"racing": _build_racing, "wnba": _build_wnba}.get(sport, _build_sport)


def _fetch(client: httpx.Client, ep: str) -> list[dict]:
    try:
        r = client.get(f"{_BASE}{ep}")
        return r.json() if r.status_code == 200 else []
    except Exception:
        log.exception("recommended fetch failed for %s", ep)
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


def compute_recommended(settings: dict, snapshot: dict | None = None) -> list[_Row]:
    """The full cross-sport Recommended GAMES set (the 'All Recommended Bets'
    tab), as a flat list sorted by stake desc -- identical to loadCombined +
    RecommendedBetsTable's readiness filter.

    `snapshot` (verification only): {sport: [market rows], "readiness": {...}} to
    compute from FROZEN data instead of live endpoints, so a diff against the
    frontend measures logic differences rather than 5-min price drift."""
    out: list[_Row] = []
    if snapshot is not None:
        rd = snapshot.get("readiness") or {}
        for sport, _ep, pool_key in _SPORTS:
            rows = [_Row(sport, d) for d in snapshot.get(sport, [])]
            out.extend(_builder_for(sport)(rows, settings.get(pool_key) or 0.0))
    else:
        with httpx.Client(timeout=90.0) as client:
            rd = _fetch_readiness(client)
            for sport, ep, pool_key in _SPORTS:
                rows = [_Row(sport, d) for d in _fetch(client, ep)]
                out.extend(_builder_for(sport)(rows, settings.get(pool_key) or 0.0))
    out = [r for r in out if not _not_ready(r, rd)]  # readiness runs at display, after budget cap
    out.sort(key=lambda r: -(r.stake or 0.0))
    return out


def recommended_keys(settings: dict) -> set[str]:
    """Cross-platform keys of every bet in the Recommended tab -- the set the
    Discord alert should match."""
    return {_cross_platform_key(r) for r in compute_recommended(settings)}

"""Cross-platform (Kalshi vs Polymarket) divergence scanner -- the one edge
source that DOESN'T depend on model quality. When the SAME real-world
proposition is priced differently on the two platforms, that gap is close to
free: buy the cheap side, sell/avoid the rich side, and you profit on
convergence regardless of whether this app's Elo is any good.

Scoped to GAME-TIED markets (moneyline/spread/total/team_total/per-map etc.),
where both platforms' markets carry THIS app's own canonical game/match id
(assigned during ingestion by each sport's matcher) -- so a shared id is a
reliable "same game" guarantee, unlike futures where cross-platform team-name
matching is noisier (left as a future extension). A pair is only surfaced when
BOTH sides have real trading (has_real_trading) -- an untraded seed quote on
either platform would manufacture a phantom gap, the same class of bug
staking.py::has_real_trading was built to catch.

This is a DETECTOR, not an auto-trader: it ranks candidates for a human to act
on, same posture as the rest of this app (model_validated:false everywhere).
"""
import datetime
from collections import defaultdict

from sqlalchemy.orm import Session

from app.db.models import (
    Cs2Match, LolMatch, Market, MlbGame, NbaGame, NflGame, SoccerMatch,
    TennisMatch, ValorantMatch, WnbaGame,
)
from app.models.clv import _game_is_final, _game_kickoff_dt
from app.models.staking import has_real_trading

_ENTITY_MODEL = {
    "nfl": NflGame, "nba": NbaGame, "wnba": WnbaGame, "mlb": MlbGame,
    "tennis": TennisMatch, "soccer": SoccerMatch,
    "valorant": ValorantMatch, "cs2": Cs2Match, "lol": LolMatch,
}


def _is_pregame(session: Session, entity_id: str) -> bool:
    """True only if the underlying game/match is genuinely still upcoming (not
    started, not final). A cross-platform divergence is only tradeable pre-game
    -- an already-started or finished match sitting at stale/resolved extremes
    (e.g. 0.01 vs 1.00) is the dominant NOISE source here, not real arbitrage.
    MMA is treated as never-pregame-safe (no single kickoff instant on a card,
    same exclusion clv.py makes) so its markets are conservatively dropped."""
    sport, _, raw = entity_id.partition(":")
    model = _ENTITY_MODEL.get(sport)
    if model is None:
        return False
    key = raw if sport in ("nfl", "nba", "wnba", "mlb") else int(raw)
    game = session.get(model, key)
    if game is None or _game_is_final(game):
        return False
    kickoff = _game_kickoff_dt(game)
    if kickoff is None:
        return False  # unknown start -> don't guess it's safe
    return datetime.datetime.utcnow() < kickoff


def _entity_id(m: Market) -> str | None:
    """This app's canonical per-game/match id for a market, or None for a
    futures/season-long market (no single game). Shared across BOTH platforms
    for the same game, which is what makes the cross-platform join reliable."""
    if m.nfl_game_id:
        return f"nfl:{m.nfl_game_id}"
    if m.nba_game_id:
        return f"nba:{m.nba_game_id}"
    if m.wnba_game_id:
        return f"wnba:{m.wnba_game_id}"
    if m.mlb_game_id:
        return f"mlb:{m.mlb_game_id}"
    if m.mma_fight_id:
        return f"mma:{m.mma_fight_id}"
    if m.tennis_match_id:
        return f"tennis:{m.tennis_match_id}"
    if m.soccer_match_id:
        return f"soccer:{m.soccer_match_id}"
    if m.valorant_match_id:
        return f"valorant:{m.valorant_match_id}"
    if m.cs2_match_id:
        return f"cs2:{m.cs2_match_id}"
    if m.lol_match_id:
        return f"lol:{m.lol_match_id}"
    # RACING WAS MISSING FROM THIS LIST ENTIRELY (added 2026-08-06). Every racing
    # market returned None here, so F1, NASCAR and IndyCar were silently excluded
    # from divergence scanning -- even though F1 has carried BOTH platforms all
    # along (33 Kalshi + 134 Polymarket) and NASCAR likewise (255 + 112). The
    # scanner reported 0 racing divergences and that read as "the prices agree"
    # rather than "racing was never looked at".
    #
    # A race is a single scheduled event, the same role a game id plays for the
    # team sports, so it keys the same way. Season-title markets also carry a
    # race_event_id and are legitimately comparable across platforms -- the
    # market_type in _prop_key keeps them from colliding with per-race rows.
    #
    # Third hand-maintained per-sport list this session that a later sport was
    # never added to (see health._LINK_FIELDS, kalshi_racing_client._TAGS).
    #
    # NECESSARY BUT NOT YET SUFFICIENT, and worth being precise about: adding
    # this alone still yields ZERO racing divergences, because the two platforms
    # do not SHARE RaceEvent rows. Each poller creates its own from its own
    # identifier -- Kalshi "KXINDYCARRACE-FREEDOM26", Polymarket
    # "indycar-ontario-honda-dealers-indy-markham-winner-2026-08-16" -- so the
    # same real race exists twice and the join can never fire. Measured
    # 2026-08-06: 0 of the racing race_events carry markets from both sources.
    #
    # Unifying them needs a race-identity step (series + date, the way
    # _match_espn_event already pairs our races to ESPN's), which is a separate
    # piece of work. This branch is still correct and is a prerequisite for it,
    # so it goes in now rather than being reverted and rewritten later.
    if m.race_event_id:
        return f"racing:{m.race_event_id}"
    return None


def _prop_key(m: Market) -> tuple | None:
    """The real-world proposition a market represents, independent of platform.
    Two markets with the same key on different sources ARE the same bet."""
    eid = _entity_id(m)
    if eid is None:
        return None
    return (m.sport, eid, m.market_type, m.team, m.line, m.side)


def find_divergences(session: Session, min_gap: float = 0.03, limit: int = 100) -> list[dict]:
    """Returns cross-platform divergence rows, largest gap first. `min_gap` is
    the minimum |kalshi_prob - poly_prob| (in probability, so 0.03 = 3pp)."""
    from app.api.routers.markets import _batch_latest_snapshots, _implied_prob

    markets = session.query(Market).filter(Market.status != "settled").all()
    snaps = _batch_latest_snapshots(session, [m.id for m in markets])

    groups: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for m in markets:
        key = _prop_key(m)
        if key is None:
            continue
        snap = snaps.get(m.id)
        prob = _implied_prob(snap)
        if prob is None:
            continue
        vol = snap.volume if snap else None
        if not has_real_trading(m.source, vol, snap.last_price if snap else None):
            continue
        # keep the most-traded market per (source) within a prop group
        prev = groups[key].get(m.source)
        if prev is None or (vol or 0) > (prev["volume"] or 0):
            groups[key][m.source] = {
                "market_id": m.id, "prob": prob, "volume": vol,
                "team": m.team, "line": m.line, "side": m.side,
                "market_type": m.market_type, "sport": m.sport, "label": m.group_label,
            }

    out = []
    for key, by_source in groups.items():
        k = by_source.get("kalshi")
        p = by_source.get("polymarket")
        if not k or not p:
            continue
        gap = abs(k["prob"] - p["prob"])
        if gap < min_gap:
            continue
        # Pre-game only: drops the settled/in-play staleness that otherwise
        # dominates (one platform resolved, the other stale at the opposite
        # extreme -- a fake ~100pp "gap", not tradeable arbitrage).
        if not _is_pregame(session, key[1]):
            continue
        cheap = "kalshi" if k["prob"] < p["prob"] else "polymarket"
        out.append({
            "sport": k["sport"],
            "entity_id": key[1],
            "market_type": k["market_type"],
            "team": k["team"],
            "line": k["line"],
            "side": k["side"],
            "kalshi_prob": round(k["prob"], 4),
            "polymarket_prob": round(p["prob"], 4),
            "gap": round(gap, 4),
            "buy_side": cheap,  # the cheaper platform is where you'd buy YES
            "kalshi_market_id": k["market_id"],
            "polymarket_market_id": p["market_id"],
            "kalshi_volume": k["volume"],
            "polymarket_volume": p["volume"],
        })
    out.sort(key=lambda r: r["gap"], reverse=True)
    return out[:limit]

"""Settles placed bets straight from the Kalshi market's OWN resolution -- the
authoritative, 100%-coverage settlement path.

Every Market row is one Kalshi yes/no ticker, and the app only ever bets that
market's priced YES side (see staking.py), so once Kalshi marks the market
`finalized`, its `result` grades the bet directly: yes->won, no->lost, void/''
->void. This needs NO external result scraping and NO team-name matching, so it
covers what the per-sport graders can't -- lower-tier matches missing from the
result feeds, map_winner (no per-map data stored), and even season futures once
they resolve. The per-sport graders + result scrapers stay for POLYMARKET bets
(no Kalshi ticker) and as an immediate fallback.

Network (batch ticker fetch) happens BEFORE the write lock; only the grade+commit
takes it.
"""
import datetime
import logging

from app.clients.base import get_json
from app.db.database import SessionLocal
from app.db.models import Market, PlacedBet
from app.ingestion.poller_lock import db_write_lock
from app.models.bet_position import position_note, resolve_status_for_position

log = logging.getLogger("market_resolution_settlement")

_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
_BATCH = 100


def normalize_result(market: dict) -> str:
    """Kalshi's result for one market, reduced to 'yes'|'no'|'void'|''.

    Exists for result == "scalar", which the settler used to skip -- leaving the
    bet pending FOREVER, because nothing ever revisits a finalized market. Found
    2026-08-06: 32 pending bets across tennis/mma/cs2/valorant were stranded this
    way.

    A scalar result on a market Kalshi itself labels `market_type: "binary"` is
    not a third outcome, it is the refund: the market settles at a value between
    0 and 1 instead of resolving either way. All 32 stranded markets settled
    strictly between (0.03 to 0.70, none at an endpoint), and one cross-confirms
    against the other exchange -- KXWTASETWINNER-26JUL24OLIAVA is the same
    Oliynyk/Avanesyan match Polymarket refunded 0.50/0.50, and Kalshi settled it
    at 0.51.

    The endpoints are still honoured rather than assumed away: a scalar that
    lands on 1.0 or 0.0 IS a clean win or loss, so it maps that way. Only the
    strictly-between case becomes void.
    """
    result = (market.get("result") or "")
    if result != "scalar":
        return result
    try:
        value = float(market.get("settlement_value_dollars"))
    except (TypeError, ValueError):
        return ""  # unreadable -> void, same as Kalshi's own empty result
    if value >= 0.999:
        return "yes"
    if value <= 0.001:
        return "no"
    return "void"


def _fetch_resolutions(tickers: list[str]) -> dict:
    """{ticker: 'yes'|'no'|'void'|''} for the FINALIZED ones among `tickers`."""
    out: dict[str, str] = {}
    for i in range(0, len(tickers), _BATCH):
        chunk = tickers[i : i + _BATCH]
        try:
            d = get_json(f"{_MARKETS_URL}?tickers={','.join(chunk)}&limit={_BATCH}")
        except Exception:
            log.exception("kalshi batch resolution fetch failed for a chunk")
            continue
        for m in d.get("markets", []):
            if m.get("status") in ("finalized", "settled"):
                out[m.get("ticker")] = normalize_result(m)
    return out


def _fetch_statuses(tickers: list[str]) -> dict:
    """{ticker: status} for whatever Kalshi currently says, not just finalized."""
    out: dict[str, str] = {}
    for i in range(0, len(tickers), _BATCH):
        chunk = tickers[i : i + _BATCH]
        try:
            d = get_json(f"{_MARKETS_URL}?tickers={','.join(chunk)}&limit={_BATCH}")
        except Exception:
            log.exception("kalshi batch status fetch failed for a chunk")
            continue
        for m in d.get("markets", []):
            st = m.get("status")
            if st:
                out[m.get("ticker")] = st
    return out


def reconcile_kalshi_market_status() -> int:
    """Refresh Market.status for rows we still believe are active.

    REAL BUG this fixes (user-reported 2026-08-06: a finished CS2 series, "33 vs
    SPARTA", still showing as a live market at 100%). Every per-sport Kalshi
    refresh fetches only OPEN markets, so the moment a market resolves it stops
    being returned and its stored status is frozen at whatever it last was --
    "active", forever. Nothing ever walks back over a resolved market to correct
    it.

    Measured on a random 180-ticker sample of the 10,586 Kalshi markets this app
    called active: 38 of 180 (21%) were already FINALIZED on Kalshi, spread
    across mlb/tennis/cs2/wnba/lol/valorant. That is roughly 2,200 resolved
    markets being served as live ones.

    This matters beyond cosmetics: routers filter on status == "active", so a
    stale-active row is still eligible to be priced and recommended, and its
    last traded price sits at 0 or 1 -- which is exactly the shape that
    manufactures a huge fake edge against any confident model.

    Network first, then a single locked write, per poller_lock.
    """
    session = SessionLocal()
    try:
        rows = (
            session.query(Market.id, Market.source_ticker)
            .filter(Market.source == "kalshi", Market.status == "active", Market.source_ticker.isnot(None))
            .all()
        )
    finally:
        session.close()
    if not rows:
        return 0

    statuses = _fetch_statuses(sorted({t for _mid, t in rows if t}))
    if not statuses:
        return 0

    changed = 0
    with db_write_lock():
        session = SessionLocal()
        try:
            for mid, ticker in rows:
                real = statuses.get(ticker)
                # Only ever write a status Kalshi actually reported, and only
                # when it differs -- a missing ticker (delisted, or dropped from
                # a failed chunk) must not be guessed at.
                if not real or real == "active":
                    continue
                m = session.get(Market, mid)
                if m is None or m.status == real:
                    continue
                m.status = real
                changed += 1
            if changed:
                session.commit()
                log.info("reconciled %d kalshi market statuses away from 'active'", changed)
        finally:
            session.close()
    return changed


def backfill_esports_winners_from_kalshi() -> int:
    """Set Cs2Match/ValorantMatch/LolMatch.winner from the Kalshi series_winner
    market's own resolution.

    WHY THIS EXISTS. CS2 match results come from refresh_cs2_matches, whose
    HLTV/Liquipedia source is Cloudflare-gated and hangs or fails -- so
    Cs2Match.winner stays None (442 of 514 rows), the per-sport esports graders
    can never fire, and worse, the Elo model never learns from a live match at
    all. Kalshi resolution already settles the BETS, but nothing was writing the
    result back onto the match row where the model reads it.

    No new source and no scraping: this reuses resolutions already being
    fetched. Coverage is bounded by what Kalshi listed -- 160 of 442 winnerless
    CS2 matches have a finalized market -- so it does not replace a working
    scraper, it just stops a blocked one from costing us results we already
    have in hand.

    SAFETY. A row is only written when EXACTLY ONE side resolved "yes" and that
    side's name matches EXACTLY ONE of the match's two teams. Ambiguity (both
    yes, neither yes, a name matching both or neither) is skipped rather than
    guessed -- a wrong winner would corrupt Elo, which is far worse than a
    missing one. Name comparison folds accents/case/spacing via each title's own
    normalize_team_name, which was measured to resolve every real mismatch here
    ("Gremio" vs "Gremio", "Mai tai" vs "Mai Tai", a leading space).
    """
    from app.db.models import Cs2Match, LolMatch, ValorantMatch
    from app.ingestion.market_matcher_cs2 import normalize_team_name as norm_cs2
    from app.ingestion.market_matcher_lol import normalize_team_name as norm_lol
    from app.ingestion.market_matcher_valorant import normalize_team_name as norm_val

    titles = (
        ("cs2", Cs2Match, "cs2_match_id", norm_cs2),
        ("valorant", ValorantMatch, "valorant_match_id", norm_val),
        ("lol", LolMatch, "lol_match_id", norm_lol),
    )

    session = SessionLocal()
    try:
        plan = []  # (title, match_id, {ticker: team_name})
        for sport, model, fk, _norm in titles:
            winnerless = {
                m.id: m for m in session.query(model).filter(model.winner.is_(None)).all()
            }
            if not winnerless:
                continue
            rows = (
                session.query(Market)
                .filter(Market.sport == sport, Market.source == "kalshi",
                        Market.market_type == "series_winner",
                        getattr(Market, fk).in_(list(winnerless)))
                .all()
            )
            by_match: dict = {}
            for r in rows:
                if r.source_ticker and r.team:
                    by_match.setdefault(getattr(r, fk), {})[r.source_ticker] = r.team
            for mid, tickers in by_match.items():
                if len(tickers) >= 2:  # need both sides to tell a yes from a no
                    plan.append((sport, model, mid, tickers))
    finally:
        session.close()
    if not plan:
        return 0

    all_tickers = sorted({t for _s, _m, _mid, tk in plan for t in tk})
    resolution = _fetch_resolutions(all_tickers)
    if not resolution:
        return 0

    written = 0
    with db_write_lock():
        session = SessionLocal()
        try:
            for sport, model, mid, tickers in plan:
                yes = [team for tk, team in tickers.items() if resolution.get(tk) == "yes"]
                if len(yes) != 1:
                    continue  # 0 or 2 winners reported -- not a usable result
                norm = dict((s, n) for s, _m, _f, n in titles)[sport]
                match = session.get(model, mid)
                if match is None or match.winner is not None:
                    continue
                win = norm(yes[0])
                a, b = norm(match.team_a or ""), norm(match.team_b or "")
                if win == a and win != b:
                    match.winner = "team_a"
                elif win == b and win != a:
                    match.winner = "team_b"
                else:
                    continue  # matched both sides or neither -- do not guess
                written += 1
            if written:
                session.commit()
                log.info("backfilled %d esports match winners from Kalshi resolution", written)
        finally:
            session.close()
    return written


def settle_from_kalshi_resolution() -> int:
    """Grade every pending bet whose Kalshi market has finalized. Returns count."""
    # 1) read pending Kalshi bets + their tickers (no lock)
    session = SessionLocal()
    try:
        rows = (
            session.query(PlacedBet.id, Market.source_ticker)
            .join(Market, PlacedBet.market_id == Market.id)
            .filter(PlacedBet.status == "pending", Market.source == "kalshi", Market.source_ticker.isnot(None))
            .all()
        )
    finally:
        session.close()
    if not rows:
        return 0
    tickers = sorted({t for _bid, t in rows if t})

    # 2) batch-fetch resolutions (no lock)
    resolution = _fetch_resolutions(tickers)
    if not resolution:
        return 0

    # 3) grade + commit (under lock)
    now = datetime.datetime.utcnow()
    settled = 0
    with db_write_lock():
        session = SessionLocal()
        try:
            for bid, ticker in rows:
                r = resolution.get(ticker)
                if r is None:
                    continue  # market not finalized yet
                status = "won" if r == "yes" else "lost" if r == "no" else "void" if r in ("void", "") else None
                if status is None:
                    continue
                bet = session.get(PlacedBet, bid)
                if bet is None or bet.status != "pending":
                    continue
                bet.status = resolve_status_for_position(bet, status)
                bet.settled_at = now
                bet.settlement_note = (
                    f"auto-settled from Kalshi market resolution (result={r or 'void'})"
                    + position_note(bet)
                )
                settled += 1
            if settled:
                session.commit()
                log.info("settled %d bets from Kalshi market resolution", settled)
        finally:
            session.close()
    return settled

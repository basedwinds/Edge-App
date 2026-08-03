"""Closing Line Value (CLV) for placed bets -- a standard sports-betting
skill metric independent of win/loss: did you get a better price than
where the market ultimately closed, in the direction of your bet? Unlike
win rate, CLV doesn't need a large sample of settled outcomes to be
informative (a single game's randomness doesn't swamp it the way win/loss
does), which makes it a genuinely useful early signal for an app that's
just starting to accumulate real placed-bet history.

Only meaningful for WEEKLY-pool (game-tied) bets, which have a real single
"closing" moment (kickoff). Futures/season-long bets have no such moment
(they resolve at the end of the season, not a specific closing auction), so
those get an honestly-labeled "current price vs placement price" instead
of true CLV -- see status="not_applicable" below.
"""
import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.data.mlb_ballparks import TEAM_TZ
from app.db.models import CfbGame, Cs2Match, LolMatch, MmaFight, MarketSnapshot, MlbGame, NbaGame, NflGame, PlacedBet, RaceEvent, SoccerMatch, TennisMatch, ValorantMatch, WnbaGame


def _implied_prob(snap: MarketSnapshot | None) -> float | None:
    if snap is None:
        return None
    if snap.yes_bid is not None and snap.yes_ask is not None:
        return round((snap.yes_bid + snap.yes_ask) / 2, 4)
    return snap.last_price


def _mlb_kickoff_utc(gameday: str, gametime: str, home_team: str) -> datetime.datetime | None:
    """REAL BUG fixed here (2026-07-17), same root cause and same fix as
    mlb_markets.py::_game_kickoff_local (found while wiring up the live
    weather signal, ported here for CLV's closing-price cutoff specifically):
    `gametime` is a raw UTC clock reading with NO date attached, and naively
    pairing it with `gameday` (the LOCAL calendar date) silently assumes the
    UTC calendar day equals the local one -- FALSE for evening games at
    negative UTC offsets (the real instant is on gameday+1). The OLD version
    of this function (a bare `strptime` with no timezone handling at all)
    would treat that miscalculated instant as if it were already UTC,
    corrupting the "closing snapshot cutoff" and "has kickoff passed yet"
    checks below for exactly the games most likely to need real CLV (evening
    West-Coast/Central games). Resolved by trying both candidate UTC days and
    keeping whichever one's LOCAL conversion (at this team's own real,
    stable timezone -- TEAM_TZ covers all 30, unlike weather's 21) round-
    trips back to the real `gameday`."""
    tz_name = TEAM_TZ.get(home_team)
    if tz_name is None:
        return None
    tz = ZoneInfo(tz_name)
    for day_offset in (0, 1):
        candidate_date = datetime.date.fromisoformat(gameday) + datetime.timedelta(days=day_offset)
        candidate_utc = datetime.datetime.fromisoformat(f"{candidate_date.isoformat()}T{gametime}:00+00:00")
        if candidate_utc.astimezone(tz).date().isoformat() == gameday:
            return candidate_utc.replace(tzinfo=None)  # naive UTC, matching this module's other datetimes
    return None  # neither candidate round-tripped -- genuinely unknown, don't guess


def _tennis_kickoff_utc(match: TennisMatch) -> datetime.datetime | None:
    """Tennis is the SIMPLEST case here, not a special one: unlike NFL/NBA
    (separate gameday+gametime strings to recombine) or MLB (a bare clock
    reading with no date, needing the real day-boundary-aware fix above),
    TennisMatch.estimated_start_time is already a single, complete ISO UTC
    instant (Kalshi's occurrence_datetime / Polymarket's gameStartTime,
    whichever platform's poller saw it first -- see poller_tennis.py). None
    if not yet known (a live match before either platform has posted a real
    estimate) -- genuinely unknown, not guessed."""
    if not match.estimated_start_time:
        return None
    try:
        return datetime.datetime.fromisoformat(match.estimated_start_time.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _soccer_kickoff_utc(match: SoccerMatch) -> datetime.datetime | None:
    """Identical real shape to _tennis_kickoff_utc -- SoccerMatch.
    estimated_start_time is already a single, complete ISO UTC instant
    (Kalshi's occurrence_datetime / Polymarket's gameStartTime, whichever
    platform's poller saw it first, see poller_soccer.py). Kept as its own
    named function rather than reusing _tennis_kickoff_utc directly (same
    "parallel module per sport" convention as the rest of this app, even
    where two sports' real data shapes happen to coincide)."""
    if not match.estimated_start_time:
        return None
    try:
        return datetime.datetime.fromisoformat(match.estimated_start_time.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _valorant_kickoff_utc(match: ValorantMatch) -> datetime.datetime | None:
    """Identical real shape to _tennis_kickoff_utc/_soccer_kickoff_utc --
    ValorantMatch.estimated_start_time is already a single, complete ISO
    UTC instant (vlr.gg's own timer widget, or whichever platform's poller
    saw it first -- see poller_valorant.py). Kept as its own named function
    per this app's "parallel module per sport" convention."""
    if not match.estimated_start_time:
        return None
    try:
        return datetime.datetime.fromisoformat(match.estimated_start_time.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _cs2_kickoff_utc(match: Cs2Match) -> datetime.datetime | None:
    """See _valorant_kickoff_utc's own docstring -- CS2's own version,
    Cs2Match.estimated_start_time is Liquipedia's own real timer widget."""
    if not match.estimated_start_time:
        return None
    try:
        return datetime.datetime.fromisoformat(match.estimated_start_time.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _lol_kickoff_utc(match: LolMatch) -> datetime.datetime | None:
    """See _valorant_kickoff_utc's own docstring -- LoL's own version,
    LolMatch.estimated_start_time is Leaguepedia's real Cargo DateTime_UTC
    field (not an estimate, more trustworthy than vlr.gg's own timer)."""
    if not match.estimated_start_time:
        return None
    try:
        return datetime.datetime.fromisoformat(match.estimated_start_time.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _mma_kickoff_utc(fight: MmaFight) -> datetime.datetime | None:
    """MmaFight.estimated_start_time is a full ISO UTC instant -- Kalshi's own
    per-fight occurrence_datetime, STAGGERED across the card rather than one
    flat event-level time (see the column's own docstring).

    That per-fight staggering is what makes MMA CLV possible, and it is why the
    old blanket exclusion here ("no single kickoff-equivalent moment on a UFC
    card") is no longer true -- it was written before this field existed. All
    164 MMA placed bets were returning status "not_applicable", so MMA could
    never accrue the forward CLV this app treats as its only real evidence.

    It is genuinely an ESTIMATE (fight order can reshuffle) and is refreshed
    every poll, so the closing instant is approximate -- the same honest
    compromise already accepted for RaceEvent.start_time. Fights without one
    (75 of 109 today) return None and fall back to the degraded path exactly as
    before, rather than being given a fabricated cutoff."""
    if not fight.estimated_start_time:
        return None
    try:
        return datetime.datetime.fromisoformat(
            fight.estimated_start_time.replace("Z", "+00:00")
        ).replace(tzinfo=None)
    except ValueError:
        return None


def _game_kickoff_dt(game) -> datetime.datetime | None:
    """None if the kickoff/tip-off time isn't reliably known yet -- "00:00"
    is nflverse/ESPN's placeholder for "not yet announced" (see
    RecommendedBetsTable.tsx's formatGameDate for the same convention on
    the frontend), not a real midnight kickoff. NFL/NBA's gametime is
    already a real wall-clock-consistent value (NFL: stadium local; NBA:
    both gameday and gametime derive from the same UTC instant, confirmed
    self-consistent) -- MLB needs the real day-boundary-aware conversion in
    _mlb_kickoff_utc above instead; Tennis/Soccer each have their own
    single-instant field, see _tennis_kickoff_utc/_soccer_kickoff_utc."""
    if isinstance(game, TennisMatch):
        return _tennis_kickoff_utc(game)
    if isinstance(game, SoccerMatch):
        return _soccer_kickoff_utc(game)
    if isinstance(game, ValorantMatch):
        return _valorant_kickoff_utc(game)
    if isinstance(game, Cs2Match):
        return _cs2_kickoff_utc(game)
    if isinstance(game, LolMatch):
        return _lol_kickoff_utc(game)
    if isinstance(game, MmaFight):
        return _mma_kickoff_utc(game)
    if isinstance(game, RaceEvent):
        return game.start_time  # UTC race-start proxy (see _get_game note)
    if not game.gameday or not game.gametime or game.gametime == "00:00":
        return None
    if isinstance(game, MlbGame):
        return _mlb_kickoff_utc(game.gameday, game.gametime, game.home_team)
    try:
        return datetime.datetime.strptime(f"{game.gameday} {game.gametime}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _game_is_final(game) -> bool:
    """TennisMatch has no home_score -- a real result is signaled by
    winner_key being set instead (see TennisMatch's own docstring: null
    means "not yet played, or a real walkover/retirement excluded from
    scoring" -- either way, not yet a final result to compute true CLV
    against, same as a null home_score for the other sports). SoccerMatch
    is the same shape, with result_ft as its own null-until-played field."""
    if isinstance(game, TennisMatch):
        return game.winner_key is not None
    if isinstance(game, SoccerMatch):
        return game.result_ft is not None
    if isinstance(game, (ValorantMatch, Cs2Match, LolMatch)):
        return game.winner is not None
    # MmaFight has no home_score, so it must be handled before the fallback
    # below -- winner_id is its null-until-fought result field.
    if isinstance(game, MmaFight):
        return game.winner_id is not None
    if isinstance(game, RaceEvent):
        # No finishing results tracked; the race is "final" (closing line exists)
        # once its start time has passed.
        return game.start_time is not None and game.start_time < datetime.datetime.utcnow()
    return game.home_score is not None


def _get_game(session: Session, bet: PlacedBet):
    """REAL BUG this fixes: the original version was hardcoded to
    bet.nfl_game_id/NflGame -- an NBA weekly-pool bet (nfl_game_id always
    None, nba_game_id set instead) would silently fall into the
    "not_applicable" (futures-style) branch below instead of getting real
    CLV, even though it has a genuine kickoff/tip-off moment just like an
    NFL game does. Dispatches on bet.sport instead of assuming NFL. Same
    gap would have hit MLB placed bets (mlb_game_id set, nfl/nba_game_id
    both None) -- added proactively rather than waiting to catch it live,
    since the exact same class of bug was already found once here. Tennis
    added the same way (2026-07-19) -- a real closing moment exists there
    too (estimated_start_time), unlike MMA which is deliberately excluded
    below (no single kickoff-equivalent moment on a UFC card). Soccer added
    the same way again (2026-07-19), same reasoning as Tennis. Valorant/
    CS2/LoL added the same way too (2026-07-20) -- each has its own real
    single estimated_start_time instant (unlike MMA's whole-card ambiguity),
    so true CLV is genuinely computable for esports match-tied bets."""
    if bet.sport == "nba":
        return session.get(NbaGame, bet.nba_game_id) if bet.nba_game_id else None
    if bet.sport == "wnba":
        return session.get(WnbaGame, bet.wnba_game_id) if bet.wnba_game_id else None
    if bet.sport == "mma":
        return session.get(MmaFight, bet.mma_fight_id) if bet.mma_fight_id else None
    if bet.sport == "cfb":
        # Without this CFB fell through to the NFL lookup at the bottom, where
        # nfl_game_id is always None for a CFB bet -- so every CFB bet silently
        # got no CLV at all. CfbGame has a real kickoff (gameday + gametime),
        # so true CLV is computable exactly as it is for NFL/WNBA.
        return session.get(CfbGame, bet.cfb_game_id) if bet.cfb_game_id else None
    if bet.sport == "mlb":
        return session.get(MlbGame, bet.mlb_game_id) if bet.mlb_game_id else None
    if bet.sport == "tennis":
        return session.get(TennisMatch, bet.tennis_match_id) if bet.tennis_match_id else None
    if bet.sport == "soccer":
        return session.get(SoccerMatch, bet.soccer_match_id) if bet.soccer_match_id else None
    if bet.sport == "valorant":
        return session.get(ValorantMatch, bet.valorant_match_id) if bet.valorant_match_id else None
    if bet.sport == "cs2":
        return session.get(Cs2Match, bet.cs2_match_id) if bet.cs2_match_id else None
    if bet.sport == "lol":
        return session.get(LolMatch, bet.lol_match_id) if bet.lol_match_id else None
    if bet.sport in ("f1", "irl", "nascar"):
        # Motorsport: the RaceEvent's start_time is the closing-line cutoff
        # (2026-07-23). NOTE: currently a race-day proxy (Kalshi close_time);
        # swap to the real ESPN race start for exact CLV once wired.
        return session.get(RaceEvent, bet.race_event_id) if bet.race_event_id else None
    return session.get(NflGame, bet.nfl_game_id) if bet.nfl_game_id else None


def compute_bet_clv(session: Session, bet: PlacedBet) -> dict:
    """Returns {closing_prob, clv_pp, status}.

    status:
      "closed"         -- real closing price found, clv_pp is meaningful CLV
      "pending"         -- game hasn't kicked off yet, too early to know the close
      "unavailable"     -- missing data (no game record, no snapshot history)
      "not_applicable"  -- futures/season-long bet; clv_pp here is actually
                            CURRENT-price-vs-placement, not true CLV
    """
    if bet.market_prob_at_placement is None:
        return {"closing_prob": None, "clv_pp": None, "status": "unavailable"}

    # MMA deliberately excluded from "has a real game id" here even though
    # PlacedBet.mma_fight_id exists -- a UFC card has no single kickoff-
    # equivalent moment (early prelims to main event can span ~8 hours, and
    # Kalshi keeps these markets open THROUGH the live fight itself,
    # confirmed live -- "closes after a champion is declared"), so there's
    # no reliable snapshot-cutoff time to compute a TRUE closing line
    # against. Routes into the same "current price vs. placement" honest
    # degrade futures already use below, rather than risk a subtly wrong
    # "closed" status by guessing at a fight time this app doesn't track.
    has_game_id = (
        bet.nba_game_id if bet.sport == "nba"
        else bet.wnba_game_id if bet.sport == "wnba"
        else bet.cfb_game_id if bet.sport == "cfb"
        else bet.mlb_game_id if bet.sport == "mlb"
        else bet.tennis_match_id if bet.sport == "tennis"
        else bet.soccer_match_id if bet.sport == "soccer"
        else bet.valorant_match_id if bet.sport == "valorant"
        else bet.cs2_match_id if bet.sport == "cs2"
        else bet.lol_match_id if bet.sport == "lol"
        else bet.race_event_id if bet.sport in ("f1", "irl", "nascar")
        else bet.mma_fight_id if bet.sport == "mma"
        else bet.nfl_game_id
    )
    if bet.stake_pool != "weekly" or not has_game_id:
        snap = (
            session.query(MarketSnapshot)
            .filter(MarketSnapshot.market_id == bet.market_id)
            .order_by(MarketSnapshot.ts.desc())
            .first()
        )
        current = _implied_prob(snap)
        clv_pp = round(current - bet.market_prob_at_placement, 4) if current is not None else None
        return {"closing_prob": current, "clv_pp": clv_pp, "status": "not_applicable"}

    game = _get_game(session, bet)
    if game is None:
        return {"closing_prob": None, "clv_pp": None, "status": "unavailable"}

    kickoff = _game_kickoff_dt(game)
    game_is_final = _game_is_final(game)
    now = datetime.datetime.utcnow()

    if not game_is_final and kickoff is not None and now < kickoff:
        return {"closing_prob": None, "clv_pp": None, "status": "pending"}

    # Without a kickoff we CAN'T establish a real closing line -- taking the
    # latest snapshot would grab an in-play/post-match price (a winning side
    # trading near 100%), which fabricates huge CLV. Better to report no CLV
    # than a contaminated one.
    if kickoff is None:
        return {"closing_prob": None, "clv_pp": None, "status": "unavailable"}
    query = (
        session.query(MarketSnapshot)
        .filter(MarketSnapshot.market_id == bet.market_id, MarketSnapshot.ts <= kickoff)
    )
    closing_snap = query.order_by(MarketSnapshot.ts.desc()).first()
    closing_prob = _implied_prob(closing_snap)
    if closing_prob is None:
        return {"closing_prob": None, "clv_pp": None, "status": "unavailable"}

    return {
        "closing_prob": closing_prob,
        "clv_pp": round(closing_prob - bet.market_prob_at_placement, 4),
        "status": "closed",
    }

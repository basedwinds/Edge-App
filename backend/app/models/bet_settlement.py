"""Auto-settles PENDING placed bets tied to a real game once that game's
final score lands (already ingested via each sport's own fetch_games() into
{Nfl,Nba,Mlb}Game.home_score/away_score -- no new data source needed).

Only game-tied FULL-GAME market types can be graded this way: moneyline,
spread, total, team_total. Half-line markets (spread_1h/2h, total_1h/2h) are
NOT auto-settled -- this app never ingests half-time scores, only final
scores, so there's no real data to grade them against. MLB's F5 (3-way,
including a tie) and RFI (1st-inning-specific) are ALSO not auto-settled for
the same reason -- this app's live DB only stores each game's FINAL score,
not per-inning linescores, so there's no real data on hand to grade either
one against (the linescore cache built for the F5/RFI model derivation was a
one-off historical pull, not a live-ingested table). These (and every
futures/season-long market type, which has no single resolvable game at
all) stay "pending" until the user manually settles them via
POST /placed-bets/{id}/settle.

REAL BUG fixed here (2026-07-17, Phase 10 polish pass): this module was
hardcoded to NflGame/bet.nfl_game_id throughout, so NBA and MLB weekly-pool
bets NEVER auto-settled regardless of market type -- same class of gap
already found and fixed once in clv.py's `_get_game` (2026-07-17, earlier
this session) for CLV specifically. The graders below only ever read
`game.home_score`/`away_score`/`home_team`/`away_team`, which are the exact
same field names on NflGame/NbaGame/MlbGame (confirmed live) -- so they were
already sport-agnostic in practice, just never given a non-NFL game to grade.
"""
import logging

from sqlalchemy.orm import Session

from app.db.models import MlbGame, NbaGame, NflGame, PlacedBet, SoccerMatch, WnbaGame

log = logging.getLogger("bet_settlement")

# Soccer's own market_type strings (moneyline_3way/game_spread/game_total/
# btts) are distinct from NFL/NBA/MLB's (moneyline/spread/total/team_total)
# -- no collision, safe to share one set/dict across sports, same
# "market_type string alone is enough to dispatch the right grader" design
# this file already used before Soccer existed.
AUTO_SETTLE_MARKET_TYPES = {"moneyline", "spread", "total", "team_total", "moneyline_3way", "game_spread", "game_total", "btts"}


def _get_game(session: Session, bet: PlacedBet):
    """Same dispatch-on-bet.sport pattern as clv.py::_get_game."""
    if bet.sport == "nba":
        return session.get(NbaGame, bet.nba_game_id) if bet.nba_game_id else None
    if bet.sport == "wnba":
        return session.get(WnbaGame, bet.wnba_game_id) if bet.wnba_game_id else None
    if bet.sport == "mlb":
        return session.get(MlbGame, bet.mlb_game_id) if bet.mlb_game_id else None
    if bet.sport == "soccer":
        return session.get(SoccerMatch, bet.soccer_match_id) if bet.soccer_match_id else None
    return session.get(NflGame, bet.nfl_game_id) if bet.nfl_game_id else None


def _game_is_final(bet: PlacedBet, game) -> bool:
    """SoccerMatch has no home_score/away_score -- result_ft is its own
    null-until-played field (same real distinction clv.py::_game_is_final
    already draws for TennisMatch)."""
    if bet.sport == "soccer":
        return game.result_ft is not None
    return game.home_score is not None and game.away_score is not None


def _grade_moneyline(bet: PlacedBet, game) -> str:
    team_score = game.home_score if bet.team == game.home_team else game.away_score
    opp_score = game.away_score if bet.team == game.home_team else game.home_score
    if team_score == opp_score:
        return "push"
    return "won" if team_score > opp_score else "lost"


def _grade_spread(bet: PlacedBet, game) -> str:
    # "yes" side is "bet.team wins by more than bet.line" -- same convention
    # as game_lines.py's/game_lines_nba.py's/game_lines_mlb.py's margin
    # models (favorite: positive line; underdog: negated line already baked
    # into how `line` was stored at ingestion) -- confirmed the same
    # convention holds for all 3 sports before reusing this grader as-is.
    margin = (game.home_score - game.away_score) if bet.team == game.home_team else (game.away_score - game.home_score)
    if margin == bet.line:
        return "push"
    return "won" if margin > bet.line else "lost"


def _grade_total(bet: PlacedBet, game) -> str:
    actual_total = game.home_score + game.away_score
    if actual_total == bet.line:
        return "push"
    if bet.side == "under":
        return "won" if actual_total < bet.line else "lost"
    return "won" if actual_total > bet.line else "lost"  # side == "over" (Kalshi total ladders are always "over")


def _grade_team_total(bet: PlacedBet, game) -> str:
    team_score = game.home_score if bet.team == game.home_team else game.away_score
    if team_score == bet.line:
        return "push"
    return "won" if team_score > bet.line else "lost"


def _grade_soccer_moneyline_3way(bet: PlacedBet, game: SoccerMatch) -> str:
    """No push -- moneyline_3way is a discrete home/draw/away proposition,
    not a line, same "no draw-the-line-exactly case" shape as every other
    3-outcome market in this app."""
    side_to_result = {"home": "H", "draw": "D", "away": "A"}
    return "won" if side_to_result.get(bet.side) == game.result_ft else "lost"


def _grade_soccer_spread(bet: PlacedBet, game: SoccerMatch) -> str:
    """Same "wins by more than bet.line goals" convention as
    _grade_spread -- reused directly, not re-derived, see that function's
    own comment on why the sign convention already holds without
    adjustment."""
    margin = (
        (game.home_goals_ft - game.away_goals_ft) if bet.team == game.home_team
        else (game.away_goals_ft - game.home_goals_ft)
    )
    if margin == bet.line:
        return "push"
    return "won" if margin > bet.line else "lost"


def _grade_soccer_total(bet: PlacedBet, game: SoccerMatch) -> str:
    actual_total = game.home_goals_ft + game.away_goals_ft
    if actual_total == bet.line:
        return "push"
    if bet.side == "under":
        return "won" if actual_total < bet.line else "lost"
    return "won" if actual_total > bet.line else "lost"  # side == "over" -- Soccer's own total ladder is always framed as Over, see market_catalog_soccer.py


def _grade_soccer_btts(bet: PlacedBet, game: SoccerMatch) -> str:
    """No push, no "no" side to grade -- market.side is always "yes" for
    this market type (see market_catalog_soccer.py::upsert_kalshi_soccer_
    btts_market and staking.py::kelly_fraction's own docstring on why this
    app never recommends/places a bet on the losing side of a binary
    market's own priced YES row)."""
    return "won" if (game.home_goals_ft >= 1 and game.away_goals_ft >= 1) else "lost"


_GRADERS = {
    "moneyline": _grade_moneyline,
    "spread": _grade_spread,
    "total": _grade_total,
    "team_total": _grade_team_total,
    "moneyline_3way": _grade_soccer_moneyline_3way,
    "game_spread": _grade_soccer_spread,
    "game_total": _grade_soccer_total,
    "btts": _grade_soccer_btts,
}


def _settlement_note(bet: PlacedBet, game) -> str:
    if bet.sport == "soccer":
        return f"auto-settled: final score {game.away_team} {game.away_goals_ft} @ {game.home_team} {game.home_goals_ft}"
    return f"auto-settled: final score {game.away_team} {game.away_score} @ {game.home_team} {game.home_score}"


def settle_finished_games(session: Session) -> int:
    """Grades every pending, auto-gradeable placed bet whose game now has a
    final score. Returns the number settled."""
    pending = (
        session.query(PlacedBet)
        .filter(PlacedBet.status == "pending", PlacedBet.market_type.in_(AUTO_SETTLE_MARKET_TYPES))
        .all()
    )
    settled = 0
    import datetime

    for bet in pending:
        game = _get_game(session, bet)
        if game is None or not _game_is_final(bet, game):
            continue  # not final yet
        grader = _GRADERS.get(bet.market_type)
        if grader is None:
            continue
        result = grader(bet, game)
        bet.status = result
        bet.settled_at = datetime.datetime.utcnow()
        bet.settlement_note = _settlement_note(bet, game)
        settled += 1

    if settled:
        session.commit()
        log.info("auto-settled %d placed bets from final scores", settled)
    return settled

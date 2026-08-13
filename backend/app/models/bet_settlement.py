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

from sqlalchemy.orm import Session, object_session

from app.db.models import (
    CfbGame, CodMatch, Cs2Match, LolMap, LolMatch, Market, MlbGame, MmaFight, NbaGame, NflGame, PlacedBet,
    RaceEvent, SoccerMatch, TennisMatch, ValorantMap, ValorantMatch, WnbaGame,
)

log = logging.getLogger("bet_settlement")

# Market-type strings collide ACROSS sports now (mma/tennis both have their own
# "moneyline", esports its own "series_winner", etc.), so this set is only the
# coarse "is this type ever auto-settleable" filter -- the ACTUAL grader is then
# chosen by (sport, market_type) in settle_finished_games via _pick_grader,
# because e.g. an mma "moneyline" needs a completely different grader than an
# NFL "moneyline". The score-based game sports (nfl/nba/wnba/mlb) + soccer still
# share the flat _GRADERS dict below; mma/tennis/esports/racing get their own.
AUTO_SETTLE_MARKET_TYPES = {
    # score-based game sports (nfl/nba/wnba/mlb) + soccer
    "moneyline", "spread", "total", "team_total", "moneyline_3way", "game_spread", "game_total", "btts",
    # NFL half winner -- needs NflGame.home_score_1h, filled by
    # espn_client.fetch_half_scores (nflverse publishes only the final).
    "winner_1h", "winner_2h",
    # soccer halves -- graded off ESPN half-time linescores (see
    # espn_soccer_client.fetch_half_time_goals); second half is FT minus HT.
    "first_half_winner", "second_half_winner", "first_half_total", "second_half_total",
    "first_half_team_total", "second_half_team_total", "second_half_btts",
    # ftts needs SoccerMatch.first_scorer (ESPN scoring plays); its grader
    # returns None when that is unknown, so those bets stay pending.
    "ftts",
    # cup + UEFA regulation markets (2026-08-08). cup_advance is deliberately
    # NOT here -- see _SOCCER_GRADERS for why it must stay unsettleable.
    "cup_moneyline_3way", "uefa_moneyline_3way", "cup_total", "uefa_total",
    # Leagues Cup (2026-08-08). All four are ordinary single-match questions
    # settled off the regulation score, so they reuse the SAME graders the
    # league markets use -- there is no new settlement rule here, only new
    # market_type names pointing at existing logic.
    "leagues_cup_moneyline_3way", "leagues_cup_total",
    "leagues_cup_spread", "leagues_cup_btts",
    # National teams (2026-08-09). Ordinary single-match questions settled off
    # the regulation score -- same graders as the league markets, new names only.
    "national_moneyline_3way", "national_total",
    "national_spread", "national_btts",
    # mma
    "distance", "rounds", "method_of_finish",
    # Joint "<fighter> wins by KO" -- its grader requires the backed fighter to
    # have WON as well as the method to match, which _grade_mma_method does not.
    "method_of_victory",
    # esports (cs2/valorant/lol). map_winner grades for LoL + Valorant -- see
    # _grade_esports_map_winner; CS2 has no reachable per-map source, and its
    # bets return None (stay pending) rather than being guessed.
    "series_winner", "series_total", "map_winner",
    # tennis (moneyline shared above); set/game markets.
    #
    # "total_sets" was MISSING here while having a working grader, so those bets
    # could never reach it -- the filter runs before _pick_grader, so a type
    # absent from this set is skipped no matter what grader exists. Found
    # 2026-08-06: 4 bets on completed matches sat pending for exactly that
    # reason. Note the near-namesake "set_total" is the opposite case -- it is
    # listed here but deliberately has NO grader (ambiguous side semantics), so
    # it stays pending on purpose. The two are easy to confuse; they are not the
    # same market.
    "set_winner", "set_total", "total_sets", "exact_score", "set_spread",
    # racing
    "race_winner", "top_n", "pole", "h2h",
}


def _get_game(session: Session, bet: PlacedBet):
    """Dispatch-on-bet.sport, mirroring clv.py::_get_game (kept in sync so a bet
    that can get CLV can also be win/loss graded). Returns the sport's own
    match/event row, or None if unlinked."""
    if bet.sport == "nba":
        return session.get(NbaGame, bet.nba_game_id) if bet.nba_game_id else None
    if bet.sport == "wnba":
        return session.get(WnbaGame, bet.wnba_game_id) if bet.wnba_game_id else None
    if bet.sport == "cfb":
        # CFB previously fell through to the NFL lookup below, where a CFB bet's
        # nfl_game_id is always None -- so CFB bets could never be graded and sat
        # pending forever. CfbGame carries home/away team + score exactly like
        # NflGame, so the shared score-based graders apply unchanged and
        # _pick_grader's default _GRADERS branch already routes CFB correctly.
        # CFB's only game market is moneyline (cfb_markets.GAME_MARKET_TYPES);
        # Kalshi has not listed CFB spread/total, so nothing else needs a grader.
        return session.get(CfbGame, bet.cfb_game_id) if bet.cfb_game_id else None
    if bet.sport == "mlb":
        return session.get(MlbGame, bet.mlb_game_id) if bet.mlb_game_id else None
    if bet.sport == "soccer":
        return session.get(SoccerMatch, bet.soccer_match_id) if bet.soccer_match_id else None
    if bet.sport == "tennis":
        return session.get(TennisMatch, bet.tennis_match_id) if bet.tennis_match_id else None
    if bet.sport == "mma":
        return session.get(MmaFight, bet.mma_fight_id) if bet.mma_fight_id else None
    if bet.sport == "valorant":
        return session.get(ValorantMatch, bet.valorant_match_id) if bet.valorant_match_id else None
    if bet.sport == "cs2":
        return session.get(Cs2Match, bet.cs2_match_id) if bet.cs2_match_id else None
    if bet.sport == "lol":
        return session.get(LolMatch, bet.lol_match_id) if bet.lol_match_id else None
    if bet.sport == "cod":
        return session.get(CodMatch, bet.cod_match_id) if bet.cod_match_id else None
    if bet.sport in ("f1", "irl", "nascar"):
        return session.get(RaceEvent, bet.race_event_id) if bet.race_event_id else None
    return session.get(NflGame, bet.nfl_game_id) if bet.nfl_game_id else None


def _game_is_final(bet: PlacedBet, game) -> bool:
    """True once the event has a real result to grade against. Each sport stores
    its result in its own null-until-played field."""
    if bet.sport == "soccer":
        return game.result_ft is not None
    if bet.sport == "tennis":
        return game.winner_key is not None
    if bet.sport == "mma":
        return game.winner_id is not None
    if bet.sport in ("cs2", "valorant", "lol", "cod"):
        # CoD joins this branch rather than getting its own: CodMatch stores the
        # same "team_a"/"team_b" winner as the other three, so _esports_side and
        # _grade_esports_series_winner already handle it unchanged. Reusing the
        # shared shape is deliberate -- a fourth near-identical grader is how one
        # of them ends up not getting a fix the others got.
        #
        # NOT gated on is_live. A live match has winner=None and so is not final
        # here anyway; a match that has finished but whose is_live flag has not
        # yet been cleared by the next poll must still settle.
        return game.winner is not None
    if bet.sport in ("f1", "irl", "nascar"):
        # RaceEvent.result_json is populated by the racing results scraper once
        # the race is done (see poller_racing / espn_racing_results).
        return getattr(game, "result_json", None) is not None
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


def _grade_half_winner(bet: PlacedBet, game, half: int) -> "str | None":
    """NFL 1H/2H winner (KXNFL1H / KXNFL2H). Returns None -- bet stays pending
    -- when the half score is missing, since nflverse gives only the final and
    espn_client.fetch_half_scores may not have run for this game yet.

    A HALF CAN END LEVEL (measured 6.3% of 1st halves, 9.8% of 2nd), and Kalshi
    lists TIE as its own leg. A tied half is a real LOSS for a team leg, not a
    push -- "will Carolina win the 1st half" is simply false when nobody wins
    it. The TIE leg itself grades as won on exactly that outcome.
    """
    # getattr, not attribute access: only NflGame has these columns, and this
    # grader is reached from the shared _GRADERS table. A plain game.home_score_1h
    # would raise AttributeError on an NBA/MLB row and abort settle_finished_games
    # for EVERY sport -- exactly the soccer team_total crash, one market type over.
    home_1h = getattr(game, "home_score_1h", None)
    away_1h = getattr(game, "away_score_1h", None)
    if home_1h is None or away_1h is None:
        return None
    if half == 1:
        h, a = home_1h, away_1h
    else:
        if game.home_score is None or game.away_score is None:
            return None
        h = game.home_score - home_1h
        a = game.away_score - away_1h
    if (bet.team or "").upper() in ("TIE", "DRAW"):
        return "won" if h == a else "lost"
    if bet.team == game.home_team:
        return "won" if h > a else "lost"
    if bet.team == game.away_team:
        return "won" if a > h else "lost"
    return None  # unrecognised leg -- never guess a side


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


def _grade_soccer_team_total(bet: PlacedBet, game: SoccerMatch) -> str:
    """Soccer's own team_total, reading goals rather than home_score/away_score.

    REAL BUG this fixes (2026-08-06): "team_total" is one of the few market
    types soccer SHARES by name with the score sports, so it resolved to
    _grade_team_total, which reads game.home_score -- a field SoccerMatch does
    not have. That raised AttributeError inside settle_finished_games, which
    grades EVERY sport in one loop, so a single soccer team_total bet aborted
    settlement for all sports. It stayed hidden only because soccer results
    were never being written, so the grader never actually ran.
    """
    team_goals = game.home_goals_ft if bet.team == game.home_team else game.away_goals_ft
    if team_goals == bet.line:
        return "push"
    return "won" if team_goals > bet.line else "lost"


# ---- soccer halves --------------------------------------------------------
# All of these return None (bet stays pending) when the half-time score is
# missing, rather than guessing. home_goals_ht is populated by
# espn_soccer_client.fetch_half_time_goals, which is a per-event request and
# can legitimately come back empty for an older or lower-coverage match.
#
# SECOND-half goals are DERIVED (full time minus half time), not fetched --
# ESPN's linescores validated as summing to the final on 16 of 16 sampled
# matches, so the subtraction is exact rather than an approximation.
def _soccer_half_goals(game: SoccerMatch, half: int) -> "tuple[int, int] | None":
    if game.home_goals_ht is None or game.away_goals_ht is None:
        return None
    if half == 1:
        return game.home_goals_ht, game.away_goals_ht
    if game.home_goals_ft is None or game.away_goals_ft is None:
        return None
    return game.home_goals_ft - game.home_goals_ht, game.away_goals_ft - game.away_goals_ht


def _grade_soccer_half_winner(bet: PlacedBet, game: SoccerMatch, half: int) -> "str | None":
    """A half winner is a 3-way home/draw/away proposition, same shape as
    moneyline_3way -- a drawn half is a real outcome, not a push."""
    g = _soccer_half_goals(game, half)
    if g is None:
        return None
    h, a = g
    actual = "H" if h > a else ("A" if a > h else "D")
    return "won" if {"home": "H", "draw": "D", "away": "A"}.get(bet.side) == actual else "lost"


def _grade_soccer_half_total(bet: PlacedBet, game: SoccerMatch, half: int) -> "str | None":
    g = _soccer_half_goals(game, half)
    if g is None:
        return None
    total = sum(g)
    if total == bet.line:
        return "push"
    if bet.side == "under":
        return "won" if total < bet.line else "lost"
    return "won" if total > bet.line else "lost"


def _grade_soccer_half_team_total(bet: PlacedBet, game: SoccerMatch, half: int) -> "str | None":
    g = _soccer_half_goals(game, half)
    if g is None:
        return None
    team_goals = g[0] if bet.team == game.home_team else g[1]
    if team_goals == bet.line:
        return "push"
    return "won" if team_goals > bet.line else "lost"


def _grade_soccer_second_half_btts(bet: PlacedBet, game: SoccerMatch) -> "str | None":
    g = _soccer_half_goals(game, 2)
    if g is None:
        return None
    return "won" if (g[0] >= 1 and g[1] >= 1) else "lost"


def _grade_soccer_btts(bet: PlacedBet, game: SoccerMatch) -> str:
    """No push, no "no" side to grade -- market.side is always "yes" for
    this market type (see market_catalog_soccer.py::upsert_kalshi_soccer_
    btts_market and staking.py::kelly_fraction's own docstring on why this
    app never recommends/places a bet on the losing side of a binary
    market's own priced YES row)."""
    return "won" if (game.home_goals_ft >= 1 and game.away_goals_ft >= 1) else "lost"


def _names_eq(a: "str | None", b: "str | None") -> bool:
    """Case/space-insensitive name match -- bet team/player names come from
    Kalshi/Polymarket and the result names from the scrapers; they routinely
    differ only in casing/spacing."""
    return bool(a) and bool(b) and a.strip().lower() == b.strip().lower()


# ---- esports (cs2 / valorant / lol) -- match.winner is "team_a"/"team_b" -----
def _esports_side(bet: PlacedBet, match) -> "str | None":
    # Case/space-insensitive: the bet team comes from Kalshi/Polymarket
    # ("magic", "The Mongolz") while the match roster comes from the results
    # scraper ("Magic", "The MongolZ") -- an exact compare silently left real,
    # already-decided bets pending.
    t = (bet.team or "").strip().lower()
    if t and t == (match.team_a or "").strip().lower():
        return "team_a"
    if t and t == (match.team_b or "").strip().lower():
        return "team_b"
    return None  # bet team doesn't match either roster -> can't grade, leave pending


def _grade_esports_series_winner(bet: PlacedBet, match) -> "str | None":
    side = _esports_side(bet, match)
    if side is None:
        return None
    return "won" if match.winner == side else "lost"


def _grade_esports_series_total(bet: PlacedBet, match) -> "str | None":
    if bet.line is None or match.maps_won_a is None or match.maps_won_b is None:
        return None
    total = match.maps_won_a + match.maps_won_b
    if total == bet.line:
        return "push"
    if bet.side == "under":
        return "won" if total < bet.line else "lost"
    return "won" if total > bet.line else "lost"  # side "over"


# ---- mma (MmaFight: winner_id, method, round, went_the_distance) -------------
def _mma_winner_name(fight: MmaFight) -> "str | None":
    if fight.winner_id == fight.fighter_a_id:
        return fight.fighter_a_name
    if fight.winner_id == fight.fighter_b_id:
        return fight.fighter_b_name
    return None  # draw / no-contest -> not gradeable as a straight winner


def _grade_mma_moneyline(bet: PlacedBet, fight: MmaFight) -> "str | None":
    """Graded by resolving the bet's team to a SIDE of the fight and comparing
    that to winner_id, rather than string-comparing the bet's team against the
    winner's name.

    The string compare was actively dangerous, not merely lossy: on a name it
    couldn't match it fell through to "lost". So a bet on the Kalshi spelling
    "Yadier Delvalle" would be settled as a LOSS even when he won, because
    ufcstats calls him "Yadier del Valle". Resolving the side instead makes an
    unrecognised name return None (stays pending, visible as unsettled) instead
    of silently booking a false loss.
    """
    from app.ingestion.market_matcher_mma import resolve_fight_side

    if fight.winner_id is None:
        return None  # draw / no-contest -> not gradeable as a straight winner
    side = resolve_fight_side(bet.team, fight.fighter_a_name, fight.fighter_b_name)
    if side is None:
        return None  # can't tell which fighter was backed -> never assume a loss
    backed_id = fight.fighter_a_id if side == "a" else fight.fighter_b_id
    if fight.winner_id not in (fight.fighter_a_id, fight.fighter_b_id):
        return None
    return "won" if fight.winner_id == backed_id else "lost"


def _grade_mma_distance(bet: PlacedBet, fight: MmaFight) -> "str | None":
    """side "yes" = fight goes the distance (reaches the scorecards)."""
    if fight.went_the_distance is None:
        return None
    went = bool(fight.went_the_distance)
    if bet.side == "no":
        return "won" if not went else "lost"
    return "won" if went else "lost"  # side "yes" (the priced YES row)


def _grade_mma_rounds(bet: PlacedBet, fight: MmaFight) -> "str | None":
    """over/under X.5 rounds, graded on the round the fight ended in (a finish
    in round R, or R=scheduled_rounds for a decision). "Entered the round"
    convention: over X.5 needs round > X.5."""
    if fight.round is None or bet.line is None:
        return None
    if bet.side == "under":
        return "won" if fight.round < bet.line else "lost"
    return "won" if fight.round > bet.line else "lost"  # side "over"


def _grade_mma_method(bet: PlacedBet, fight: MmaFight) -> "str | None":
    """side in {decision, kotko, submission}; MmaFight.method is like
    'Decision - Unanimous' / 'KO/TKO' / 'Submission'."""
    if not fight.method:
        return None
    m = fight.method.lower()
    cat = "decision" if m.startswith("decision") else "kotko" if ("ko" in m or "tko" in m) else "submission" if "sub" in m else "other"
    side = (bet.side or "").lower()
    want = "kotko" if side in ("ko", "tko", "ko/tko", "kotko") else side
    return "won" if cat == want else "lost"


def _grade_mma_method_of_victory(bet: PlacedBet, fight: MmaFight) -> "str | None":
    """"<fighter> wins by KO/TKO" -- a JOINT condition, so BOTH halves must hold.

    Deliberately NOT _grade_mma_method, which checks only how the fight ended.
    Reusing it here would pay out whenever the fight was won by KO REGARDLESS OF
    BY WHOM -- so a bet on the loser would be booked as a win every time the
    other fighter scored the knockout. That is a silent wrong-way settlement,
    not a missing one.

    Follows _grade_mma_moneyline's side-resolution rather than string-comparing
    names: an unrecognised name returns None and the bet stays visibly pending,
    instead of falling through to a false loss (the Yadier del Valle case)."""
    from app.ingestion.market_matcher_mma import resolve_fight_side

    if fight.winner_id is None or not fight.method:
        return None  # draw / no-contest / unknown method -> not gradeable yet
    side = resolve_fight_side(bet.team, fight.fighter_a_name, fight.fighter_b_name)
    if side is None:
        return None  # can't tell which fighter was backed -> never assume a loss
    backed_id = fight.fighter_a_id if side == "a" else fight.fighter_b_id
    if fight.winner_id not in (fight.fighter_a_id, fight.fighter_b_id):
        return None  # winner isn't either listed fighter -> data problem, stay pending
    if fight.winner_id != backed_id:
        return "lost"  # backed fighter lost; how it ended is irrelevant
    m = fight.method.lower()
    cat = ("decision" if m.startswith("decision")
           else "kotko" if ("ko" in m or "tko" in m)
           else "submission" if "sub" in m else "other")
    side_wanted = (bet.side or "").lower()
    want = "kotko" if side_wanted in ("ko", "tko", "ko/tko", "kotko") else side_wanted
    return "won" if cat == want else "lost"


# ---- tennis (TennisMatch: winner_key, score) --------------------------------
def _tennis_winner_name(match: TennisMatch) -> "str | None":
    if match.winner_key == match.player_a_key:
        return match.player_a_name
    if match.winner_key == match.player_b_key:
        return match.player_b_name
    return None


def _grade_tennis_moneyline(bet: PlacedBet, match: TennisMatch) -> "str | None":
    """Graded on the credited winner, INCLUDING a retirement -- that is what the
    platforms do. Kalshi's own rule text (pulled live 2026-08-06 from
    KXATPMATCH): "If <player> wins the match ... AFTER A BALL HAS BEEN PLAYED,
    then the market resolves to Yes." So once play starts, a retirement still
    produces a real winner and this settles normally.

    The carve-out is a walkover BEFORE a ball is played, which Kalshi resolves
    "to a fair price" rather than to a side. We do not currently distinguish
    that case -- see _tennis_match_incomplete for why the retirement flag is
    not trustworthy -- but it is the rarer one and it does not affect which
    side wins when play DID happen.
    """
    w = _tennis_winner_name(match)
    if w is None:
        return None
    return "won" if _names_eq(bet.team, w) else "lost"


def _tennis_match_incomplete(match: TennisMatch) -> bool:
    """True when the score is not a completed match -- a retirement/walkover.

    DERIVED FROM THE SCORE, NOT match.is_retirement, because that flag is
    measured to be broken: of 1,878 resolved tennis matches, 78 have a score
    that cannot be a finished match ("4-1", "0-5", "6-0 1-0") and the flag is 0
    on ALL 78. It has never once fired. It is scraped by regexing "ret." out of
    a tennisexplorer row, and that text evidently is not there for these rows.

    WHY THIS MATTERS BEYOND MONEYLINE. Moneyline is fine on a retirement (the
    winner is real). Every DERIVATIVE market is not: a match abandoned at 4-1
    has no meaningful total games, set count, or margin, and grading one off
    the partial score invents a result. Measured 2026-08-06: 104 derivative
    bets had been settled exactly that way.

    A completed match needs at least 2 won sets (best-of-3; a best-of-5 winner
    also clears 2). A set is won at 6+ by two, or at 7 (tiebreak/7-5).
    """
    sets = _parse_sets(match.score)
    if not sets:
        return True  # no parseable score at all -- cannot confirm completion
    won = 0
    for a, b in sets:
        hi, lo = max(a, b), min(a, b)
        if (hi >= 6 and hi - lo >= 2) or hi == 7:
            won += 1
    return won < 2


def _parse_sets(score: "str | None") -> "list[tuple[int, int]]":
    """'6-4 6-3' -> [(6,4),(6,3)] in the match's player_a/player_b order.

    REAL BUG fixed 2026-08-02: the scrapers write a tiebreak set by appending the
    loser's tiebreak points to their game count -- '7-65' means 7-6 (tiebreak 7-5),
    NOT 7 games to 65. The old int() parse took it literally, so a single tiebreak
    set inflated a match's game total from ~10 to ~75 and inverted per-set winner
    comparisons (7 > 65 is False). 43 real settled tennis bets had such a score.
    A game count above 20 is impossible in any real set, so that's the detector;
    the leading digit is the true game count (6 or 7)."""
    out = []
    for s in (score or "").split():
        p = s.split("-")
        if len(p) != 2 or not p[0].isdigit() or not p[1].isdigit():
            continue  # retirements ("ret."), walkovers, junk -> skip the token
        a, b = int(p[0]), int(p[1])
        if a > 20:
            a = int(p[0][0])
        if b > 20:
            b = int(p[1][0])
        out.append((a, b))
    return out


def _tennis_side(bet: PlacedBet, match: TennisMatch) -> "str | None":
    if bet.team == match.player_a_name:
        return "a"
    if bet.team == match.player_b_name:
        return "b"
    return None


def _grade_tennis_set_spread(bet: PlacedBet, match: TennisMatch) -> "str | None":
    """Polymarket's "Set Handicap +/-1.5" -- a SET margin, not a games margin.

    Measured, not assumed: across 120 resolved markets on real 3-set matches
    every -1.5 side resolved to 0 and every +1.5 side to 1, which is the set
    handicap's defining behaviour and not the games handicap's (one bet covered
    +1 on games and still resolved 0). See _grade_tennis_game_spread for the
    Kalshi games version this was wrongly folded into.

    Same refuse-to-guess policy as every other tennis grader: an unparseable
    score, an unknown side, or a parse that disagrees with the recorded winner
    all return None rather than risk misgrading real money.
    """
    if bet.line is None:
        return None
    side = _tennis_side(bet, match)
    if side is None:
        return None
    sets = _parse_sets(match.score)
    if not sets:
        return None
    sets_a = sum(1 for a, b in sets if a > b)
    sets_b = sum(1 for a, b in sets if b > a)
    winner = _tennis_winner_name(match)
    if winner is None or sets_a == sets_b:
        return None
    parsed_winner = match.player_a_name if sets_a > sets_b else match.player_b_name
    if not _names_eq(parsed_winner, winner):
        return None
    margin = (sets_a - sets_b) if side == "a" else (sets_b - sets_a)
    # SIGNED LINE, unlike the Kalshi games version. Polymarket states the
    # handicap from each player's own side: -1.5 means "must win by more than
    # 1.5 sets", +1.5 means "must not lose by more than 1.5". Both are therefore
    # `margin > -line`, NOT `margin > line` -- copying the games grader's
    # comparison graded a 2-1 win as covering -1.5, which it does not.
    threshold = -bet.line
    if margin == threshold:
        return "push"
    return "won" if margin > threshold else "lost"


def _grade_tennis_game_spread(bet: PlacedBet, match: TennisMatch) -> "str | None":
    """Games-differential handicap. Convention is pinned by the ingestion layer
    (market_catalog_tennis.upsert_kalshi_tennis_game_spread_market /
    upsert_polymarket_tennis_set_handicap_row) and the model
    (game_lines_tennis.prob_game_spread_cover): `bet.team` is the player the YES
    side favors and `bet.line` is a "wins by MORE than this many games" threshold
    -- so a positive line means team must win by more than it, and a negative line
    means team must not lose by more than |line|.

    KALSHI ONLY. This used to claim Polymarket's "Set Handicap" resolved on the
    same games differential "confirmed live". That was wrong, and it misgraded
    real P/L. Tested against 120 resolved Polymarket markets on 3-set matches --
    where the two readings disagree, since nobody wins 2-0 in three sets -- the
    SET reading was correct 120/120 and the games reading 73/120. Those markets
    are now market_type "set_spread" and grade in _grade_tennis_set_spread.

    Left ungraded (returns None) rather than guessed whenever anything is
    uncertain -- an unparseable/incomplete score, an unknown player side, or a
    parsed score that DISAGREES with the recorded winner (which would mean the
    parse is wrong). Misgrading real P/L is worse than leaving a bet pending."""
    if bet.line is None:
        return None
    side = _tennis_side(bet, match)
    if side is None:
        return None
    sets = _parse_sets(match.score)
    if not sets:
        return None
    # Sanity gate: the parsed sets must agree with the recorded match winner.
    sets_a = sum(1 for a, b in sets if a > b)
    sets_b = sum(1 for a, b in sets if b > a)
    winner = _tennis_winner_name(match)
    if winner is None or sets_a == sets_b:
        return None
    parsed_winner = match.player_a_name if sets_a > sets_b else match.player_b_name
    if not _names_eq(parsed_winner, winner):
        return None  # parse disagrees with the real result -> don't guess
    games_a = sum(a for a, _ in sets)
    games_b = sum(b for _, b in sets)
    diff = (games_a - games_b) if side == "a" else (games_b - games_a)
    if diff == bet.line:
        return "push"  # lines are .5 in practice, but don't silently mis-call an exact tie
    return "won" if diff > bet.line else "lost"


def _grade_tennis_game_total(bet: PlacedBet, match: TennisMatch) -> "str | None":
    sets = _parse_sets(match.score)
    if not sets or bet.line is None:
        return None
    total = sum(a + b for a, b in sets)
    if total == bet.line:
        return "push"
    if bet.side == "under":
        return "won" if total < bet.line else "lost"
    return "won" if total > bet.line else "lost"


def _grade_tennis_total_sets(bet: PlacedBet, match: TennisMatch) -> "str | None":
    sets = _parse_sets(match.score)
    if not sets or bet.line is None:
        return None
    n = len(sets)
    if n == bet.line:
        return "push"
    if bet.side == "under":
        return "won" if n < bet.line else "lost"
    return "won" if n > bet.line else "lost"


def _grade_tennis_set_winner(bet: PlacedBet, match: TennisMatch) -> "str | None":
    sets = _parse_sets(match.score)
    side = _tennis_side(bet, match)
    if not sets or side is None or bet.line is None:
        return None
    idx = int(bet.line) - 1
    if idx < 0 or idx >= len(sets):
        return None  # that set was never played (match ended earlier) -> leave pending
    a, b = sets[idx]
    if a == b:
        return None
    return "won" if (("a" if a > b else "b") == side) else "lost"


def _grade_tennis_exact_score(bet: PlacedBet, match: TennisMatch) -> "str | None":
    sets = _parse_sets(match.score)
    side = _tennis_side(bet, match)
    if not sets or side is None or not bet.side or "-" not in bet.side:
        return None
    sa = sum(1 for a, b in sets if a > b)
    sb = sum(1 for a, b in sets if b > a)
    my, opp = (sa, sb) if side == "a" else (sb, sa)
    want = bet.side.split("-")
    if len(want) != 2 or not want[0].isdigit() or not want[1].isdigit():
        return None
    return "won" if (my == int(want[0]) and opp == int(want[1])) else "lost"


# ---- racing (RaceEvent.result_json = {"order":[driver_id...], "pole": id}) ---
def _race_result(event) -> "dict | None":
    import json
    try:
        return json.loads(event.result_json) if event.result_json else None
    except (ValueError, TypeError):
        return None


def _race_did(series: str, name: str) -> "str | None":
    from app.models.baseline import racing_ratings  # lazy: avoid import cycle
    return racing_ratings.resolve_driver_loose(series, name or "")


def _grade_racing_race_winner(bet: PlacedBet, event) -> "str | None":
    r = _race_result(event)
    did = _race_did(bet.sport, bet.team)
    if not r or not r.get("order") or not did:
        return None
    return "won" if r["order"][0] == did else "lost"


def _grade_racing_top_n(bet: PlacedBet, event) -> "str | None":
    r = _race_result(event)
    did = _race_did(bet.sport, bet.team)
    if not r or not r.get("order") or not did or bet.line is None:
        return None
    return "won" if did in set(r["order"][: int(bet.line)]) else "lost"


def _grade_racing_pole(bet: PlacedBet, event) -> "str | None":
    r = _race_result(event)
    did = _race_did(bet.sport, bet.team)
    if not r or not r.get("pole") or not did:
        return None
    return "won" if r["pole"] == did else "lost"


def _grade_racing_h2h(bet: PlacedBet, event) -> "str | None":
    import re
    r = _race_result(event)
    if not r or not r.get("order"):
        return None
    from app.models.baseline import racing_ratings as _rr

    pair = _rr.split_h2h_label(bet.team or "")
    if pair is None:
        return None
    a = _race_did(bet.sport, pair[0])
    b = _race_did(bet.sport, pair[1])
    order = r["order"]
    if not a or not b or a not in order or b not in order:
        return None
    return "won" if order.index(a) < order.index(b) else "lost"


_RACING_GRADERS = {
    "race_winner": _grade_racing_race_winner,
    "top_n": _grade_racing_top_n,
    "pole": _grade_racing_pole,
    "h2h": _grade_racing_h2h,
    # drivers_champion / constructors_champion are season futures -> never here.
}


_GRADERS = {  # score-based game sports (nfl/nba/wnba/mlb/cfb)
    "moneyline": _grade_moneyline,
    "spread": _grade_spread,
    "total": _grade_total,
    "team_total": _grade_team_total,
    # NFL half winner. Only NFL lists these today, and only NflGame carries
    # home_score_1h -- any other sport reaching here returns None (stays
    # pending) rather than raising, the same posture that kept soccer's
    # team_total collision from crashing every sport's settlement.
    "winner_1h": lambda b, g: _grade_half_winner(b, g, 1),
    "winner_2h": lambda b, g: _grade_half_winner(b, g, 2),
}

# Soccer gets its OWN table rather than sharing _GRADERS. Sharing was unsafe:
# the two sports overlap on the market_type NAME "team_total" while storing
# their scores in different columns (home_score vs home_goals_ft), so soccer
# silently picked up a grader that could not read its rows. A separate table
# also means an unrecognised soccer market_type resolves to None and the bet
# stays pending, instead of being graded by whatever the score sports happen
# to register under that name later.
def _grade_soccer_ftts(bet: PlacedBet, game: SoccerMatch) -> str | None:
    """First Team To Score. CANNOT be graded from the final score -- a 1-0 home
    win says nothing about who scored first in a 2-1, and a draw says nothing at
    all. Graded off SoccerMatch.first_scorer, populated from ESPN's scoring
    plays (espn_soccer_client._first_scorer).

    Returns None when first_scorer is NULL, which leaves the bet pending. That
    is deliberate and it is the whole point: before this existed, ftts bets sat
    permanently unsettled with no indication why. Pending-and-explainable beats
    graded-and-wrong.

    'N' means a real goalless match, so BOTH sides lose -- nobody scored first.
    """
    first = getattr(game, "first_scorer", None)
    if first is None:
        return None  # unknown -- stay pending rather than guess
    if first == "N":
        return "lost"  # 0-0: neither side scored first
    side_to_result = {"home": "H", "away": "A"}
    want = side_to_result.get(bet.side)
    if want is None:
        return None  # unrecognised side -- never guess
    return "won" if want == first else "lost"


_SOCCER_GRADERS = {
    "moneyline_3way": _grade_soccer_moneyline_3way,
    # DOMESTIC CUPS + UEFA (2026-08-08). These settle on REGULATION -- Kalshi
    # labels them "Reg Time" -- so the existing 90-minute graders are the right
    # ones and are reused rather than re-derived.
    #
    # WHY cup_advance IS ABSENT AND MUST STAY ABSENT: it settles on who
    # PROGRESSED, which includes extra time and penalties, and for UEFA
    # knockouts an aggregate over two legs. No column here carries that. An
    # unrecognised soccer market_type resolves to None and the bet stays
    # pending (see this table's own comment above), which is the correct
    # outcome -- pending is recoverable, a wrongly-graded bet is not.
    "cup_moneyline_3way": _grade_soccer_moneyline_3way,
    "uefa_moneyline_3way": _grade_soccer_moneyline_3way,
    "cup_total": _grade_soccer_total,
    "uefa_total": _grade_soccer_total,
    "leagues_cup_moneyline_3way": _grade_soccer_moneyline_3way,
    "leagues_cup_total": _grade_soccer_total,
    # Spread reuses the league grader, which reads bet.team as the favoured
    # side and bet.line as "wins by more than line" -- exactly the convention
    # upsert_kalshi_leagues_cup_spread_market writes.
    "leagues_cup_spread": _grade_soccer_spread,
    "leagues_cup_btts": _grade_soccer_btts,
    "national_moneyline_3way": _grade_soccer_moneyline_3way,
    "national_total": _grade_soccer_total,
    "national_spread": _grade_soccer_spread,
    "national_btts": _grade_soccer_btts,
    "game_spread": _grade_soccer_spread,
    "game_total": _grade_soccer_total,
    "team_total": _grade_soccer_team_total,
    "btts": _grade_soccer_btts,
    # Halves. First half is read straight off the half-time score; second half
    # is full time minus half time. Each returns None when the half-time score
    # is missing, so those bets stay pending instead of being guessed.
    "first_half_winner": lambda b, g: _grade_soccer_half_winner(b, g, 1),
    "second_half_winner": lambda b, g: _grade_soccer_half_winner(b, g, 2),
    "first_half_total": lambda b, g: _grade_soccer_half_total(b, g, 1),
    "second_half_total": lambda b, g: _grade_soccer_half_total(b, g, 2),
    "first_half_team_total": lambda b, g: _grade_soccer_half_team_total(b, g, 1),
    "second_half_team_total": lambda b, g: _grade_soccer_half_team_total(b, g, 2),
    "second_half_btts": _grade_soccer_second_half_btts,
    # Graded off first_scorer, NOT the final score -- see the grader.
    "ftts": _grade_soccer_ftts,
}

_MMA_GRADERS = {
    "moneyline": _grade_mma_moneyline,
    "distance": _grade_mma_distance,
    "rounds": _grade_mma_rounds,
    "method_of_finish": _grade_mma_method,
    "method_of_victory": _grade_mma_method_of_victory,
}

def _grade_esports_map_winner(bet: PlacedBet, match) -> "str | None":
    """"Who wins map N", graded off the per-map rows lol_map_results writes.

    map_winner was previously ungradeable for the reason this file used to note
    here: only the SERIES score was stored. Per-map winners now exist for two of
    the three titles -- LoL from gol.gg (one page per game) and Valorant from
    vlr.gg match pages -- so the note no longer holds for them.

    CS2 is still excluded, and not by oversight: its own source (Liquipedia) is
    Cloudflare-gated (403 live), so CS2 matches currently get no result at all,
    let alone per-map ones. Its bets return None and stay pending rather than
    being guessed at.

    bet.line is the MAP NUMBER, not a handicap (see paper_logger's own note on
    the same field).
    """
    if bet.line is None:
        return None
    side = _esports_side(bet, match)
    if side is None:
        return None
    session = object_session(match)
    if session is None:
        return None
    if bet.sport == "lol":
        model, link = LolMap, LolMap.lol_match_id
    elif bet.sport == "valorant":
        model, link = ValorantMap, ValorantMap.valorant_match_id
    else:
        return None
    row = (
        session.query(model)
        .filter(link == match.id, model.map_number == int(bet.line))
        .one_or_none()
    )
    if row is None or row.winner is None:
        return None
    return "won" if row.winner == side else "lost"


_ESPORTS_GRADERS = {
    "series_winner": _grade_esports_series_winner,
    "series_total": _grade_esports_series_total,
    "map_winner": _grade_esports_map_winner,
}

def _complete_only(grader):
    """Wrap a derivative tennis grader so it REFUSES an unfinished match.

    Returns None (bet stays pending for a human call) rather than grading a
    partial score. Deliberately not "lost" and not "void": voiding
    automatically would be this app deciding a platform's settlement policy for
    it, and those differ -- Kalshi's own match rules turn on "after a ball has
    been played", which is a different test again. Pending is the honest state,
    and the void button exists for exactly this.

    Moneyline is NOT wrapped: a retirement produces a real winner and both
    platforms settle it, so refusing there would leave correct bets unsettled.
    """
    def wrapped(bet: PlacedBet, match: TennisMatch):
        if _tennis_match_incomplete(match):
            return None
        return grader(bet, match)
    return wrapped


_TENNIS_GRADERS = {
    "moneyline": _grade_tennis_moneyline,
    "game_total": _complete_only(_grade_tennis_game_total),
    "total_sets": _complete_only(_grade_tennis_total_sets),
    "set_winner": _complete_only(_grade_tennis_set_winner),
    "exact_score": _complete_only(_grade_tennis_exact_score),
    # game_spread added 2026-08-02: the storage convention IS now pinned down
    # (see _grade_tennis_game_spread) -- team = the favored player, line = "wins
    # by more than this many games", identical for Kalshi and Polymarket. The
    # grader still refuses to guess (returns None) when the score can't be
    # parsed or disagrees with the recorded winner.
    "game_spread": _complete_only(_grade_tennis_game_spread),
    "set_spread": _complete_only(_grade_tennis_set_spread),
    # set_total still deliberately absent: its side semantics remain ambiguous,
    # and misgrading real P/L is worse than leaving it pending.
}


def effective_market_type(session: Session, bet: PlacedBet) -> "str | None":
    """The market's CURRENT type, not the copy frozen on the bet.

    PlacedBet.market_type is a snapshot taken at placement. That is the right
    thing for the tracker -- it records what you thought you were betting -- but
    it is the WRONG thing to dispatch a grader on, because when a market is
    later re-typed the bet keeps pointing at the old grader forever.

    REAL DAMAGE this caused (measured 2026-08-06): 499 Polymarket tennis bets,
    8 of them REAL money, carried market_type "game_spread" while every one of
    the 5,892 Polymarket tennis spread markets had since been re-typed
    "set_spread". They kept routing to _grade_tennis_game_spread, whose own
    docstring says KALSHI ONLY -- Polymarket's Set Handicap resolves on SETS,
    not games. Back-tested against Polymarket's own resolution, that grader
    flipped 21.7% of them, against <=2.1% for every other market type.

    Every PlacedBet has a Market row (verified: 18,097 of 18,097), so the live
    type is always available; the snapshot is kept only as the fallback that
    cannot normally fire.
    """
    market = session.get(Market, bet.market_id) if bet.market_id else None
    return (market.market_type if market and market.market_type else bet.market_type)


def _pick_grader(bet: PlacedBet, market_type: "str | None" = None):
    """Grader is chosen by (sport, market_type) -- market_type alone collides now
    (mma/tennis/game sports all have "moneyline").

    `market_type` is passed in by callers that resolved it via
    effective_market_type; it falls back to the bet's snapshot only so this stays
    callable in isolation.
    """
    mt = market_type or bet.market_type
    if bet.sport == "mma":
        return _MMA_GRADERS.get(mt)
    if bet.sport in ("cs2", "valorant", "lol", "cod"):
        return _ESPORTS_GRADERS.get(mt)
    if bet.sport == "tennis":
        return _TENNIS_GRADERS.get(mt)
    if bet.sport in ("f1", "irl", "nascar"):
        return _RACING_GRADERS.get(mt)
    if bet.sport == "soccer":
        return _SOCCER_GRADERS.get(mt)
    return _GRADERS.get(mt)  # nfl/nba/wnba/cfb/mlb


def _settlement_note(bet: PlacedBet, game) -> str:
    if bet.sport == "soccer":
        return f"auto-settled: final score {game.away_team} {game.away_goals_ft} @ {game.home_team} {game.home_goals_ft}"
    if bet.sport in ("cs2", "valorant", "lol", "cod"):
        win = game.team_a if game.winner == "team_a" else game.team_b
        return f"auto-settled: {game.team_a} {game.maps_won_a}-{game.maps_won_b} {game.team_b} (winner {win})"
    if bet.sport == "mma":
        return f"auto-settled: {_mma_winner_name(game) or 'no result'} by {game.method or '?'} in R{game.round or '?'}"
    if bet.sport == "tennis":
        return f"auto-settled: winner {_tennis_winner_name(game) or '?'} ({game.score or 'score n/a'})"
    if bet.sport in ("f1", "irl", "nascar"):
        return "auto-settled: race result"
    return f"auto-settled: final score {game.away_team} {game.away_score} @ {game.home_team} {game.home_score}"


# A market that went `inactive` and stayed that way this long after its own
# start time is cancelled, not merely between polls. 6h is chosen against the
# shortest event type this can fire on: a CS2 series runs 1-3 hours, so at +6h
# the match is certainly over AND the market did not come back. Raising this is
# free (bets simply stay pending); lowering it risks voiding a market that was
# briefly deactivated mid-event.
_DELISTED_VOID_GRACE_HOURS = 6


def void_delisted_markets(session: Session) -> int:
    """Void pending bets whose market was DELISTED without ever producing a
    result -- walkovers, cancellations, withdrawn listings.

    THE GAP THIS FILLS, stated precisely. A walkover produces no played match,
    so no result scraper will ever return a winner and every grader here is
    stuck; #84's void path keys on the FIXTURE being cancelled or replaced, not
    on the market being pulled.

    It is NOT true that nothing handled this. Watched live on 2026-08-12: a real
    $10 CS2 bet on a Grêmio walkover self-resolved via
    _settle_stragglers_from_platform -> settle_pending_from_kalshi, which asked
    Kalshi and got `result=void`, ~4h after start. That path is correct and this
    function must never pre-empt it -- hence being called LAST.

    What that path deliberately does NOT cover is PAPER bets: it filters
    `paper == False` and caps at _MAX_PLATFORM_LOOKUPS, because ~1,600 stuck
    paper bets turned it into thousands of API calls and a 600s+ stall. So a
    paper bet on a delisted market has nothing to clear it, ever. Two were
    sitting 158h and 108h past their start when this was written. That is the
    real hole: not the real-money case, the paper one -- which matters because
    paper bets are the entire measurement substrate.

    `inactive` is Kalshi's own status, stored verbatim by the ingester -- no
    code in this app writes it -- and it is distinct from `closed`, which means
    trading stopped and a result is still coming. Voiding `closed` would be
    wrong and would hit 133 pending bets; this deliberately touches only
    `inactive`.

    Conservative by construction: the start time must be KNOWN and at least
    _DELISTED_VOID_GRACE_HOURS past, so a market that is temporarily inactive
    before its event can never be voided. A bet with no resolvable kickoff is
    left alone rather than guessed at."""
    import datetime

    from app.models.clv import _game_kickoff_dt, _get_game

    now = datetime.datetime.utcnow()
    rows = (
        session.query(PlacedBet)
        .join(Market, PlacedBet.market_id == Market.id)
        .filter(PlacedBet.status == "pending", Market.status == "inactive")
        .all()
    )
    voided = 0
    for bet in rows:
        game = _get_game(session, bet)
        kickoff = _game_kickoff_dt(game) if game is not None else None
        if kickoff is None:
            continue  # cannot establish that the event is over -> leave it pending
        hours = (now - kickoff).total_seconds() / 3600.0
        if hours < _DELISTED_VOID_GRACE_HOURS:
            continue
        bet.status = "void"
        bet.settled_at = now
        bet.settlement_note = (
            f"voided: market delisted by {bet.source or 'the platform'} with no result, "
            f"{hours:.0f}h after start (walkover/cancellation)"
        )
        voided += 1
    if voided:
        session.commit()
        log.info("voided %d placed bets on delisted markets", voided)
    return voided


def settle_finished_games(session: Session) -> int:
    """Grades every pending, auto-gradeable placed bet whose game now has a
    final score. Returns the number settled."""
    # Gate on the market's LIVE type, not the bet's frozen snapshot -- see
    # effective_market_type. Filtering on the snapshot both let re-typed bets
    # reach the wrong grader and hid bets whose market had since BECOME
    # auto-settleable.
    pending = (
        session.query(PlacedBet)
        .join(Market, PlacedBet.market_id == Market.id)
        .filter(PlacedBet.status == "pending", Market.market_type.in_(AUTO_SETTLE_MARKET_TYPES))
        .all()
    )
    settled = 0
    import datetime

    for bet in pending:
        game = _get_game(session, bet)
        if game is None or not _game_is_final(bet, game):
            continue  # not final yet
        grader = _pick_grader(bet, effective_market_type(session, bet))
        if grader is None:
            continue
        result = grader(bet, game)
        if result not in ("won", "lost", "push"):
            continue  # grader couldn't resolve (e.g. team-name mismatch, draw) -> leave pending
        bet.status = result
        bet.settled_at = datetime.datetime.utcnow()
        bet.settlement_note = _settlement_note(bet, game)
        settled += 1

    if settled:
        session.commit()
        log.info("auto-settled %d placed bets from final scores", settled)

    # Fallback for bets the result scrapers haven't caught up on: ask Kalshi
    # for its own market result. Only for bets already past their scheduled
    # start, so this is a few single-market lookups, not a crawl. See
    # kalshi_settlement.py for why it is deliberately narrow.
    settled += _settle_stragglers_from_platform(session)
    # LAST, deliberately: only bets that no result path could grade should ever
    # reach the void rule. Running it earlier would void a market that the
    # platform lookup was about to settle properly.
    settled += void_delisted_markets(session)
    return settled


# Most lookups one settlement pass may make (1 HTTP call each).
_MAX_PLATFORM_LOOKUPS = 60


def _settle_stragglers_from_platform(session: Session) -> int:
    import datetime

    from app.models.clv import _game_kickoff_dt, _get_game
    from app.models.kalshi_settlement import settle_pending_from_kalshi
    from app.models.polymarket_settlement import settle_pending_from_polymarket

    now = datetime.datetime.utcnow()
    stuck = []
    # REAL-money bets only, and capped. This makes ONE network call per bet, and
    # there are ~1,600 stuck PAPER bets -- including them turned a quick fallback
    # into thousands of API calls that stalled outright (600s+, caught in
    # testing). Paper bets exist to accrue CLV and are graded by the normal
    # result path; they do not need an authoritative platform lookup.
    candidates = (
        session.query(PlacedBet)
        .filter(PlacedBet.status == "pending", PlacedBet.paper == False)  # noqa: E712
        .all()
    )
    for bet in candidates:
        game = _get_game(session, bet)
        kickoff = _game_kickoff_dt(game) if game is not None else None
        if kickoff is None or (now - kickoff).total_seconds() < 4 * 3600:
            continue
        stuck.append(bet)
    if not stuck:
        return 0
    # Hard cap so one bad day can never turn this into an unbounded crawl.
    stuck = stuck[:_MAX_PLATFORM_LOOKUPS]
    # Both platforms: Kalshi resolves a whole market yes/no, Polymarket publishes
    # a per-outcome price, so the two paths grade different market types.
    return settle_pending_from_kalshi(session, stuck) + settle_pending_from_polymarket(session, stuck)

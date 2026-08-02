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

from app.db.models import (
    Cs2Match, LolMatch, MlbGame, MmaFight, NbaGame, NflGame, PlacedBet,
    RaceEvent, SoccerMatch, TennisMatch, ValorantMatch, WnbaGame,
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
    # mma
    "distance", "rounds", "method_of_finish",
    # esports (cs2/valorant/lol)
    "series_winner", "series_total",
    # tennis (moneyline shared above); set/game markets
    "set_winner", "set_total", "exact_score",
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
    if bet.sport in ("cs2", "valorant", "lol"):
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
    w = _mma_winner_name(fight)
    if w is None:
        return None
    return "won" if _names_eq(bet.team, w) else "lost"


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


# ---- tennis (TennisMatch: winner_key, score) --------------------------------
def _tennis_winner_name(match: TennisMatch) -> "str | None":
    if match.winner_key == match.player_a_key:
        return match.player_a_name
    if match.winner_key == match.player_b_key:
        return match.player_b_name
    return None


def _grade_tennis_moneyline(bet: PlacedBet, match: TennisMatch) -> "str | None":
    w = _tennis_winner_name(match)
    if w is None:
        return None
    return "won" if _names_eq(bet.team, w) else "lost"


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


def _grade_tennis_game_spread(bet: PlacedBet, match: TennisMatch) -> "str | None":
    """Games-differential handicap. Convention is pinned by the ingestion layer
    (market_catalog_tennis.upsert_kalshi_tennis_game_spread_market /
    upsert_polymarket_tennis_set_handicap_row) and the model
    (game_lines_tennis.prob_game_spread_cover): `bet.team` is the player the YES
    side favors and `bet.line` is a "wins by MORE than this many games" threshold
    -- so a positive line means team must win by more than it, and a negative line
    means team must not lose by more than |line|. Polymarket names its version
    "Set Handicap" but it was confirmed live to resolve on the same games
    differential, so both sources grade identically here.

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
    parts = re.split(r"\s+vs\.?\s+", bet.team or "", flags=re.IGNORECASE)
    if len(parts) != 2:
        return None
    a = _race_did(bet.sport, parts[0].strip())
    b = _race_did(bet.sport, parts[1].strip())
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


_GRADERS = {  # score-based game sports (nfl/nba/wnba/mlb) + soccer
    "moneyline": _grade_moneyline,
    "spread": _grade_spread,
    "total": _grade_total,
    "team_total": _grade_team_total,
    "moneyline_3way": _grade_soccer_moneyline_3way,
    "game_spread": _grade_soccer_spread,
    "game_total": _grade_soccer_total,
    "btts": _grade_soccer_btts,
}

_MMA_GRADERS = {
    "moneyline": _grade_mma_moneyline,
    "distance": _grade_mma_distance,
    "rounds": _grade_mma_rounds,
    "method_of_finish": _grade_mma_method,
}

_ESPORTS_GRADERS = {
    "series_winner": _grade_esports_series_winner,
    "series_total": _grade_esports_series_total,
    # map_winner deliberately absent: we store only the SERIES map score
    # (maps_won_a/b), never per-map winners, so "who wins map N" isn't gradeable.
}

_TENNIS_GRADERS = {
    "moneyline": _grade_tennis_moneyline,
    "game_total": _grade_tennis_game_total,
    "total_sets": _grade_tennis_total_sets,
    "set_winner": _grade_tennis_set_winner,
    "exact_score": _grade_tennis_exact_score,
    # game_spread added 2026-08-02: the storage convention IS now pinned down
    # (see _grade_tennis_game_spread) -- team = the favored player, line = "wins
    # by more than this many games", identical for Kalshi and Polymarket. The
    # grader still refuses to guess (returns None) when the score can't be
    # parsed or disagrees with the recorded winner.
    "game_spread": _grade_tennis_game_spread,
    # set_total still deliberately absent: its side semantics remain ambiguous,
    # and misgrading real P/L is worse than leaving it pending.
}


def _pick_grader(bet: PlacedBet):
    """Grader is chosen by (sport, market_type) -- market_type alone collides now
    (mma/tennis/game sports all have "moneyline")."""
    if bet.sport == "mma":
        return _MMA_GRADERS.get(bet.market_type)
    if bet.sport in ("cs2", "valorant", "lol"):
        return _ESPORTS_GRADERS.get(bet.market_type)
    if bet.sport == "tennis":
        return _TENNIS_GRADERS.get(bet.market_type)
    if bet.sport in ("f1", "irl", "nascar"):
        return _RACING_GRADERS.get(bet.market_type)
    return _GRADERS.get(bet.market_type)  # nfl/nba/wnba/mlb/soccer


def _settlement_note(bet: PlacedBet, game) -> str:
    if bet.sport == "soccer":
        return f"auto-settled: final score {game.away_team} {game.away_goals_ft} @ {game.home_team} {game.home_goals_ft}"
    if bet.sport in ("cs2", "valorant", "lol"):
        win = game.team_a if game.winner == "team_a" else game.team_b
        return f"auto-settled: {game.team_a} {game.maps_won_a}-{game.maps_won_b} {game.team_b} (winner {win})"
    if bet.sport == "mma":
        return f"auto-settled: {_mma_winner_name(game) or 'no result'} by {game.method or '?'} in R{game.round or '?'}"
    if bet.sport == "tennis":
        return f"auto-settled: winner {_tennis_winner_name(game) or '?'} ({game.score or 'score n/a'})"
    if bet.sport in ("f1", "irl", "nascar"):
        return "auto-settled: race result"
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
        grader = _pick_grader(bet)
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
    return settled

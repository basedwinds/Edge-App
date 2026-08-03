"""Backfills PlacedBet.league for bets placed before that column existed.

The column is normally captured at placement time (the frontend already knows
the league from the market row it is looking at), so this only exists for the
history: without it every pre-existing bet renders with a blank league on the
Bet Tracker, which makes the feature look broken rather than new.

League is NOT on the Market row -- group_label is null across the board -- it
lives on each sport's own match table, so this joins per sport and mirrors the
labels the frontend builds:

    tennis    TennisMatch.tour + .tier   -> "ATP · Grand Slam"
    soccer    SoccerMatch.league         -> raw league code
    esports   <Match>.event_name         -> tournament name

Sports with a single league (nfl/nba/wnba/mlb/cfb/mma/racing) are deliberately
left null -- "NFL" as both sport and league is noise, not information.

Idempotent: only touches rows where league IS NULL.
"""
import logging

from sqlalchemy.orm import Session

from app.db.models import Cs2Match, LolMatch, PlacedBet, SoccerMatch, TennisMatch, ValorantMatch

log = logging.getLogger("league_backfill")

def _tennis_label(tour: str | None, tier: str | None) -> str | None:
    """"ATP Tour", "ITF Women", "ATP Challenger". Mirrors tennisLeagueLabel in
    the frontend EXACTLY -- two spellings of one league would split the Bet
    Tracker search box.

    `tour` is the gender circuit (atp/wta), `tier` the real competition. The
    earlier version joined them into "WTA ITF", which reads as two leagues: an
    ITF event is not a WTA event."""
    t = (tour or "").lower()
    women = t == "wta"
    if tier == "itf":
        return "ITF Women" if women else "ITF Men"
    if tier == "challenger":
        return "WTA 125" if women else "ATP Challenger"
    if tier == "tour":
        return "WTA Tour" if women else "ATP Tour"
    return t.upper() or None


# (sport, PlacedBet id attr, match model, how to build the label from the row)
_SOURCES = [
    ("tennis", "tennis_match_id", TennisMatch, lambda m: _tennis_label(m.tour, m.tier)),
    ("soccer", "soccer_match_id", SoccerMatch, lambda m: m.league),
    ("valorant", "valorant_match_id", ValorantMatch, lambda m: m.event_name),
    ("cs2", "cs2_match_id", Cs2Match, lambda m: m.event_name),
    ("lol", "lol_match_id", LolMatch, lambda m: m.event_name),
]


def backfill_leagues(session: Session) -> int:
    """Fill league on bets that have none. Returns how many rows were set."""
    filled = 0
    for sport, id_attr, model, label_of in _SOURCES:
        bets = (
            session.query(PlacedBet)
            .filter(PlacedBet.sport == sport, PlacedBet.league.is_(None))
            .all()
        )
        if not bets:
            continue
        match_ids = {getattr(b, id_attr) for b in bets if getattr(b, id_attr)}
        if not match_ids:
            continue
        by_id = {m.id: m for m in session.query(model).filter(model.id.in_(match_ids)).all()}
        for bet in bets:
            match = by_id.get(getattr(bet, id_attr))
            if match is None:
                continue  # match row pruned/never ingested -- leave null rather than guess
            label = label_of(match)
            if label:
                bet.league = label
                filled += 1
    if filled:
        session.commit()
        log.info("league backfill: set %d placed bets", filled)
    return filled

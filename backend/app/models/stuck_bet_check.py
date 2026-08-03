"""Flags PENDING bets whose event should long since have finished.

WHY. Every settlement/timing bug found on 2026-08-03 was spotted by the user
reading the tracker, never by the app: a LoL fixture whose date got overwritten
to a future rematch so the played match could never settle; a Polymarket CS2 bet
with no settlement fallback; a tennis match recommended hours after Kalshi had
already finalized it. They had different causes and one shared symptom -- a bet
sat pending long after its event was over.

So this checks the SYMPTOM rather than any one cause. It needs no scraper, no
platform call and no per-sport knowledge, which is exactly why it still fires
when a brand-new failure mode shows up.

It only reports. Settlement decisions stay in bet_settlement.py, where a wrong
call costs real money; here a false positive costs a line of log output.
"""
import datetime
import logging

from sqlalchemy.orm import Session

from app.db.models import PlacedBet

log = logging.getLogger("stuck_bet_check")

# An event this far past its start, with the bet still pending, is not "running
# long" -- something is wrong. Deliberately generous: a 5-set match plus
# settlement lag is comfortably inside it, so anything caught is a real problem.
STUCK_AFTER = datetime.timedelta(hours=12)


def find_stuck_bets(session: Session, now: datetime.datetime | None = None) -> list[dict]:
    """Pending bets whose known start is more than STUCK_AFTER in the past.

    Returns one dict per bet with enough context to act without re-querying:
    which sport, how long overdue, and whether we even have a result row -- that
    last field is what distinguishes "the scraper is behind" from "this bet is
    pointing at the wrong fixture".
    """
    from app.models.clv import _game_kickoff_dt, _get_game

    now = now or datetime.datetime.utcnow()
    stuck: list[dict] = []
    for bet in session.query(PlacedBet).filter(PlacedBet.status == "pending").all():
        game = _get_game(session, bet)
        start = _game_kickoff_dt(game) if game is not None else None
        if start is None or (now - start) <= STUCK_AFTER:
            continue
        stuck.append({
            "bet_id": bet.id,
            "sport": bet.sport,
            "paper": bool(bet.paper),
            "market_type": bet.market_type,
            "label": bet.label,
            "started": start.isoformat(),
            "hours_overdue": round((now - start).total_seconds() / 3600, 1),
            "has_event_row": game is not None,
        })
    stuck.sort(key=lambda r: -r["hours_overdue"])
    return stuck


def report_stuck_bets(session: Session) -> int:
    """Log the stuck list. Returns how many REAL (non-paper) bets are stuck --
    paper bets are counted separately because they cost nothing and would
    otherwise drown out the ones that hold actual money."""
    rows = find_stuck_bets(session)
    real = [r for r in rows if not r["paper"]]
    if not rows:
        log.info("stuck-bet check: none")
        return 0
    log.warning(
        "stuck-bet check: %d pending bets >%dh past their start (%d real, %d paper)",
        len(rows), int(STUCK_AFTER.total_seconds() // 3600), len(real), len(rows) - len(real),
    )
    for r in real[:20]:
        log.warning(
            "  STUCK %s %s | %s | %.1fh overdue | started %s",
            r["sport"], r["market_type"], r["label"][:60], r["hours_overdue"], r["started"],
        )
    return len(real)

"""Backfills FINAL results (winner + set score) onto live TennisMatch rows so
placed tennis bets can auto-settle. The live match rows are created upcoming
(for bet linkage) and nobody fills in the result once the match is played -- this
closes that gap by pulling tennisexplorer's results-day pages for the play dates
and matching on the {player_a_key, player_b_key} pair.

Match rate is partial (~50%): some live rows are speculative Kalshi-created
matches that never actually happened, and some real matches have name/accent
variants that don't key-match. Unmatched rows simply stay winner_key=None (never
misgraded), and get retried next cycle.
"""
import datetime
import logging

from sqlalchemy.orm import Session

from app.clients.tennisexplorer_client import TennisExplorerClient
from app.db.models import TennisMatch

log = logging.getLogger("tennis_results")

_LOOKBACK_DAYS = 10  # only backfill recent finished matches, not the whole history


def _rkey(name: str | None) -> str:
    """tennisexplorer result names are already "Surname I." -- the same shape as
    TennisMatch.player_a_key ("surname i."), just cased -- so lowercasing is the
    match key (NOT re-abbreviating, which would garble an already-short name)."""
    return (name or "").lower().strip()


def _flip_score(score: str | None) -> str | None:
    """Flip each "a-b" set to "b-a" so a result stored in the opposite player
    order still reads in the live match's player_a/player_b order."""
    if not score:
        return score
    out = []
    for s in score.split():
        parts = s.split("-")
        out.append(f"{parts[1]}-{parts[0]}" if len(parts) == 2 else s)
    return " ".join(out)


def _finished_ungraded(session: Session) -> list:
    now = datetime.datetime.utcnow().isoformat() + "Z"
    cutoff = (datetime.date.today() - datetime.timedelta(days=_LOOKBACK_DAYS)).isoformat()
    return [
        m for m in session.query(TennisMatch).filter(TennisMatch.winner_key.is_(None)).all()
        if m.estimated_start_time and m.estimated_start_time < now
        and (m.estimated_start_time or "")[:10] >= cutoff
    ]


def fetch_results_index(session: Session) -> dict:
    """Network step (call OUTSIDE the write lock): read which finished matches
    need a result, fetch those play-dates from tennisexplorer, return a global
    {player-pair -> result} index. Empty when nothing to do."""
    finished = _finished_ungraded(session)
    if not finished:
        return {}
    dates: set[datetime.date] = set()
    for m in finished:
        d = datetime.date.fromisoformat((m.estimated_start_time or m.match_date)[:10])
        for off in (-1, 0, 1):  # +/- 1 day for the UTC boundary
            dates.add(d + datetime.timedelta(days=off))
    cli = TennisExplorerClient()
    index: dict[frozenset, dict] = {}
    for d in sorted(dates):
        try:
            rows = cli.get_results_day(d.year, d.month, d.day)
        except Exception:
            continue  # tennisexplorer is flaky; skip the date, retried next cycle
        for r in rows:
            if r.get("winner"):
                index[frozenset({_rkey(r["player_a_name"]), _rkey(r["player_b_name"])})] = r
    return index


def apply_results_index(session: Session, index: dict) -> int:
    """Write step (call UNDER the write lock): set winner_key + score on finished
    rows from an already-fetched index. Returns the number newly resolved."""
    if not index:
        return 0
    resolved = 0
    for m in _finished_ungraded(session):
        r = index.get(frozenset({m.player_a_key, m.player_b_key}))
        if not r:
            continue
        win_name = r["player_a_name"] if r["winner"] == "a" else r["player_b_name"]
        wk = _rkey(win_name)
        if wk == m.player_a_key:
            m.winner_key = m.player_a_key
        elif wk == m.player_b_key:
            m.winner_key = m.player_b_key
        else:
            continue  # winner name didn't key-match either side -> leave pending
        # store the score in the live match's a/b order (result_a == live_a?)
        result_a_is_live_a = _rkey(r["player_a_name"]) == m.player_a_key
        m.score = r.get("score") if result_a_is_live_a else _flip_score(r.get("score"))
        m.is_retirement = bool(r.get("is_retirement")) or bool(m.is_retirement)
        resolved += 1

    if resolved:
        session.commit()
        log.info("tennis results backfill: resolved %d finished matches", resolved)
    return resolved

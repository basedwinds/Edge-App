"""Backfills FINAL results (winner + map score) onto live LolMatch rows so
placed LoL bets can auto-settle. LoL live rows were never getting their winner
filled in (known gap) even though Leaguepedia's cargoquery -- the same source
lol_data.fetch_matches already parses -- returns Winner/Team1Score/Team2Score
for completed matches. Matches on the normalized {team_a, team_b} pair.

Unmatched rows (name variants, matches Leaguepedia hasn't posted, or Kalshi-only
matchups) stay winner=None and are retried next cycle -- never misgraded.
"""
import datetime
import logging

from sqlalchemy.orm import Session

from app.db.models import LolMatch
from app.ingestion import lol_data

log = logging.getLogger("lol_results")

_LOOKBACK_DAYS = 10


def _norm(name: str | None) -> str:
    return (name or "").lower().strip()


def fetch_lol_results() -> list[dict]:
    """Network fetch of recent completed matches -- call OUTSIDE the DB write
    lock (Leaguepedia's cargoquery can burn 100+s under rate-limit backoff, and
    holding the shared poller lock that long starves every other sport)."""
    try:
        return lol_data.fetch_matches(days_back=_LOOKBACK_DAYS + 2, days_forward=0)
    except Exception:
        log.exception("lol results fetch failed (rate-limited?) -- retried next cycle")
        return []


def apply_lol_results(session: Session, results: list[dict]) -> int:
    """Populate winner + maps_won_a/b on finished-but-ungraded LolMatch rows from
    already-fetched results. Returns the number newly resolved."""
    if not results:
        return 0
    now = datetime.datetime.utcnow().isoformat() + "Z"
    cutoff = (datetime.date.today() - datetime.timedelta(days=_LOOKBACK_DAYS)).isoformat()
    finished = [
        m for m in session.query(LolMatch).filter(LolMatch.winner.is_(None)).all()
        if m.estimated_start_time and m.estimated_start_time < now
        and (m.estimated_start_time or "")[:10] >= cutoff
    ]
    if not finished:
        return 0

    index: dict[frozenset, dict] = {}
    for r in results:
        if r.get("winner"):
            index[frozenset({_norm(r.get("team_a")), _norm(r.get("team_b"))})] = r

    resolved = 0
    for m in finished:
        r = index.get(frozenset({_norm(m.team_a), _norm(m.team_b)}))
        if not r:
            continue
        # orient the map score + winner to the live row's team_a/team_b order
        same_order = _norm(r.get("team_a")) == _norm(m.team_a)
        m.maps_won_a = r.get("maps_won_a") if same_order else r.get("maps_won_b")
        m.maps_won_b = r.get("maps_won_b") if same_order else r.get("maps_won_a")
        win = r.get("winner")  # "team_a"/"team_b" in the RESULT's order
        m.winner = win if same_order else ("team_b" if win == "team_a" else "team_a")
        resolved += 1

    if resolved:
        session.commit()
        log.info("lol results backfill: resolved %d finished matches", resolved)
    return resolved

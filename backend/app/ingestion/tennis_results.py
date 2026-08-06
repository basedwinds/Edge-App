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


def _surname_key(key: str) -> "str | None":
    """Trim a MIDDLE NAME out of a player key, or None if there is nothing to
    trim. "lennard struff j." -> "struff j."

    REAL GAP this closes (measured 2026-08-06): TennisMatch.player_a_key is
    built by treating everything after the first word as the surname, which is
    right for a genuine compound surname ("Carreno Busta P.", "Van Assche L.")
    but wrong whenever the player has a middle name -- Jan-Lennard Struff
    became "lennard struff j." while tennisexplorer says "Struff J.". 373 of
    820 pending matches carried such a key, and the results join could never
    fire for them: it resolved 2 matches out of 820.

    Used ONLY as a fallback after the exact key misses, and only when the
    trimmed form is UNAMBIGUOUS in that day's results (see _build_alt_index) --
    trimming can in principle collide two different players, and a wrong
    winner is far worse than an unresolved match.
    """
    parts = key.split()
    if len(parts) >= 3 and parts[-1].endswith("."):
        return " ".join(parts[-2:])
    return None


def _build_alt_index(index: dict) -> dict:
    """{trimmed pair -> result}, dropping any trimmed key that is not unique.

    A collision means two different real matches would map to the same trimmed
    pair; those are excluded entirely rather than guessed between.
    """
    counts: dict = {}
    for pair in index:
        alt = frozenset({_surname_key(k) or k for k in pair})
        if len(alt) != 2:
            continue  # trimming merged the two sides -- unusable
        counts[alt] = counts.get(alt, 0) + 1
    return {
        frozenset({_surname_key(k) or k for k in pair}): r
        for pair, r in index.items()
        if counts.get(frozenset({_surname_key(k) or k for k in pair})) == 1
    }


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
    alt_index = _build_alt_index(index)
    resolved = 0
    for m in _finished_ungraded(session):
        r = index.get(frozenset({m.player_a_key, m.player_b_key}))
        if not r:
            # Fallback: same match with a middle name trimmed off either side.
            a_alt = _surname_key(m.player_a_key) or m.player_a_key
            b_alt = _surname_key(m.player_b_key) or m.player_b_key
            if a_alt != b_alt:
                r = alt_index.get(frozenset({a_alt, b_alt}))
        if not r:
            continue
        win_name = r["player_a_name"] if r["winner"] == "a" else r["player_b_name"]
        wk = _rkey(win_name)
        # Compare on the trimmed form too, so a middle-name key still maps the
        # winner onto the right side. winner_key is always stored as this app's
        # OWN key, never the source's, so downstream lookups stay consistent.
        if wk == m.player_a_key or wk == (_surname_key(m.player_a_key) or m.player_a_key):
            m.winner_key = m.player_a_key
        elif wk == m.player_b_key or wk == (_surname_key(m.player_b_key) or m.player_b_key):
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

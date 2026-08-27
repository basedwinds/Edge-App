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
    for pair in index:  # values are now LISTS of dated results
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
    # {pair -> [result, ...]}, each result STAMPED WITH THE DAY IT WAS PLAYED.
    #
    # REAL BUG this fixes (user-reported 2026-08-27). This used to be
    # `index[pair] = r`, keyed on the player pair ALONE and overwriting, so:
    #
    #   * the play date -- known right here as `d` -- was thrown away, and
    #   * when a pair met twice inside the fetched window, one result silently
    #     replaced the other.
    #
    # `dates` spans +/-1 day around EVERY ungraded fixture within
    # _LOOKBACK_DAYS, so older dates are fetched to resolve other matches. A
    # Harris/Ruiz result from 2026-08-20 therefore entered the index and was
    # applied to their UNPLAYED 2026-08-27 fixture, which was in a rain delay.
    # apply_results_index stores the score in the fixture's own player order, so
    # it arrived mirrored -- "6-4 4-3" became "4-6 3-4" -- and a REAL $10 bet
    # was settled as lost on a match that had not been played.
    #
    # Keeping every result WITH its date lets the write step demand that the
    # date agree with the fixture before touching it.
    index: dict[frozenset, list[dict]] = {}
    for d in sorted(dates):
        try:
            rows = cli.get_results_day(d.year, d.month, d.day)
        except Exception:
            continue  # tennisexplorer is flaky; skip the date, retried next cycle
        for r in rows:
            if r.get("winner"):
                r = dict(r)
                r["_result_date"] = d.isoformat()
                key = frozenset({_rkey(r["player_a_name"]), _rkey(r["player_b_name"])})
                index.setdefault(key, []).append(r)
    return index


# How far a result's play date may sit from the fixture's own and still be
# accepted as that fixture's result. ONE day, for the UTC boundary that
# fetch_results_index already fetches around -- not a tolerance for "near
# enough", which is what let a week-old result through.
_RESULT_DATE_TOLERANCE_DAYS = 1


def _result_for_fixture(candidates, fixture_day):
    """The one result whose play date matches this fixture, or None.

    Returns None when nothing matches AND when more than one does: two results
    for the same pair within a day of each other cannot be told apart, and
    guessing between them is exactly the failure this exists to prevent.
    """
    if not candidates or not fixture_day:
        return None
    try:
        want = datetime.date.fromisoformat(fixture_day[:10])
    except ValueError:
        return None
    hits = []
    for r in candidates:
        rd = r.get("_result_date")
        if not rd:
            continue
        try:
            got = datetime.date.fromisoformat(rd)
        except ValueError:
            continue
        if abs((got - want).days) <= _RESULT_DATE_TOLERANCE_DAYS:
            hits.append(r)
    return hits[0] if len(hits) == 1 else None


def apply_results_index(session: Session, index: dict) -> int:
    """Write step (call UNDER the write lock): set winner_key + score on finished
    rows from an already-fetched index. Returns the number newly resolved."""
    if not index:
        return 0
    alt_index = _build_alt_index(index)
    resolved = 0
    skipped_date = 0
    for m in _finished_ungraded(session):
        # THE FIXTURE'S OWN DAY decides which result may be written to it. Start
        # time first, then match_date -- the same order every other tennis path
        # uses.
        fixture_day = ((m.estimated_start_time or m.match_date) or "")[:10]
        cands = index.get(frozenset({m.player_a_key, m.player_b_key}))
        r = _result_for_fixture(cands, fixture_day) if cands else None
        if not r:
            # Fallback: same match with a middle name trimmed off either side.
            a_alt = _surname_key(m.player_a_key) or m.player_a_key
            b_alt = _surname_key(m.player_b_key) or m.player_b_key
            if a_alt != b_alt:
                alt = alt_index.get(frozenset({a_alt, b_alt}))
                r = _result_for_fixture(alt, fixture_day) if alt else None
        if not r:
            if cands:
                # A result exists for this PAIR but not for this DAY -- exactly
                # the shape that settled an unplayed match. Leave it pending.
                skipped_date += 1
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
    if skipped_date:
        log.info("tennis results backfill: %d fixture(s) had a result for the pair "
                 "but not for their DAY -- left pending rather than cross-applied",
                 skipped_date)
    return resolved

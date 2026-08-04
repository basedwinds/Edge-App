"""Joins gol.gg series results onto LolMatch rows so LoL bets can settle.

Deliberately NOT reusing lol_results.apply_lol_results, for two reasons that
both cause wrong grades rather than missed ones:

  1. That function indexes results by team-pair ALONE, with no date. Safe for
     Leaguepedia, which it only ever feeds a 12-day window; unsafe here, where
     the gol.gg cache spans ~12 months, because two teams meeting twice would
     let an OLD meeting grade a NEW match. Every key here carries the date.
  2. It only considers matches inside a 10-day lookback. The backlog this exists
     to clear is older than that, so a 10-day window would silently skip most of
     it while reporting success.

DATE_TOLERANCE_DAYS exists because the two sides date a match differently:
Kalshi's date comes from the market ticker (UTC-ish) while gol.gg's comes from
the game page, so an evening match in Asia or the Americas can land a day apart.
It is applied ONLY when the pair has exactly one candidate in the window -- if
two meetings are both in range there is no way to tell which is which, so the
row is left unsettled rather than graded on a coin flip.
"""
from __future__ import annotations

import datetime
import logging
import re

from sqlalchemy.orm import Session

from app.db.models import LolMatch

log = logging.getLogger("lol_results_golgg")

DATE_TOLERANCE_DAYS = 1


def _norm(x: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", x.lower()) if x else ""


def _pair(a: str | None, b: str | None) -> tuple[str, str]:
    return tuple(sorted((_norm(a), _norm(b))))  # type: ignore[return-value]


def _resolved_pair(a: str | None, b: str | None, index) -> tuple[str, str] | None:
    """The pair key under gol.gg's OWN spelling of both teams, or None.

    Requires BOTH names to resolve. A half-resolved pair is not a weaker match,
    it is a different fixture -- so it is refused rather than fallen back on.
    """
    from app.ingestion.lol_team_aliases import resolve

    ra, rb = resolve(a, index), resolve(b, index)
    if ra is None or rb is None:
        return None
    return _pair(ra, rb)


def _day(value: str | None) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def apply_golgg_results(session: Session, rows: list[dict],
                        tolerance_days: int = DATE_TOLERANCE_DAYS) -> int:
    """Fill winner + maps_won_a/b on ungraded, already-started LolMatch rows.

    Returns the number newly resolved. Orients the map score to each row's own
    team_a/team_b order, since gol.gg's pair order is its own.
    """
    if not rows:
        return 0

    from app.ingestion.lol_team_aliases import build_alias_index

    by_pair: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        if not r.get("winner"):
            continue
        by_pair.setdefault(_pair(r.get("team_a"), r.get("team_b")), []).append(r)

    # Second lookup for names the two sources merely SPELL differently
    # ("INTZ e-Sports" vs "INTZ"). Built from gol.gg's own names, so the pair
    # key it produces is directly comparable to by_pair above. See
    # lol_team_aliases.py for why sub-rosters are excluded by construction.
    alias_index = build_alias_index(
        {n for r in rows for n in (r.get("team_a"), r.get("team_b")) if n}
    )

    today = datetime.date.today()
    pending = [
        m for m in session.query(LolMatch).filter(LolMatch.winner.is_(None)).all()
        if (_day(m.match_date) or today) < today
    ]

    resolved = 0
    ambiguous = 0
    for m in pending:
        target = _day(m.match_date)
        if target is None:
            continue
        # Identical spelling first, so the common case never depends on the
        # alias rules; only fall back to gol.gg's own spelling of both teams.
        candidates = by_pair.get(_pair(m.team_a, m.team_b))
        if not candidates:
            alias_pair = _resolved_pair(m.team_a, m.team_b, alias_index)
            candidates = by_pair.get(alias_pair) if alias_pair else None
        if not candidates:
            continue

        exact = [r for r in candidates if _day(r.get("match_date")) == target]
        if exact:
            picked = exact[0]
        else:
            near = [r for r in candidates
                    if (d := _day(r.get("match_date"))) is not None
                    and abs((d - target).days) <= tolerance_days]
            if len(near) != 1:
                # 0 -> gol.gg has no meeting near this date (usually just its
                # ~6-day publishing lag). >1 -> genuinely undecidable; never guess.
                ambiguous += len(near) > 1
                continue
            picked = near[0]

        # Orientation must compare the SAME spelling on both sides. Once an
        # alias match is possible, `m.team_a` ("INTZ e-Sports") no longer equals
        # the result's ("INTZ"), and a raw comparison would read every aliased
        # row as reversed -- silently recording the losing team as the winner.
        # So resolve this row's own name through the same index before comparing,
        # falling back to the raw name when it already matches exactly.
        from app.ingestion.lol_team_aliases import resolve as _resolve_name

        own_a = _resolve_name(m.team_a, alias_index) or m.team_a
        same_order = _norm(picked.get("team_a")) in (_norm(m.team_a), _norm(own_a))
        m.maps_won_a = picked.get("maps_won_a") if same_order else picked.get("maps_won_b")
        m.maps_won_b = picked.get("maps_won_b") if same_order else picked.get("maps_won_a")
        win = picked.get("winner")  # "team_a"/"team_b" in the RESULT's order
        m.winner = win if same_order else ("team_b" if win == "team_a" else "team_a")
        resolved += 1

    if resolved:
        session.commit()
        log.info("gol.gg results backfill: resolved %d matches (%d left ambiguous)",
                 resolved, ambiguous)
    return resolved

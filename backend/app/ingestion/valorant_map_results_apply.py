"""Writes vlr.gg per-map results onto ValorantMap rows.

Split from the fetch (valorant_map_results.py) for the reason every poller here
splits them: the network work must happen outside the DB write lock.

The check that makes this safe is in `_oriented_rows`: the per-map tally derived
from the match page must equal the maps_won_a/maps_won_b already stored for that
series from vlr.gg's LIST page. Those are two independent reads of the same
result, so agreement is real evidence that the maps were parsed in the right
order and oriented to the right team. On any disagreement the match is skipped
entirely -- a pending map bet costs nothing, a wrongly-graded one pays the wrong
side.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.db.models import ValorantMap, ValorantMatch

log = logging.getLogger("valorant_map_results")


def _norm(x: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", x.lower()) if x else ""


def matches_needing_maps(session: Session) -> list[ValorantMatch]:
    """Settled vlr-sourced matches that have no per-map rows yet."""
    have = {mid for (mid,) in session.query(ValorantMap.valorant_match_id).distinct().all()}
    return [
        m for m in session.query(ValorantMatch)
        .filter(ValorantMatch.winner.isnot(None), ValorantMatch.source == "vlr").all()
        if m.id not in have and m.source_match_id
    ]


def _oriented_rows(match: ValorantMatch, rows: list[dict]) -> list[tuple[int, str]] | None:
    """[(map_number, "team_a"/"team_b")] oriented to THIS row, or None to skip."""
    a, b = _norm(match.team_a), _norm(match.team_b)
    out: list[tuple[int, str]] = []
    wins = {"team_a": 0, "team_b": 0}
    for r in rows:
        na, nb = _norm(r["team_a_name"]), _norm(r["team_b_name"])
        if {na, nb} != {a, b}:
            return None  # the page is not about this fixture
        a_is_first = na == a
        a_score = r["score_a"] if a_is_first else r["score_b"]
        b_score = r["score_b"] if a_is_first else r["score_a"]
        side = "team_a" if a_score > b_score else "team_b"
        wins[side] += 1
        out.append((r["map_number"], side))

    # The independent cross-check. Both numbers describe the same series but come
    # from different pages, so requiring equality catches a mis-order, a missed
    # map and a flipped orientation alike.
    if match.maps_won_a is None or match.maps_won_b is None:
        return None
    if (wins["team_a"], wins["team_b"]) != (match.maps_won_a, match.maps_won_b):
        return None
    return out


def apply_valorant_map_results(session: Session, by_match: dict) -> int:
    """Write per-map winners. Returns the number of matches given map rows."""
    if not by_match:
        return 0
    from app.ingestion.market_catalog_valorant import upsert_valorant_map

    written = skipped = 0
    for match_id, rows in by_match.items():
        match = session.get(ValorantMatch, match_id)
        if match is None:
            continue
        oriented = _oriented_rows(match, rows)
        if oriented is None:
            skipped += 1
            continue
        for map_number, side in oriented:
            upsert_valorant_map(session, valorant_match_id=match.id,
                                map_number=map_number, winner=side)
        written += 1

    if written or skipped:
        session.commit()
        log.info("valorant map backfill: %d matches written, %d skipped on the "
                 "series-tally cross-check", written, skipped)
    return written

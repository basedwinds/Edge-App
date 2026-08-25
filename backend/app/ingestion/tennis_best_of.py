"""Derives a tennis match's FORMAT (best-of-3 vs best-of-5) from the book's own
market inventory, because no metadata field carries it.

WHY THIS EXISTS. best_of drives every tennis model -- the moneyline logistic, the
set/game spreads, and above all total games, where Bo3 expects ~22 and Bo5 ~38.
Getting it wrong by one format is a ~16-game error, which turns every total line
into a screaming OVER.

AND IT WAS BEING GUESSED FROM A TOURNAMENT NAME. Kalshi labels a men's Slam
QUALIFYING match "US Open Men Singles" -- byte-identical to the main draw -- and
its structured fields agree: sub_title is just the players, and
product_metadata.competition is the same string. Confirmed live 2026-08-25 across
the whole US Open qualifying board. So the old `"qualif" in tournament_text` test
could never fire, all 80 live-created men's rows were flagged Bo5, and 36
game_total bets were staked against tight liquid books at +48 to +72pp of
entirely fictional edge.

THE INVENTORY IS CONSTRAINED BY THE FORMAT, so the book tells us for free:

    total_sets   line 2.5 can only exist in a Bo3 (max 3 sets); Bo5 uses 3.5/4.5
    exact_score  sides run 2-0/2-1 in a Bo3 and 3-0/3-1/3-2 in a Bo5
    set_winner   a market for set 4 or 5 can only exist in a Bo5
    game_total   a Bo5 cannot end under 18 games, so a line below that is a
                 certainty no book would list

Measured coverage on the 99 US Open matches carrying live markets: total_sets
decides 95, min-game-line 73, exact_score 47 -- and every one of them said Bo3,
which is correct. This is better evidence than any name-based rule because it
comes from the counterparty that settles the contract.

AMBIGUITY REFUSES. If two signals disagree the match is left ALONE rather than
guessed at, and the disagreement is reported. That matters because a wrong
best_of is worse than an unknown one: unknown falls back to 3, which is right for
WTA, Challenger/ITF, all Slam qualifying and most ATP tour matches, whereas wrong
routes into TOTAL_GAMES_PARAMS[5] -- a fit that has NEVER been validated (under
40 settled Bo5 matches exist to check it against).
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.db.models import Market, TennisMatch

log = logging.getLogger(__name__)

# Market types whose presence/shape constrains the format.
_RELEVANT = ("total_sets", "exact_score", "set_winner", "game_total")


def _digits(value) -> int | None:
    d = "".join(ch for ch in str(value or "") if ch.isdigit())
    return int(d) if d else None


def infer_best_of(rows) -> tuple[int | None, dict]:
    """(best_of, per-signal votes) from one match's markets. None = undecided."""
    votes: dict[str, int] = {}

    # total_sets: the line IS the set threshold.
    ts = [r.line for r in rows if r.market_type == "total_sets" and r.line is not None]
    if ts:
        votes["total_sets"] = 5 if max(ts) >= 3.5 else 3

    # exact_score: the side is "<sets>-<sets>", winner's count first.
    es = [str(r.side) for r in rows if r.market_type == "exact_score" and r.side]
    if es:
        votes["exact_score"] = 5 if any(x.strip().startswith("3") for x in es) else 3

    # set_winner: the LINE is the set number. NOT set_total, whose line is a
    # games threshold (8.5/9.5) and would read as "set 9" -- that exact mistake
    # made a first pass claim Bo5 for 95 of 99 matches.
    sw = [r.line for r in rows if r.market_type == "set_winner" and r.line is not None]
    if sw:
        votes["set_winner"] = 5 if max(sw) >= 4 else 3

    # game_total: a Bo5 cannot finish under 18 games. Only ever votes Bo3 --
    # a high minimum line proves nothing, since a book can list a high ladder on
    # a Bo3 too.
    gt = [r.line for r in rows if r.market_type == "game_total" and r.line is not None]
    if gt and min(gt) < 18:
        votes["min_game_line"] = 3

    distinct = set(votes.values())
    if len(distinct) == 1:
        return distinct.pop(), votes
    return None, votes


def refresh_best_of(session: Session) -> dict:
    """Set best_of on tennis matches where the book's inventory decides it.

    Only writes when the answer CHANGES, so a steady state costs one query pass.
    """
    linked = (session.query(Market.tennis_match_id)
              .filter(Market.status == "active")
              .filter(Market.tennis_match_id.isnot(None))
              .filter(Market.market_type.in_(_RELEVANT))
              .distinct().all())
    ids = [x[0] for x in linked]
    if not ids:
        return {"considered": 0, "decided": 0, "changed": 0, "conflicted": 0}

    rows = (session.query(Market)
            .filter(Market.status == "active")
            .filter(Market.tennis_match_id.in_(ids))
            .filter(Market.market_type.in_(_RELEVANT)).all())
    by_match: dict[int, list] = {}
    for r in rows:
        by_match.setdefault(r.tennis_match_id, []).append(r)

    matches = {m.id: m for m in session.query(TennisMatch)
               .filter(TennisMatch.id.in_(ids)).all()}

    decided = changed = conflicted = 0
    for mid, mrows in by_match.items():
        match = matches.get(mid)
        if match is None:
            continue
        bo, votes = infer_best_of(mrows)
        if bo is None:
            if len(votes) > 1:
                conflicted += 1
                log.info("tennis best_of undecided for match %s: %s", mid, votes)
            continue
        decided += 1
        if match.best_of != bo:
            log.info("tennis best_of %s -> %s for match %s (%s)",
                     match.best_of, bo, mid, votes)
            match.best_of = bo
            changed += 1
    session.commit()

    result = {"considered": len(by_match), "decided": decided,
              "changed": changed, "conflicted": conflicted}
    log.info("tennis best_of: %s", result)
    return result

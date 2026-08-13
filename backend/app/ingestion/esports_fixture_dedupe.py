"""Merge esports fixture rows that are the SAME real match stored twice.

THE CAUSE, shared by every title. Each `market_catalog_<title>` builds its dedupe
candidate pool as `<Model>.winner.is_(None)` -- it can only see UNPLAYED
fixtures. So once the result lands, the next platform listing of that match
cannot find it and inserts a second row, keyed by a `source_match_id` built from
the team names, so the unique constraint never fires. The name matchers DO try
both A/B orderings; the winner filter is what makes the row invisible.

Measured 2026-08-13, duplicate groups keyed on (estimated_start_time, unordered
team pair), and of those the ones where one twin carries the result and the
other is stuck at winner=None forever:

    cs2       168 groups / 165 split   (merged 8d60c74)
    valorant   50 groups /  49 split
    lol       118 groups /  90 split
    cod         0 groups            -- no damage

WHY MERGE RATHER THAN LOOSEN THE LOOKUP. Letting the lookup match a PLAYED
fixture would let a new match bind to an old one inside the +/-2 day rematch
window and settle its bets off the wrong match's result -- the bug
market_catalog_lol.py's own comment describes, and strictly worse than a
duplicate. Merging on an EXACT start-time match cannot make that mistake: two
matches between the same pair at the identical timestamp are the same match, and
a genuine rematch has a different start time. Rows with no estimated_start_time
are never merged, because without a timestamp there is no proof of identity.
"""
from __future__ import annotations

import collections
import logging
import unicodedata

from sqlalchemy.orm import Session

from app.db import models as M
from app.db.models import Market, ModelObservation, PlacedBet

log = logging.getLogger("esports_fixture_dedupe")

# sport -> (fixture model, per-map model or None, the *_match_id column name)
SPORTS: dict[str, tuple] = {
    "cs2": (M.Cs2Match, M.Cs2Map, "cs2_match_id"),
    "valorant": (M.ValorantMatch, M.ValorantMap, "valorant_match_id"),
    "lol": (M.LolMatch, M.LolMap, "lol_match_id"),
}
if hasattr(M, "CodMatch"):
    SPORTS["cod"] = (M.CodMatch, getattr(M, "CodMap", None), "cod_match_id")

# Order-INDEPENDENT fields, safe to copy straight across.
_FILL_FIELDS = ("event_name", "best_of", "match_date", "estimated_start_time", "start_time_source")


def _fold(name: str | None) -> str:
    """Accent- and case-insensitive team key. Only ever used to decide that two
    ROWS describe one match -- never to decide two different teams are one."""
    s = unicodedata.normalize("NFKD", (name or "").strip().lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _pair_key(m):
    st = (getattr(m, "estimated_start_time", None) or "").strip()
    if not st:
        return None
    return (st, frozenset({_fold(m.team_a), _fold(m.team_b)}))


def _teams_are_reversed(survivor, loser) -> bool:
    """True when the loser lists the same two teams the other way round, so its
    team-indexed fields (winner, maps_won_a/b) must be FLIPPED before copying.
    Getting this wrong records the WRONG WINNER, which is worse than leaving the
    duplicate in place -- so it is checked explicitly rather than assumed."""
    return _fold(loser.team_a) != _fold(survivor.team_a)


def find_duplicate_groups(session: Session, sport: str) -> list[list]:
    model = SPORTS[sport][0]
    groups: dict = collections.defaultdict(list)
    for m in session.query(model).all():
        key = _pair_key(m)
        if key is not None:
            groups[key].append(m)
    return [sorted(v, key=lambda r: r.id) for v in groups.values() if len(v) > 1]


def _pick_survivor(group: list):
    """The row carrying a result wins -- it is the one the results feed keeps
    updating, so keeping it means future results keep landing. Ties break to the
    lowest id (the original)."""
    resolved = [m for m in group if getattr(m, "winner", None) is not None]
    return min(resolved or group, key=lambda r: r.id)


def merge_duplicate_fixtures(session: Session, sport: str, dry_run: bool = True) -> dict:
    """Merge one sport's duplicate fixtures. With dry_run=True this MUTATES
    NOTHING and only reports -- deliberately not a rollback-wrapped commit, see
    the incident where a "dry run" of an internally-committing function settled
    123 live bets for real."""
    model, map_model, fk = SPORTS[sport]
    groups = find_duplicate_groups(session, sport)
    plan = {"sport": sport, "groups": len(groups), "merged": 0, "bets_repointed": 0,
            "markets_repointed": 0, "maps_repointed": 0, "maps_dropped": 0,
            "obs_repointed": 0, "results_recovered": 0, "rows_deleted": 0, "examples": []}

    for group in groups:
        survivor = _pick_survivor(group)
        losers = [m for m in group if m.id != survivor.id]
        if not losers:
            continue
        # Map numbers already claimed on the survivor, tracked ACROSS every loser
        # in the group. REAL BUG this fixes (hit on the first live LoL run):
        # computing it per-loser re-read the DB, which does not yet reflect the
        # previous loser's re-points because nothing has flushed -- so in a
        # three-row group both losers' map 1 were pointed at the survivor and the
        # commit died on "UNIQUE constraint failed: lol_maps.lol_match_id,
        # lol_maps.map_number". Groups of two never exposed it.
        taken: set = set()
        if map_model is not None:
            taken = {r.map_number for r in
                     session.query(map_model).filter(getattr(map_model, fk) == survivor.id).all()}
        for loser in losers:
            flip = _teams_are_reversed(survivor, loser)
            if getattr(survivor, "winner", None) is None and getattr(loser, "winner", None) is not None:
                w = loser.winner
                a, b = getattr(loser, "maps_won_a", None), getattr(loser, "maps_won_b", None)
                if not dry_run:
                    survivor.winner = ("team_b" if w == "team_a" else "team_a") if flip else w
                    survivor.maps_won_a, survivor.maps_won_b = (b, a) if flip else (a, b)
                plan["results_recovered"] += 1
            for f in _FILL_FIELDS:
                if getattr(survivor, f, None) in (None, "") and getattr(loser, f, None) not in (None, ""):
                    if not dry_run:
                        setattr(survivor, f, getattr(loser, f))

            bets = session.query(PlacedBet).filter(getattr(PlacedBet, fk) == loser.id).all()
            mkts = session.query(Market).filter(getattr(Market, fk) == loser.id).all()
            obs = session.query(ModelObservation).filter(
                getattr(ModelObservation, fk) == str(loser.id)).all()
            plan["bets_repointed"] += len(bets)
            plan["markets_repointed"] += len(mkts)
            plan["obs_repointed"] += len(obs)
            if not dry_run:
                for r in bets:
                    setattr(r, fk, survivor.id)
                for r in mkts:
                    setattr(r, fk, survivor.id)
                for r in obs:
                    setattr(r, fk, str(survivor.id))

            # The per-map tables are UNIQUE on (<fk>, map_number), so a re-point
            # can collide. The survivor's own map row is authoritative; drop the
            # duplicate rather than violate the constraint.
            if map_model is not None:
                for r in session.query(map_model).filter(getattr(map_model, fk) == loser.id).all():
                    if r.map_number in taken:
                        plan["maps_dropped"] += 1
                        if not dry_run:
                            session.delete(r)
                    else:
                        taken.add(r.map_number)
                        plan["maps_repointed"] += 1
                        if not dry_run:
                            setattr(r, fk, survivor.id)

            plan["rows_deleted"] += 1
            if not dry_run:
                session.delete(loser)
        plan["merged"] += 1
        if len(plan["examples"]) < 5:
            plan["examples"].append(
                f"keep {survivor.id} ({survivor.team_a} vs {survivor.team_b}, "
                f"winner={getattr(survivor,'winner',None)!r}) <- drop {[l.id for l in losers]}")

    if not dry_run and plan["rows_deleted"]:
        session.commit()
        log.info("%s dedupe: merged %d groups, deleted %d rows, re-pointed %d bets / %d markets",
                 sport, plan["merged"], plan["rows_deleted"],
                 plan["bets_repointed"], plan["markets_repointed"])
    return plan

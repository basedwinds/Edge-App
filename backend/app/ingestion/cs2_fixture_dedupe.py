"""Merge cs2_matches rows that are the SAME real match stored twice.

THE CAUSE, not the symptom. `market_catalog_cs2._load_upcoming_matches` builds
the dedupe candidate pool as `Cs2Match.winner.is_(None)` -- it can only see
UNPLAYED fixtures. So when the results feed writes a winner first, the Kalshi
listing path can no longer find that fixture and inserts a second row under its
own team ordering. The A/B reversal is incidental (the two feeds just order
differently); the `winner IS NULL` filter is what makes the row invisible.

Measured 2026-08-13: 168 duplicate groups keyed on (start, unordered pair), 165
of them with one twin resolved and one stuck at winner=None forever, because the
results feed keeps writing to the row it already knows.

WHY MERGE RATHER THAN LOOSEN THE LOOKUP. Dropping the `winner IS NULL` filter
would let a NEW fixture bind to an already-played one inside the +/-2 day rematch
window, and settle its bets off the wrong match's result. That is the bug
market_catalog_lol.py's comment describes and it is strictly worse than a
duplicate. Merging on an EXACT start-time match cannot make that mistake: two
matches between the same pair at the identical timestamp are the same match, and
a genuine rematch has a different start time.

Rows with no estimated_start_time are never merged -- without a timestamp there
is no proof the two are the same fixture.
"""
from __future__ import annotations

import collections
import logging
import unicodedata

from sqlalchemy.orm import Session

from app.db.models import Cs2Map, Cs2Match, Market, ModelObservation, PlacedBet

log = logging.getLogger("cs2_fixture_dedupe")

# Fields copied from a loser onto the survivor when the survivor lacks them.
# maps_won_a/b and winner are handled separately -- they are ORDER-DEPENDENT.
_FILL_FIELDS = ("event_name", "best_of", "match_date", "estimated_start_time", "start_time_source")


def _fold(name: str | None) -> str:
    """Accent- and case-insensitive team key. Gremio and Grêmio are the same
    team; this is only used to decide that two ROWS describe one match, never to
    decide that two different teams are one team."""
    s = unicodedata.normalize("NFKD", (name or "").strip().lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _pair_key(m: Cs2Match):
    st = (m.estimated_start_time or "").strip()
    if not st:
        return None
    return (st, frozenset({_fold(m.team_a), _fold(m.team_b)}))


def _teams_are_reversed(survivor: Cs2Match, loser: Cs2Match) -> bool:
    """True when the loser lists the same two teams the other way round, so its
    team-indexed fields (winner, maps_won_a/b) must be FLIPPED before being
    copied. Getting this wrong would record the wrong winner, which is worse
    than leaving the duplicate in place."""
    return _fold(loser.team_a) != _fold(survivor.team_a)


def find_duplicate_groups(session: Session) -> list[list[Cs2Match]]:
    groups: dict = collections.defaultdict(list)
    for m in session.query(Cs2Match).all():
        key = _pair_key(m)
        if key is not None:
            groups[key].append(m)
    return [sorted(v, key=lambda r: r.id) for v in groups.values() if len(v) > 1]


def _pick_survivor(group: list[Cs2Match]) -> Cs2Match:
    """The row carrying a result wins -- it is the one the results feed keeps
    updating, so keeping it means future results keep landing. Ties break to the
    lowest id (the original)."""
    resolved = [m for m in group if m.winner is not None]
    return min(resolved or group, key=lambda r: r.id)


def merge_duplicate_cs2_fixtures(session: Session, dry_run: bool = True) -> dict:
    """Merge duplicate fixtures. With dry_run=True this MUTATES NOTHING and just
    reports what it would do -- deliberately not a rollback-wrapped commit, see
    the 123-bets-settled-by-a-dry-run incident."""
    groups = find_duplicate_groups(session)
    plan = {"groups": len(groups), "merged": 0, "bets_repointed": 0,
            "markets_repointed": 0, "maps_repointed": 0, "maps_dropped": 0,
            "obs_repointed": 0, "results_recovered": 0, "rows_deleted": 0, "examples": []}

    for group in groups:
        survivor = _pick_survivor(group)
        losers = [m for m in group if m.id != survivor.id]
        if not losers:
            continue
        for loser in losers:
            flip = _teams_are_reversed(survivor, loser)
            # RESULT RECOVERY. If only the loser knows the outcome, carry it over
            # -- flipped when the row is reversed, or we would record the wrong
            # team as the winner.
            if survivor.winner is None and loser.winner is not None:
                w = loser.winner
                survivor_winner = ("team_b" if w == "team_a" else "team_a") if flip else w
                a, b = loser.maps_won_a, loser.maps_won_b
                if not dry_run:
                    survivor.winner = survivor_winner
                    survivor.maps_won_a, survivor.maps_won_b = (b, a) if flip else (a, b)
                plan["results_recovered"] += 1
            for f in _FILL_FIELDS:
                if getattr(survivor, f, None) in (None, "") and getattr(loser, f, None) not in (None, ""):
                    if not dry_run:
                        setattr(survivor, f, getattr(loser, f))

            bets = session.query(PlacedBet).filter(PlacedBet.cs2_match_id == loser.id).all()
            mkts = session.query(Market).filter(Market.cs2_match_id == loser.id).all()
            obs = session.query(ModelObservation).filter(
                ModelObservation.cs2_match_id == str(loser.id)).all()
            plan["bets_repointed"] += len(bets)
            plan["markets_repointed"] += len(mkts)
            plan["obs_repointed"] += len(obs)
            if not dry_run:
                for r in bets:
                    r.cs2_match_id = survivor.id
                for r in mkts:
                    r.cs2_match_id = survivor.id
                for r in obs:
                    r.cs2_match_id = str(survivor.id)

            # Cs2Map is UNIQUE on (cs2_match_id, map_number), so a re-point can
            # collide. The survivor's own map row is authoritative; drop the
            # duplicate rather than violate the constraint.
            taken = {r.map_number for r in
                     session.query(Cs2Map).filter(Cs2Map.cs2_match_id == survivor.id).all()}
            for r in session.query(Cs2Map).filter(Cs2Map.cs2_match_id == loser.id).all():
                if r.map_number in taken:
                    plan["maps_dropped"] += 1
                    if not dry_run:
                        session.delete(r)
                else:
                    taken.add(r.map_number)
                    plan["maps_repointed"] += 1
                    if not dry_run:
                        r.cs2_match_id = survivor.id

            plan["rows_deleted"] += 1
            if not dry_run:
                session.delete(loser)
        plan["merged"] += 1
        if len(plan["examples"]) < 8:
            plan["examples"].append(
                f"keep {survivor.id} ({survivor.team_a} vs {survivor.team_b}, winner={survivor.winner!r}) "
                f"<- drop {[l.id for l in losers]}")

    if not dry_run and plan["rows_deleted"]:
        session.commit()
        log.info("cs2 dedupe: merged %d groups, deleted %d rows, re-pointed %d bets / %d markets",
                 plan["merged"], plan["rows_deleted"], plan["bets_repointed"], plan["markets_repointed"])
    return plan

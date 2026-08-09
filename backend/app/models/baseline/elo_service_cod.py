"""Call of Duty rating service -- the stateful layer over elo_cod.py.

Mirrors elo_service_cs2/valorant/lol: load the historical crawl plus the live
table, replay everything in real chronological order, cache the resulting state,
and expose lookups the router prices from.

DELIBERATELY THINNER than the CS2 service, and the omissions are the point:

  * No player/lineup blend -- breakingpoint.gg carries no per-match rosters.
  * No transfer-aware K -- there is no CoD transfer archive here.
  * No map-pool ratings -- Kalshi lists no CoD map markets.

Each of those exists for CS2 because CS2 has the data. Adding them here would
mean inventing structure the source does not contain, which is how a model
starts fitting its own assumptions.

WHAT IS KEPT, because it was validated on other titles and needs no extra data:
a head-to-head blend and the MIN_GAMES floor. Both operate purely on the match
list this service already replays.

WALK-FORWARD SAFETY. refresh_ratings() sorts by real start time and scores each
match BEFORE updating from it (see elo_cod.predict_and_update), so no match ever
informs its own prediction. That is the property scripts/check_cod_walkforward.py
measured at 0.6479 accuracy over 2,508 predictions.

RATINGS ARE NOT AN EDGE. The model is shipped model_validated: false and has NOT
been backtested against real CoD market odds. Predicting better than a coin flip
is a different claim from beating a market price.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app.db.database import SessionLocal
from app.db.models import CodMatch
from app.models.baseline.elo_cod import (
    CodEloState, SeriesDistribution, map_p_for_series_prob, predict_and_update,
    predict_series, series_p_from_map_p, series_score_distribution,
)

log = logging.getLogger("elo_service_cod")

HISTORICAL_CACHE_PATH = (Path(__file__).resolve().parent.parent.parent.parent.parent
                         / "data" / "cod_historical_match_cache.json")

# Both teams need this many real settled series before a rating is trusted.
# 3 is the value the other esports titles landed on from real Brier-by-bucket
# data. Not independently re-fitted for CoD: with 3,614 matches the warm-up
# effect is the same shape, and inventing a different number without measuring
# it would be worse than reusing a measured one. Revisit if a CoD-specific
# bucket analysis is ever run.
MIN_GAMES = 3

_cache: dict = {}


def _load_historical_matches() -> list[dict]:
    if not HISTORICAL_CACHE_PATH.exists():
        return []
    try:
        rows = json.loads(HISTORICAL_CACHE_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        log.exception("cod historical cache unreadable")
        return []
    out = []
    for r in rows:
        team_a, team_b = r.get("team_a"), r.get("team_b")
        if not team_a or not team_b:
            continue
        # The crawl stores the winner as a NAME; the Elo layer wants the side.
        winner_name = r.get("winner")
        winner = "team_a" if winner_name == team_a else "team_b" if winner_name == team_b else None
        out.append({
            "source_match_id": r["source_match_id"],
            "team_a": team_a, "team_b": team_b,
            "best_of": r.get("best_of"), "winner": winner,
            "match_date": r.get("match_date"),
            "sort_key": r.get("datetime") or r.get("match_date") or "",
        })
    return out


def _load_live_matches(session) -> list[dict]:
    return [
        {
            "source_match_id": r.source_match_id, "team_a": r.team_a, "team_b": r.team_b,
            "best_of": r.best_of, "winner": r.winner,
            "match_date": r.match_date,
            "sort_key": r.estimated_start_time or r.match_date or "",
        }
        for r in session.query(CodMatch).all()
    ]


def refresh_ratings():
    """Rebuild the rating state from scratch. Takes no session argument -- same
    contract as the other esports services, which the poller relies on."""
    session = SessionLocal()
    try:
        live_matches = _load_live_matches(session)
    finally:
        session.close()
    historical_matches = _load_historical_matches()

    # Merge BEFORE sorting, not one source then the other: the live table can
    # hold matches that predate some historical rows, and replaying the two as
    # separate passes would score them out of real chronological order.
    by_id: dict[str, dict] = {}
    for m in historical_matches:
        by_id[m["source_match_id"]] = m
    for m in live_matches:
        by_id[m["source_match_id"]] = m
    all_matches = sorted(by_id.values(), key=lambda m: (m["sort_key"], m["source_match_id"]))

    state = CodEloState()
    rated = 0
    last_played_date: dict[str, str] = {}
    for m in all_matches:
        if predict_and_update(state, m) is not None and m["winner"] is not None:
            rated += 1
        d = m.get("match_date")
        if d and m["winner"] is not None:
            # Only SETTLED matches count as "played" -- all_matches includes
            # scheduled rows, and treating a future match as already played
            # would corrupt any recency logic built on this later.
            last_played_date[m["team_a"]] = d
            last_played_date[m["team_b"]] = d

    _cache["state"] = state
    _cache["last_played_date"] = last_played_date
    log.info(
        "cod elo ratings refreshed: %d teams rated, %d settled series scored "
        "(%d historical + %d live, deduped to %d)",
        len(state.ratings), rated, len(historical_matches), len(live_matches), len(all_matches),
    )


def resolve_team_name(team: str) -> str:
    """No-op resolver, kept so callers match the other titles' interface.

    CS2 and Valorant need real name resolution because their sources use short
    display names and shortcodes that split one team's history across spellings.
    breakingpoint.gg returns the SAME full names the markets use ("OpTic
    Gaming", "Team Falcons"), so there is nothing to resolve -- and a fuzzy
    resolver with nothing to fix is a way to introduce a wrong merge, not a
    safeguard. If a real spelling split ever shows up, fix it here.
    """
    return team


def get_team_rating(team: str) -> float | None:
    """None -- never a fabricated 1500 -- when the team has no real history.

    Returning BASE_RATING on a miss is the exact bug this app has already been
    bitten by: a bracket seeded off it entered the field's strongest teams at
    mid-table. Callers must handle None."""
    state = _cache.get("state")
    if state is None:
        return None
    resolved = resolve_team_name(team)
    if resolved not in state.ratings:
        return None
    return state.ratings[resolved]


def get_team_games(team: str) -> int:
    state = _cache.get("state")
    if state is None:
        return 0
    return state.games_played(resolve_team_name(team))


# Weight on the head-to-head record, matching the other titles: a real prior
# meeting is informative but a 2-0 record over two games is not proof, so the
# blend is capped and shrinks toward the Elo prior when the sample is thin.
H2H_MAX_WEIGHT = 0.15
H2H_FULL_WEIGHT_GAMES = 6


def _blend_h2h(state: CodEloState, team_a: str, team_b: str,
               dist: SeriesDistribution) -> SeriesDistribution:
    wins_a, total = state.h2h_record(team_a, team_b)
    if total <= 0:
        return dist
    weight = H2H_MAX_WEIGHT * min(1.0, total / H2H_FULL_WEIGHT_GAMES)
    h2h_rate = wins_a / total
    blended_series = (1.0 - weight) * dist.prob_series_win_a() + weight * h2h_rate
    # Re-derive the MAP probability that reproduces the blended SERIES number,
    # so the score distribution stays internally consistent. Blending the map
    # probability directly would leave prob_series_win_a disagreeing with the
    # distribution it is summed from.
    map_p = map_p_for_series_prob(blended_series, dist.best_of)
    return SeriesDistribution(map_p=map_p, best_of=dist.best_of,
                              dist=series_score_distribution(map_p, dist.best_of))


def get_series_distribution(team_a: str, team_b: str, best_of: int,
                            match_date: str | None = None) -> SeriesDistribution | None:
    """None when either side lacks MIN_GAMES of real history, which is what
    keeps an unrated team from being priced off a fabricated default.

    The gate lives HERE rather than in get_team_rating because this function --
    not the rating lookup -- is what decides whether a market can be priced at
    all. Splitting them is how one path ends up reading the pool differently
    from the other, a mistake this codebase has made four times."""
    state = _cache.get("state")
    if state is None or not best_of:
        return None
    team_a = resolve_team_name(team_a)
    team_b = resolve_team_name(team_b)
    if state.games_played(team_a) < MIN_GAMES or state.games_played(team_b) < MIN_GAMES:
        return None
    return _blend_h2h(state, team_a, team_b, predict_series(state, team_a, team_b, best_of))

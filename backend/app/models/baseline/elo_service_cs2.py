"""In-process cache of current CS2 team Elo ratings -- parallel to
elo_service_valorant.py, but NO LONGER purely cold-start: trains first on a
real historical match cache (data/cs2_historical_match_cache.json, built by
scripts/build_cs2_match_cache.py -- 8,843 real, concluded S-Tier + A-Tier
tournament matches scraped fresh from liquipedia.net, 2023-06-01 through
2026-07-18, 86 tournaments -- expanded 2026-07-20 from the original 6,283-
match S-Tier-only crawl to grow the real market-odds backtest sample), THEN
continues walk-forward through this app's own live-polled Cs2Match table on
top of that -- same "historical cache first, live data on top" pattern as
elo_service_mma.py's ufcstats cache / build_ufc_fight_cache.py, just for
CS2's own real source.

Historical and live rows can genuinely overlap (a very recent match scraped
by BOTH the one-off historical crawl AND the live poller, since both derive
source_match_id identically -- see cs2_data.py::parse_matches_from_html/
fetch_matches, which share the same underlying parser) -- deduped by
source_match_id below, keeping whichever copy sorts first (they're the same
real match either way, so which copy "wins" doesn't matter).

K=32 (see elo_cs2.py) IS grid-searched against this real historical data
(scripts/derive_cs2_elo_constants.py -- a real, if modest, shift from the
S-Tier-only pass's own K=24 minimum, 60.75% walk-forward accuracy
post-warmup). model_validated stays False regardless -- that accuracy
measures the model's own internal signal from win/loss history alone, not
whether it beats real market odds (a real market-odds backtest DOES now
exist too, see elo_cs2.py's own docstring -- the market still beats the
model on that sample).

Also resolves each match's own most recent real Liquipedia transfer date per
team (data/cs2_transfer_history_cache.json, built by
scripts/build_cs2_transfer_history_cache.py -- 14,849 real events, 2023-05
through 2026-07) and feeds it to update_ratings for the validated
roster-tenure boost (see elo_cs2.py::ROSTER_BOOST_MULTIPLIER's own module
comment).

Also tracks each team's own most recent real match date (no new scraping --
already in this app's own match cache) for the validated rest/fatigue
adjustment applied at PREDICTION time only (see REST_POINTS_PER_DAY's own
module comment below)."""
from app.models.baseline import team_name_resolver as _tnr
import datetime
import json
import logging
from pathlib import Path

from app.db.database import SessionLocal
from app.db.models import Cs2Match
from app.models.baseline.cs2_lineups import Cs2LineupResolver
from app.models.baseline.elo_cs2 import (
    PLAYER_BLEND_WEIGHT, Cs2EloState, SeriesDistribution, implied_elo_diff, map_win_prob,
    predict_and_update, predict_series, series_score_distribution,
)

log = logging.getLogger("elo_service_cs2")

_cache: dict = {"state": None}

HISTORICAL_CACHE_PATH = Path(__file__).resolve().parent.parent.parent.parent.parent / "data" / "cs2_historical_match_cache.json"
TRANSFER_CACHE_PATH = Path(__file__).resolve().parent.parent.parent.parent.parent / "data" / "cs2_transfer_history_cache.json"


def _load_historical_matches() -> list[dict]:
    if not HISTORICAL_CACHE_PATH.exists():
        return []
    rows = json.loads(HISTORICAL_CACHE_PATH.read_text(encoding="utf-8"))
    return [
        {
            "source_match_id": r["source_match_id"], "team_a": r["team_a"], "team_b": r["team_b"],
            # Liquipedia's short display name (e.g. "Spirit" for "Team Spirit")
            # -- carried through purely so cs2_lineups.py can match a team
            # against a participant-roster key under EITHER spelling. The live
            # Cs2Match table has no equivalent column, so live rows pass None.
            "team_a_display": r.get("team_a_display"), "team_b_display": r.get("team_b_display"),
            "best_of": r.get("best_of"), "winner": r.get("winner"),
            "match_date": r.get("match_date"),
            "sort_key": r.get("estimated_start_time") or r.get("match_date") or "",
        }
        for r in rows
    ]


def _load_live_matches(session) -> list[dict]:
    rows = session.query(Cs2Match).all()
    return [
        {
            "source_match_id": r.source_match_id, "team_a": r.team_a, "team_b": r.team_b,
            "best_of": r.best_of, "winner": r.winner,
            "match_date": r.match_date,
            "sort_key": r.estimated_start_time or r.match_date or "",
        }
        for r in rows
    ]


def _load_transfers_by_team() -> dict[str, list[str]]:
    """Real transfer dates per team, sorted -- see
    scripts/build_cs2_transfer_history_cache.py's own docstring for the
    source (Liquipedia's Player_Transfers/{year}/{month} archive)."""
    if not TRANSFER_CACHE_PATH.exists():
        return {}
    events = json.loads(TRANSFER_CACHE_PATH.read_text(encoding="utf-8"))
    by_team: dict[str, list[str]] = {}
    for e in events:
        by_team.setdefault(e["team"], []).append(e["date"])
    for team in by_team:
        by_team[team].sort()
    return by_team


def _resolve_transfer_date(team: str, match_date: str | None, transfers_by_team: dict[str, list[str]]) -> str | None:
    """The most recent real transfer date for this team STRICTLY BEFORE this
    match's own date (walk-forward safe -- never uses a transfer that
    happened after the match being scored). None if no tracked transfer
    exists for this team, or the match's own date isn't known."""
    if not match_date:
        return None
    dates = transfers_by_team.get(team)
    if not dates:
        return None
    prior = [d for d in dates if d < match_date]
    return prior[-1] if prior else None


def refresh_ratings():
    session = SessionLocal()
    try:
        live_matches = _load_live_matches(session)
    finally:
        session.close()
    historical_matches = _load_historical_matches()

    # Dedupe by source_match_id (see module docstring on why real overlap
    # happens), then walk forward in real chronological order -- combining
    # both sources BEFORE sorting, not training on historical then live as
    # two separate blind passes, since the live table can itself contain
    # matches that predate some historical ones weren't scraped yet (e.g. a
    # very recent live-polled match).
    by_id: dict[str, dict] = {}
    for m in historical_matches:
        by_id[m["source_match_id"]] = m
    for m in live_matches:
        by_id[m["source_match_id"]] = m
    all_matches = sorted(by_id.values(), key=lambda m: m["sort_key"])

    transfers_by_team = _load_transfers_by_team()
    for m in all_matches:
        m["team_a_transfer_date"] = _resolve_transfer_date(m["team_a"], m.get("match_date"), transfers_by_team)
        m["team_b_transfer_date"] = _resolve_transfer_date(m["team_b"], m.get("match_date"), transfers_by_team)

    # Real lineups for the player-level model (see elo_cs2.py::K_PLAYER).
    # A tournament's anchor date is the EARLIEST real match date seen in it --
    # derived from this app's own match data rather than trusting a separate
    # tournament-date field, so the anchor always sits inside the real event.
    tournament_dates: dict[str, str] = {}
    for m in all_matches:
        slug = str(m["source_match_id"]).split(":")[0]
        d = m.get("match_date")
        if d and (slug not in tournament_dates or d < tournament_dates[slug]):
            tournament_dates[slug] = d
    resolver = Cs2LineupResolver(tournament_dates=tournament_dates)
    for m in all_matches:
        slug = str(m["source_match_id"]).split(":")[0]
        m["lineup_a"] = resolver.lineup(slug, m["team_a"], m.get("team_a_display"), m.get("match_date"))
        m["lineup_b"] = resolver.lineup(slug, m["team_b"], m.get("team_b_display"), m.get("match_date"))
    _cache["lineup_resolver"] = resolver

    state = Cs2EloState()
    rated = 0
    last_played_date: dict[str, str] = {}
    for m in all_matches:
        if predict_and_update(state, m) is not None and m["winner"] is not None:
            rated += 1
        match_date = m.get("match_date")
        if match_date and m["winner"] is not None:
            # Only a real SETTLED match counts as "played" -- all_matches
            # includes upcoming/pending live-polled matches with a real
            # scheduled date but no winner yet, which must NOT count as a
            # team's most recent real match (that would treat a not-yet-played
            # future match as if it had already happened).
            last_played_date[m["team_a"]] = match_date
            last_played_date[m["team_b"]] = match_date
    # Market and match feeds spell teams differently and Elo lookups are exact,
    # so a market's spelling can hold a rating built from no games while another
    # spelling holds the history. See team_name_resolver for the guards and for
    # the blanket-merge approach that was tried and rejected on the data.
    _match_counts = _tnr.count_appearances(all_matches)
    _cache["match_counts"] = _match_counts
    _cache["canonical_by_key"] = _tnr.build_canonical_by_key(_match_counts, MIN_GAMES)
    _cache["state"] = state
    _cache["last_played_date"] = last_played_date
    log.info(
        "cs2 elo ratings refreshed: %d teams rated, %d settled series scored (%d historical + %d live, deduped to %d)",
        len(state.ratings), rated, len(historical_matches), len(live_matches), len(all_matches),
    )


def resolve_team_name(team: str) -> str:
    """The spelling that owns this team's match history, or the input unchanged."""
    return _tnr.resolve(team, _cache.get("match_counts") or {},
                        _cache.get("canonical_by_key") or {}, MIN_GAMES)


def get_team_rating(team: str) -> float | None:
    state = _cache.get("state")
    if state is None:
        return None
    return state.get(team)


MIN_GAMES = 3  # both teams need this many real settled series before a rating counts as trustworthy -- see get_series_distribution's own docstring for the real Brier-by-games-bucket data behind the number

# Real head-to-head blending (2026-07-20 addition, see Cs2EloState.h2h's own
# docstring for why this exists). Bayesian shrinkage: treats the Elo-implied
# series-win probability as a pseudo-prior worth H2H_PRIOR_WEIGHT
# pseudo-observations, blended with the real head-to-head wins/total for
# this exact team pair -- naturally reduces to pure Elo when total=0 (no
# prior meetings), so no separate "minimum meetings" gate is needed.
# Grid-searched (scripts/test_cs2_h2h_signal.py) against this real
# 8,839-match crawl: a real, smooth basin (Brier 0.23368 pure-Elo ->
# 0.23189 at H2H_PRIOR_WEIGHT=7, the minimum -> 0.23223 at weight=14),
# not a noisy single-cell spike, same credibility bar every other constant
# in this app used.
H2H_PRIOR_WEIGHT = 7.0


def _map_p_for_series_prob(target_prob: float, best_of: int, iterations: int = 60) -> float:
    """Binary search inverse of series_score_distribution's own
    prob_series_win_a() -- that function is monotonically increasing in
    map_p (more per-map skill can only raise, never lower, a team's series
    win probability), so bisection converges cleanly. Used to fold the
    head-to-head-blended series probability back into a single effective
    map_p, so every OTHER derived market (map N winner, total maps,
    handicap) stays internally consistent with the same blended number
    rather than only patching the top-line series-win figure."""
    lo, hi = 0.0, 1.0
    for _ in range(iterations):
        mid = (lo + hi) / 2.0
        dist = series_score_distribution(mid, best_of)
        prob = sum(p for (a, b), p in dist.items() if a > b)
        if prob < target_prob:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _blend_h2h(state: Cs2EloState, team_a: str, team_b: str, dist: SeriesDistribution) -> SeriesDistribution:
    wins_a, total = state.h2h_record(team_a, team_b)
    if total == 0:
        return dist
    elo_prob = dist.prob_series_win_a()
    blended_prob = (elo_prob * H2H_PRIOR_WEIGHT + wins_a) / (H2H_PRIOR_WEIGHT + total)
    map_p = _map_p_for_series_prob(blended_prob, dist.best_of)
    return SeriesDistribution(map_p=map_p, best_of=dist.best_of, dist=series_score_distribution(map_p, dist.best_of))


# Real rest/fatigue adjustment (2026-07-20 addition) -- uses each team's own
# most recent real settled-match date, already present in this app's own
# match cache (no new scraping, same "data already in hand" shape as
# H2H_PRIOR_WEIGHT above). Grid-searched (scripts/test_cs2_rest_signal.py)
# against the real 8,839-match walk-forward: DISCOUNTING (fewer rest days ->
# lower effective rating, i.e. treating short rest as a real handicap)
# measurably HURTS and gets steadily worse the longer the window -- MORE
# rest measurably HELPS instead, a real, smooth basin (Brier 0.23368 ->
# 0.23208 at REST_POINTS_PER_DAY=35-40/REST_CAP_DAYS=2, not a single-cell
# spike). Applied as a real Elo-rating-point bonus for whichever side has
# had more real rest, capped at REST_CAP_DAYS (a team resting 30 real days
# between tournaments isn't "extra rested," it's just infrequent play --
# letting the bonus keep growing past the cap measurably regressed Brier in
# the same grid search). Composed on top of whatever dist is passed in
# (h2h-blended or pure Elo) via implied_elo_diff, not jointly re-validated
# with H2H_PRIOR_WEIGHT together -- each adjustment's own real, independent
# effect on top of pure Elo is what was measured.
REST_POINTS_PER_DAY = 35.0
REST_CAP_DAYS = 2


def _rest_bonus(team: str, match_date: str) -> float:
    last = _cache.get("last_played_date", {}).get(team)
    if last is None:
        return 0.0
    rest_days = (datetime.date.fromisoformat(match_date[:10]) - datetime.date.fromisoformat(last[:10])).days
    return REST_POINTS_PER_DAY * min(max(rest_days, 0), REST_CAP_DAYS)


def _blend_rest(team_a: str, team_b: str, dist: SeriesDistribution, match_date: str | None) -> SeriesDistribution:
    if not match_date:
        return dist
    bonus_a = _rest_bonus(team_a, match_date)
    bonus_b = _rest_bonus(team_b, match_date)
    if bonus_a == bonus_b:
        return dist
    diff = implied_elo_diff(dist.map_p) + (bonus_a - bonus_b)
    map_p = map_win_prob(diff, 0.0)
    return SeriesDistribution(map_p=map_p, best_of=dist.best_of, dist=series_score_distribution(map_p, dist.best_of))


def _blend_player(state: Cs2EloState, dist: SeriesDistribution, team_a: str, team_b: str,
                  match_date: str | None) -> SeriesDistribution:
    """Blends the team-model series probability with the PLAYER-model one at
    PLAYER_BLEND_WEIGHT (see elo_cs2.py::K_PLAYER for the full validated
    finding). Applied LAST, on top of the h2h- and rest-adjusted team
    number -- exactly the composition order the market backtest validated.

    Returns `dist` UNCHANGED whenever either real lineup can't be resolved --
    which is the majority of matches (lineup coverage is 38.8% historically).
    That fallback is the point: a match with no known lineup gets the pure
    team prediction rather than a player estimate built on invented
    membership."""
    resolver = _cache.get("lineup_resolver")
    if resolver is None:
        return dist
    lineup_a = resolver.lineup(None, team_a, None, match_date)
    lineup_b = resolver.lineup(None, team_b, None, match_date)
    a_str = state.player_strength(lineup_a) if lineup_a else None
    b_str = state.player_strength(lineup_b) if lineup_b else None
    if a_str is None or b_str is None:
        return dist
    player_prob = sum(
        p for (a, b), p in series_score_distribution(map_win_prob(a_str, b_str), dist.best_of).items() if a > b
    )
    blended = (1.0 - PLAYER_BLEND_WEIGHT) * dist.prob_series_win_a() + PLAYER_BLEND_WEIGHT * player_prob
    map_p = _map_p_for_series_prob(blended, dist.best_of)
    return SeriesDistribution(map_p=map_p, best_of=dist.best_of, dist=series_score_distribution(map_p, dist.best_of))


def get_series_distribution(team_a: str, team_b: str, best_of: int, match_date: str | None = None):
    """See elo_service_lol.py's own version of this function for the full
    real-bug story (found live 2026-07-20 for LoL at an 86% real rate, CS2's
    own rate checked the same way is 12% -- lower, but the same latent
    architectural gap, fixed here too for consistency/correctness).

    Also requires MIN_GAMES real settled series for BOTH teams, not just
    "has played at least 1" -- real data (Brier score broken out by
    min-games-played bucket, checked live 2026-07-20 against this exact
    historical crawl): 0.23368 at >=0 games, 0.23265 at >=1, 0.23193 at >=2,
    0.23089 at >=3, then diminishing/inconsistent returns (0.23124 at >=5,
    a slight regression before continuing down at >=10/>=20) -- >=3 is
    where the real, consistent gain from the 1-2-game noise floor is mostly
    captured, without cutting the eligible sample down further than
    necessary (6,274 of 8,039 post-warmup predictions still qualify at
    >=3)."""
    state = _cache.get("state")
    if state is None or not best_of:
        return None
    # Resolve each side onto the spelling that owns its history BEFORE this
    # gate. Resolving only in get_team_rating leaves this reading the raw
    # market spelling, so a team whose history lives under another spelling
    # still fails MIN_GAMES and the whole match stays unpriced -- this gate,
    # not the rating lookup, is what decides if a market can be priced.
    team_a = resolve_team_name(team_a)
    team_b = resolve_team_name(team_b)
    if state.games_played(team_a) < MIN_GAMES or state.games_played(team_b) < MIN_GAMES:
        return None
    dist = _blend_h2h(state, team_a, team_b, predict_series(state, team_a, team_b, best_of))
    dist = _blend_rest(team_a, team_b, dist, match_date)
    return _blend_player(state, dist, team_a, team_b, match_date)

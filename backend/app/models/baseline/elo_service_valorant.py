"""In-process cache of current Valorant team Elo ratings -- parallel to
elo_service_cs2.py, but NO LONGER purely cold-start: trains first on a real
historical match cache (data/valorant_historical_match_cache.json, built by
scripts/build_valorant_match_cache.py -- 19,644 usable real matches across
455 curated events: the main VCT International/regional circuit, Game
Changers (the women's division, added after live Kalshi markets for GC
matches were found staying stuck at BASE_RATING under a main-circuit-only
crawl), AND Challengers League (the regional 2nd tier, added to grow the
real market-odds backtest sample) -- see elo_valorant.py's own docstring
for the full three-pass story), THEN continues walk-forward through this
app's own live-polled ValorantMatch table on top of that -- same
"historical cache first, live data on top" pattern as
elo_service_cs2.py/elo_service_mma.py.

Historical and live rows can genuinely overlap (a very recent match scraped
by BOTH the one-off historical crawl AND the live poller) -- deduped by
source_match_id below, same reasoning as elo_service_cs2.py's own docstring.
UNLIKE CS2's identical-parser overlap, Valorant's live poller's source_match_id
for a "live:"-prefixed synthetic row (created before vlr.gg's own real id was
seen, see market_catalog_valorant.py::upsert_vlr_match) will NOT match the
historical crawl's real vlr.gg match id -- this is a known, minor limitation
(a handful of very recent matches could double-count if scraped both ways
before the live poller reconciles onto the real vlr.gg id), not corrected
here; the vast majority of the historical cache predates any live-polled
overlap window entirely.

K=36 (see elo_valorant.py) IS grid-searched against this real combined
historical data, under the per-map update rule shipped 2026-07-20
(scripts/derive_valorant_elo_constants.py -- 63.38% walk-forward accuracy
post-warmup, up from the old per-series rule's own 61.99% at K=40 -- see
elo_valorant.py::update_ratings's own docstring for the full story).

Also resolves each match's real patch era (liquipedia.net/valorant/Patches,
153 real patches 2020-2025, one cheap page fetch -- see _load_patches' own
docstring) and feeds it to update_ratings for the validated patch-recency
boost (see elo_valorant.py::PATCH_BOOST_MULTIPLIER's own module comment).

Also tracks each team's own most recent real match date (no new scraping)
for the validated rest/fatigue adjustment applied at PREDICTION time only
(see REST_POINTS_PER_DAY's own module comment below, same technique as
elo_service_cs2.py's own version)."""
from app.models.baseline import team_name_folding
from app.models.baseline import team_name_resolver as _tnr
import datetime
import json
import unicodedata
import re
import logging
from pathlib import Path

from app.db.database import SessionLocal
from app.db.models import ValorantMatch
from app.ingestion.valorant_data import infer_best_of_from_score
from app.models.baseline.valorant_lineups import ValorantLineupResolver
from app.models.baseline.elo_valorant import (
    PLAYER_BLEND_WEIGHT, ValorantEloState, SeriesDistribution, implied_elo_diff, map_win_prob,
    predict_and_update, predict_series, series_score_distribution,
)

log = logging.getLogger("elo_service_valorant")

_cache: dict = {"state": None}

HISTORICAL_CACHE_PATH = Path(__file__).resolve().parent.parent.parent.parent.parent / "data" / "valorant_historical_match_cache.json"


def _load_historical_matches() -> list[dict]:
    """best_of is inferred from the final map tally, not read from the cache
    -- vlr.gg's own match-listing pages never state it directly (see
    infer_best_of_from_score's own docstring for the real gap this closes).
    Rows with match_date < 2020 are dropped -- a real, isolated vlr.gg data
    quirk (1 row out of 13,036, a forfeit whose timer decoded to Unix epoch
    0) found live 2026-07-19, see derive_valorant_elo_constants.py's own
    docstring for the full story.

    REAL BUG fixed here (found live 2026-07-20, while wiring in the
    patch-boost feature): maps_won_a/maps_won_b were being read to INFER
    best_of but never actually included in the returned match dict itself
    -- update_ratings' own per-map logic checks match.get("maps_won_a"),
    which was always None here, meaning the per-map update change (shipped
    the same day, see elo_valorant.py::update_ratings's own docstring) was
    silently falling back to the OLD single-series-level update in
    PRODUCTION this whole time, despite being validated and believed
    shipped -- the offline derivation script had its own, correct copy of
    this field, so the validated K=36/63.38% numbers are real, just never
    actually applied live until this fix."""
    if not HISTORICAL_CACHE_PATH.exists():
        return []
    rows = json.loads(HISTORICAL_CACHE_PATH.read_text(encoding="utf-8"))
    rows = [r for r in rows if r["match_date"] >= "2020-01-01"]
    return [
        {
            "source_match_id": r["source_match_id"], "team_a": r["team_a"], "team_b": r["team_b"],
            "best_of": r.get("best_of") or infer_best_of_from_score(r.get("maps_won_a"), r.get("maps_won_b")),
            "maps_won_a": r.get("maps_won_a"), "maps_won_b": r.get("maps_won_b"),
            "winner": r.get("winner"),
            "match_date": r.get("match_date"),
            "sort_key": r.get("estimated_start_time") or r.get("match_date") or "",
        }
        for r in rows
    ]


def _load_live_matches(session) -> list[dict]:
    rows = session.query(ValorantMatch).all()
    return [
        {
            "source_match_id": r.source_match_id, "team_a": r.team_a, "team_b": r.team_b,
            "best_of": r.best_of or infer_best_of_from_score(r.maps_won_a, r.maps_won_b),
            "maps_won_a": r.maps_won_a, "maps_won_b": r.maps_won_b,
            "winner": r.winner,
            "match_date": r.match_date,
            "sort_key": r.estimated_start_time or r.match_date or "",
        }
        for r in rows
    ]


PATCHES_PATH = Path(__file__).resolve().parent.parent.parent.parent.parent / "data" / "valorant_patches.json"


def _load_patches() -> list[dict]:
    """Real patch history from liquipedia.net/valorant/Patches (153 patches,
    2020-2025, one cheap page fetch -- see scripts/test_valorant_patch_signal.py
    for how this was originally pulled and validated). Sorted by date so
    _patch_era_for_date's own linear scan is correct."""
    if not PATCHES_PATH.exists():
        return []
    patches = json.loads(PATCHES_PATH.read_text(encoding="utf-8"))
    patches.sort(key=lambda p: p["date"])
    return patches


def _patch_era_for_date(date: str | None, patches: list[dict]) -> str | None:
    """The LATEST real patch whose date <= this match's date -- None if the
    match predates every known patch, or if no real match_date exists at
    all (never guessed)."""
    if not date or not patches:
        return None
    era = None
    for p in patches:
        if p["date"] <= date:
            era = p["patch"]
        else:
            break
    return era


def refresh_ratings():
    session = SessionLocal()
    try:
        live_matches = _load_live_matches(session)
    finally:
        session.close()
    historical_matches = _load_historical_matches()

    by_id: dict[str, dict] = {}
    for m in historical_matches:
        by_id[m["source_match_id"]] = m
    for m in live_matches:
        by_id[m["source_match_id"]] = m
    all_matches = sorted(by_id.values(), key=lambda m: m["sort_key"])
    # MERGE SPELLING VARIANTS BEFORE TRAINING. The same team arrives under
    # several spellings and each was accumulating its own independent Elo --
    # measured 2026-08-10: Dplus Kia 1794.1 vs Dplus KIA 1516.8, Heroic 1591.5
    # vs HEROIC 1741.1. Which rating a live match got priced off then depended
    # on which spelling the exchange happened to use.
    #
    # Done HERE, on the combined history, because that is what actually merges
    # the two rating histories; a lookup-time redirect would leave both pools
    # split and each training on half the matches. Folds case/whitespace/
    # punctuation ONLY, and every candidate group must survive a self-play
    # disproof test -- see team_name_folding for why fuzzy matching is unsafe
    # here (Evil Geniuses vs Evil Geniuses GC are different teams).
    _folded = team_name_folding.apply_to_matches(all_matches)
    if _folded:
        log.info("%s: folded %d team-name variants into canonical spellings", __name__, _folded)

    patches = _load_patches()
    for m in all_matches:
        m["patch_era"] = _patch_era_for_date(m.get("match_date"), patches)

    # Real per-match lineups for the player-level model (see
    # elo_valorant.py::K_PLAYER). Resolved inside the chronological loop --
    # note_played() records each side's most recent lineup as we go, so the
    # live path (latest_for_team, used for a not-yet-played match with no
    # scoreboard) can never see a lineup from the future.
    resolver = ValorantLineupResolver()
    _cache["lineup_resolver"] = resolver

    state = ValorantEloState()
    rated = 0
    last_played_date: dict[str, str] = {}
    for m in all_matches:
        la, lb = resolver.for_match(m["source_match_id"], m["team_a"], m["team_b"])
        m["lineup_a"], m["lineup_b"] = la, lb
        if predict_and_update(state, m) is not None and m["winner"] is not None:
            rated += 1
        if m["winner"] is not None:
            resolver.note_played(m["team_a"], m["team_b"], la, lb)
        match_date = m.get("match_date")
        if match_date and m["winner"] is not None:
            last_played_date[m["team_a"]] = match_date
            last_played_date[m["team_b"]] = match_date
    # Count real, settled appearances per spelling -- that asymmetry is what
    # makes the alias redirect safe (see _build_name_aliases).
    match_counts = _tnr.count_appearances(all_matches)
    _cache["match_counts"] = match_counts
    # Valorant-specific acronym map (see team_name_resolver.EXPANSIONS_BY_TITLE).
    # It adds "drx" -> "kiwoom drx", which this title needs and LoL must not
    # have. The SAME map has to be passed to resolve() below, or a key built
    # under one map is queried under another and silently misses.
    _cache["canonical_by_key"] = _tnr.build_canonical_by_key(
        match_counts, MIN_GAMES, _tnr.expansions_for("valorant"))
    _cache["state"] = state
    _cache["last_played_date"] = last_played_date
    log.info(
        "valorant elo ratings refreshed: %d teams rated, %d settled series scored (%d historical + %d live, deduped to %d)",
        len(state.ratings), rated, len(historical_matches), len(live_matches), len(all_matches),
    )


def resolve_team_name(team: str) -> str:
    """The spelling that owns this team's match history, or the input unchanged.
    Shared with cs2/lol -- see team_name_resolver for the guards and for the
    blanket-merge approach that was tried and rejected on the data."""
    return _tnr.resolve(team, _cache.get("match_counts") or {},
                        _cache.get("canonical_by_key") or {}, MIN_GAMES,
                        _tnr.expansions_for("valorant"))


def get_team_rating(team: str) -> float | None:
    state = _cache.get("state")
    if state is None:
        return None
    resolved = resolve_team_name(team)
    if resolved not in state.ratings:
        # Not "average" -- never seen. Returning BASE_RATING here made those two
        # indistinguishable in the drawer; see get_team_games.
        return None
    return state.get(resolved)


def get_team_games(team: str) -> int:
    """Real observations behind this team's rating, resolved onto the spelling
    that owns the history (same as get_team_rating).

    Exists so a displayed rating can be read honestly. BASE_RATING is 1500 and
    the median rated team sits near it, so "1500" is genuinely ambiguous on its
    own: it can mean "never seen" or "seen plenty and rates average". A user has
    now asked which it was TWICE -- once for Ground Zero (it was the name-
    resolution miss recorded above) and once for UNiTY esports, where 1500.35
    turned out to be a real rating off 36 games at the 67th percentile. Showing
    the count next to the number ends the ambiguity instead of answering it
    case by case."""
    state = _cache.get("state")
    if state is None:
        return 0
    return state.games_played(resolve_team_name(team))


MIN_GAMES = 5  # both teams need this many real map observations before a rating counts as trustworthy -- see get_series_distribution's own docstring for the real Brier-by-games-bucket data behind the number

# Real head-to-head blending (2026-07-20 addition, see ValorantEloState.h2h's
# own docstring and elo_cs2.py's equivalent for the shared rationale).
# Grid-searched (scripts/test_valorant_h2h_signal.py) against the real
# 19,644-match crawl: a real, smooth basin (Brier 0.22506 pure-Elo ->
# 0.22430 at H2H_PRIOR_WEIGHT=10, the minimum -> 0.22447 at weight=24).
H2H_PRIOR_WEIGHT = 10.0


def _map_p_for_series_prob(target_prob: float, best_of: int, iterations: int = 60) -> float:
    """See elo_service_cs2.py's identical function for the full rationale
    (bisection inverse of series_score_distribution's own monotonic
    prob_series_win_a)."""
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


def _blend_h2h(state: ValorantEloState, team_a: str, team_b: str, dist: SeriesDistribution) -> SeriesDistribution:
    wins_a, total = state.h2h_record(team_a, team_b)
    if total == 0:
        return dist
    elo_prob = dist.prob_series_win_a()
    blended_prob = (elo_prob * H2H_PRIOR_WEIGHT + wins_a) / (H2H_PRIOR_WEIGHT + total)
    map_p = _map_p_for_series_prob(blended_prob, dist.best_of)
    return SeriesDistribution(map_p=map_p, best_of=dist.best_of, dist=series_score_distribution(map_p, dist.best_of))


# Real rest/fatigue adjustment (2026-07-20 addition) -- see
# elo_service_cs2.py's identical constant for the shared rationale (data
# already in hand, no new scraping). Grid-searched
# (scripts/test_valorant_rest_signal.py) against the real 19,644-match
# walk-forward: same direction of finding as CS2 (more rest measurably
# helps, discounting measurably hurts), real smooth basin (Brier 0.22506 ->
# 0.22452 at REST_POINTS_PER_DAY=10-12/REST_CAP_DAYS=4), smaller magnitude
# than CS2's own (per-map update rule spreads the same real signal across
# more, smaller Elo nudges).
REST_POINTS_PER_DAY = 10.0
REST_CAP_DAYS = 4


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


def _blend_player(state: ValorantEloState, dist: SeriesDistribution, team_a: str, team_b: str) -> SeriesDistribution:
    """Blends in the PLAYER-model series probability at PLAYER_BLEND_WEIGHT
    (see elo_valorant.py::K_PLAYER -- note this effect is much SMALLER than
    CS2's and is NOT market-validated). Uses each team's most recent real
    lineup, since an upcoming match has no scoreboard of its own.

    Returns `dist` UNCHANGED whenever either lineup is unknown -- a team this
    app has never seen with a real lineup gets the pure team prediction
    rather than a player estimate built on invented membership."""
    resolver = _cache.get("lineup_resolver")
    if resolver is None:
        return dist
    a_str = state.player_strength(resolver.latest_for_team(team_a))
    b_str = state.player_strength(resolver.latest_for_team(team_b))
    if a_str is None or b_str is None:
        return dist
    player_prob = sum(
        p for (a, b), p in series_score_distribution(map_win_prob(a_str, b_str), dist.best_of).items() if a > b
    )
    blended = (1.0 - PLAYER_BLEND_WEIGHT) * dist.prob_series_win_a() + PLAYER_BLEND_WEIGHT * player_prob
    map_p = _map_p_for_series_prob(blended, dist.best_of)
    return SeriesDistribution(map_p=map_p, best_of=dist.best_of, dist=series_score_distribution(map_p, dist.best_of))


def get_series_distribution(team_a: str, team_b: str, best_of: int, match_date: str | None = None):
    """Returns the current SeriesDistribution off CURRENT ratings (live
    scoring, not training) -- None if ratings haven't been loaded yet, or if
    neither team has ever appeared in a real settled match (see
    elo_service_lol.py's own version of this function for the full real-bug
    story -- found live 2026-07-20 for LoL at an 86% real rate; Valorant's
    own rate checked the same way is 0% right now, but the same latent
    architectural gap, fixed here too for consistency/correctness).

    Also requires MIN_GAMES real map observations for BOTH teams (per-map,
    not per-series, since update_ratings now updates per real map -- see
    its own docstring). Real data (Brier by min-games-played bucket, checked
    live 2026-07-20 against the real 19,644-match historical crawl): 0.22506
    at >=0 games, 0.22179 at >=1, 0.21943 at >=3, 0.21786 at >=5, 0.21627 at
    >=10, then a slight uptick at >=20 (0.21925, small-sample noise) -- >=5
    is where the real, consistent gain levels off while still keeping a
    healthy majority of predictions eligible (12,846 of 19,144 post-warmup)."""
    state = _cache.get("state")
    if state is None or not best_of:
        return None
    # Resolve each side onto the spelling that owns its match history BEFORE the
    # games gate. Doing it only in get_team_rating left this gate reading the raw
    # market spelling, so a team whose history lives under another spelling still
    # failed MIN_GAMES and the whole match stayed unpriced -- the redirect had no
    # effect on the one code path that decides whether a market can be priced.
    stages = _series_stages(team_a, team_b, best_of, match_date)
    return stages["dist"] if stages else None


def _series_stages(team_a: str, team_b: str, best_of: int, match_date: str | None = None) -> dict | None:
    """The stage-by-stage build of the series number, so the reasoning drawer
    can report the SAME components the price was actually made of.

    Exists because the drawer used to recite only the two team Elo ratings while
    the price came from those PLUS head-to-head and the player blend. On
    Team Secret vs DetonatioN FocusMe that read as a contradiction and a user
    rightly challenged it: team Elo says Secret is 114 points worse, and the
    drawer said so, yet the model showed Secret at 51% -- because h2h (Secret
    3-1) and the player model (Secret's five rated 1566 vs 1483) together moved
    it +24pp, and neither was mentioned anywhere. Anything the price depends on
    has to be visible, or the number can't be audited.

    Returning the stages from the pricing function itself is deliberate: a
    second, parallel re-derivation in the router is exactly how the reasoning
    and pricing paths drift apart, which this codebase has now been bitten by
    several times.
    """
    state = _cache.get("state")
    if state is None or not best_of:
        return None
    team_a = resolve_team_name(team_a)
    team_b = resolve_team_name(team_b)
    if state.games_played(team_a) < MIN_GAMES or state.games_played(team_b) < MIN_GAMES:
        return None
    base = predict_series(state, team_a, team_b, best_of)
    after_h2h = _blend_h2h(state, team_a, team_b, base)
    after_rest = _blend_rest(team_a, team_b, after_h2h, match_date)
    final = _blend_player(state, after_rest, team_a, team_b)

    resolver = _cache.get("lineup_resolver")
    a_players = state.player_strength(resolver.latest_for_team(team_a)) if resolver else None
    b_players = state.player_strength(resolver.latest_for_team(team_b)) if resolver else None
    h2h_wins, h2h_total = state.h2h_record(team_a, team_b)
    return {
        "dist": final,
        "p_elo_only": base.prob_series_win_a(),
        "p_after_h2h": after_h2h.prob_series_win_a(),
        "p_after_rest": after_rest.prob_series_win_a(),
        "p_final": final.prob_series_win_a(),
        "h2h_wins_a": h2h_wins,
        "h2h_total": h2h_total,
        "player_strength_a": a_players,
        "player_strength_b": b_players,
    }


def explain_series(team_a: str, team_b: str, best_of: int, match_date: str | None = None) -> dict | None:
    """Public accessor for _series_stages -- see its docstring."""
    return _series_stages(team_a, team_b, best_of, match_date)

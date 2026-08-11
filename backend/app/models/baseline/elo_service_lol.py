"""In-process cache of current LoL team Elo ratings -- parallel to
elo_service_cs2.py/elo_service_valorant.py, but NO LONGER purely cold-start:
trains first on a real historical match cache (data/lol_historical_match_cache.json,
built by scripts/build_lol_match_cache.py -- 5,921 real matches, Leaguepedia's
own "Primary" tournament tier only (LCK/LPL/LEC/LCS-LTA/Worlds/MSI and their
real regional-league equivalents), 2023 through mid-2026), THEN continues
walk-forward through this app's own live-polled LolMatch table on top of
that -- same "historical cache first, live data on top" pattern as
elo_service_cs2.py/elo_service_valorant.py.

This crawl needed real, unusually heavy rate-limit resilience to build (see
lol_data.py::cargoquery's own docstring and scripts/build_lol_match_cache.py's
-- Leaguepedia's Cargo endpoint is far stricter than Liquipedia's plain page
views or vlr.gg, confirmed live: cargoquery calls failed and needed 4-minute
script-level cooldowns for roughly half the paginated requests during the
real crawl that built this cache).

Historical and live rows can genuinely overlap (a very recent match scraped
by BOTH the one-off historical crawl AND the live poller) -- deduped by
source_match_id below, same reasoning as elo_service_cs2.py's own docstring.

K=24 (see elo_lol.py) IS grid-searched against this real historical data,
under the per-map update rule shipped 2026-07-20
(scripts/derive_lol_elo_constants.py -- 67.86% walk-forward accuracy
post-warmup, up from the old per-series rule's own 67.13% at K=36, still the
strongest of all 3 esports titles in this app -- see elo_lol.py::
update_ratings's own docstring for the full story).

Also tracks each team's own most recent real match date (no new scraping)
for the validated rest/fatigue adjustment applied at PREDICTION time only
(see REST_POINTS_PER_DAY's own module comment below, same technique as
elo_service_cs2.py's own version)."""
from app.models.baseline import team_name_folding
from app.models.baseline import team_name_resolver as _tnr
import datetime
import json
import logging
from pathlib import Path

from app.db.database import SessionLocal
from app.db.models import LolMatch
from app.models.baseline.lol_lineups import LolLineupResolver
from app.models.baseline.lol_lower_tier import build_lower_tier_matches, pair_key as lower_tier_pair_key
from app.models.baseline.elo_lol import (
    PLAYER_BLEND_WEIGHT, LolEloState, SeriesDistribution, implied_elo_diff, map_win_prob,
    predict_and_update, predict_series, series_score_distribution,
)

log = logging.getLogger("elo_service_lol")

_cache: dict = {"state": None}

HISTORICAL_CACHE_PATH = Path(__file__).resolve().parent.parent.parent.parent.parent / "data" / "lol_historical_match_cache.json"


def _load_historical_matches() -> list[dict]:
    """REAL BUG fixed here (found live 2026-07-20, while wiring in
    Valorant's own patch-boost feature and re-checking the identical
    per-map-update code path for LoL): maps_won_a/maps_won_b were never
    included in the returned match dict at all -- update_ratings' own
    per-map logic checks match.get("maps_won_a"), which was always None
    here, meaning the per-map update change (shipped the same day, see
    elo_lol.py::update_ratings's own docstring) was silently falling back
    to the OLD single-series-level update in PRODUCTION this whole time,
    despite being validated and believed shipped -- the offline derivation
    script had its own, correct copy of this field, so the validated
    K=24/67.86% numbers are real, just never actually applied live until
    this fix."""
    if not HISTORICAL_CACHE_PATH.exists():
        return []
    rows = json.loads(HISTORICAL_CACHE_PATH.read_text(encoding="utf-8"))
    return [
        {
            "source_match_id": r["source_match_id"], "team_a": r["team_a"], "team_b": r["team_b"],
            "best_of": r.get("best_of"), "winner": r.get("winner"),
            "maps_won_a": r.get("maps_won_a"), "maps_won_b": r.get("maps_won_b"),
            "match_date": r.get("match_date"),
            "sort_key": r.get("estimated_start_time") or r.get("match_date") or "",
        }
        for r in rows
    ]


def _load_live_matches(session) -> list[dict]:
    rows = session.query(LolMatch).all()
    return [
        {
            "source_match_id": r.source_match_id, "team_a": r.team_a, "team_b": r.team_b,
            "best_of": r.best_of, "winner": r.winner,
            "maps_won_a": r.maps_won_a, "maps_won_b": r.maps_won_b,
            "match_date": r.match_date,
            "sort_key": r.estimated_start_time or r.match_date or "",
        }
        for r in rows
    ]


def _train(all_matches: list[dict], resolver: LolLineupResolver) -> tuple[LolEloState, dict, int]:
    """Walk-forward-trains a LolEloState over already-sorted matches, attaching
    each match's real lineup and recording last-played dates for the rest
    adjustment. Returns (state, last_played_date, settled_count)."""
    state = LolEloState()
    rated = 0
    last_played_date: dict[str, str] = {}
    for m in all_matches:
        la, lb = resolver.for_match(m.get("match_date"), m["team_a"], m["team_b"])
        m["lineup_a"], m["lineup_b"] = la, lb
        if predict_and_update(state, m) is not None and m["winner"] is not None:
            rated += 1
        match_date = m.get("match_date")
        if match_date and m["winner"] is not None:
            last_played_date[m["team_a"]] = match_date
            last_played_date[m["team_b"]] = match_date
            resolver.note_played(m["team_a"], m["team_b"], la, lb)
    return state, last_played_date, rated


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

    # Real per-game lineups for the player-level model (see elo_lol.py::
    # K_PLAYER). Resolved inside the chronological loop; note_played() records
    # each side's most recent lineup as we go, so the live path
    # (latest_for_team) never sees a future lineup.
    resolver = LolLineupResolver()
    _cache["lineup_resolver"] = resolver

    # POOL 1 (clean): Primary tier only -- drives every Primary-vs-Primary
    # prediction, byte-identical to the pre-expansion model.
    state, last_played_date, rated = _train(all_matches, resolver)
    # Market and match feeds spell teams differently and Elo lookups are exact,
    # so a market's spelling can hold a rating built from no games while another
    # spelling holds the history. See team_name_resolver for the guards and for
    # the blanket-merge approach that was tried and rejected on the data.
    _match_counts = _tnr.count_appearances(all_matches)
    _cache["match_counts"] = _match_counts
    _cache["canonical_by_key"] = _tnr.build_canonical_by_key(_match_counts, MIN_GAMES)
    _cache["state"] = state
    _cache["last_played_date"] = last_played_date

    # POOL 2 (expanded): Primary + gol.gg lower-tier series (see
    # lol_lower_tier.py + the tier-expansion feature, task #32). Used ONLY to
    # price matches the clean pool can't (a Kalshi team that never appears in
    # Primary). Trained on its OWN resolver so its note_played history is
    # independent. The two-pool split is what lets expansion add coverage with
    # ZERO pollution of the strong Primary model.
    exclude = {lower_tier_pair_key(m.get("match_date"), m["team_a"], m["team_b"]) for m in all_matches}
    lower = build_lower_tier_matches(exclude)
    exp_resolver = LolLineupResolver()
    _cache["lineup_resolver_exp"] = exp_resolver
    exp_all = sorted(list(by_id.values()) + lower, key=lambda m: m.get("sort_key") or m.get("match_date") or "")
    state_exp, last_played_exp, _ = _train(exp_all, exp_resolver)
    _cache["state_exp"] = state_exp
    _cache["last_played_date_exp"] = last_played_exp

    log.info(
        "lol elo ratings refreshed: %d teams rated (clean), %d rated (expanded, +%d lower-tier series), "
        "%d settled series scored (%d historical + %d live, deduped to %d)",
        len(state.ratings), len(state_exp.ratings), len(lower), rated,
        len(historical_matches), len(live_matches), len(all_matches),
    )


def resolve_team_name(team: str) -> str:
    """The spelling that owns this team's match history, or the input unchanged."""
    return _tnr.resolve(team, _cache.get("match_counts") or {},
                        _cache.get("canonical_by_key") or {}, MIN_GAMES)


def get_team_rating(team: str) -> float | None:
    """Resolve the name first -- see elo_service_cs2.get_team_rating for the
    real user-reported bug (a rated team displayed as an unrated 1500 because
    its history lives under another spelling). Same gap existed here."""
    state = _cache.get("state")
    if state is None:
        return None
    resolved = resolve_team_name(team)
    if resolved not in state.ratings:
        # Not "average" -- never seen. Returning BASE_RATING here made those two
        # indistinguishable in the drawer; see get_team_games.
        return None
    return state.get(resolved)


def get_field_ratings(teams: list[str]) -> dict[str, float]:
    """{team: rating} for a whole tournament field, from ONE pool.

    REAL DEFECT this fixes. price_tournament_winners built its field with
    get_team_rating, which reads the CLEAN Primary pool only, while its
    win_prob_fn uses get_series_distribution, which falls back to the EXPANDED
    pool. So the field builder dropped teams the pricer could price perfectly
    well, and (before the field-completeness guard) handed their win probability
    to whoever happened to be Primary-rated. Measured live: T1 Esports Academy
    has 128 games and a 1644 rating in the expanded pool and the field builder
    saw None; likewise HLE Challengers (31), RED Academy (33), RMD Gaming (21),
    7REX (19), Ei Nerd Esports (19), Team Solid (15). Whole Challengers and
    Circuito Desafiante fields were invisible to a model that could rate them.

    ROUTING MIRRORS get_series_distribution's, one field instead of one pair: if
    EVERY team clears MIN_GAMES in the clean pool, use the clean pool, so
    Primary-only fields stay byte-identical to the pre-expansion model. If any
    team does not, use the expanded pool for the ENTIRE field.

    Never MIXED, deliberately. The two pools are trained independently and are
    not on a common scale -- Estral Esports is 1553 clean and 1627 expanded --
    so drawing some of a field from each would seed a bracket off a comparison
    between two different rating systems.
    """
    state = _cache.get("state")
    if state is None:
        return {}
    resolved = {t: resolve_team_name(t) for t in teams if t}
    if all(state.games_played(r) >= MIN_GAMES for r in resolved.values()):
        return {t: state.get(r) for t, r in resolved.items()}
    exp = _cache.get("state_exp")
    if exp is None:
        return {t: state.get(r) for t, r in resolved.items()
                if state.games_played(r) >= MIN_GAMES}
    return {t: exp.get(r) for t, r in resolved.items()
            if exp.games_played(r) >= MIN_GAMES}


def get_matchup_ratings(team_a: str, team_b: str) -> dict | None:
    """The ratings the PRICE was actually computed from, plus their evidence.

    REAL BUG this exists for (user-reported): a recommended bet's reasoning read
    "both teams 1500" -- Elo's neutral default, i.e. "we know nothing" -- on a
    match the model had priced from real games. `get_team_rating` reads only the
    CLEAN Primary-tier pool, while `get_series_distribution` falls back to the
    EXPANDED pool for teams Primary has never seen (the whole point of the
    gol.gg tier expansion). Lower-tier matches are exactly the ones that fall
    back, so the display contradicted the model on precisely the bets whose
    reasoning most needed to be trusted -- BoostGate had 37 real map
    observations, Team Phoenix 23, and the panel claimed 1500/1500.

    Mirrors get_series_distribution's routing exactly rather than re-deriving
    it, so the two cannot drift apart. Returns None when NEITHER pool can price
    the matchup -- the honest "no rating" case, which the caller should render
    as no rating rather than as 1500.
    """
    state = _cache.get("state")
    if state is None:
        return None
    if state.games_played(team_a) >= MIN_GAMES and state.games_played(team_b) >= MIN_GAMES:
        pool, name = state, "primary"
    else:
        pool = _cache.get("state_exp")
        name = "expanded"
        # get_series_distribution resolves the names in THIS branch only --
        # mirror that exactly, or the panel reports a different rating (or no
        # rating) than the price was computed from, which is the whole defect
        # this function exists to prevent.
        team_a = resolve_team_name(team_a)
        team_b = resolve_team_name(team_b)
        if pool is None or pool.games_played(team_a) < MIN_GAMES or pool.games_played(team_b) < MIN_GAMES:
            return None
    return {
        "pool": name,
        "a_rating": pool.get(team_a),
        "b_rating": pool.get(team_b),
        "a_games": pool.games_played(team_a),
        "b_games": pool.games_played(team_b),
    }


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

# Real head-to-head blending (2026-07-20 addition, see LolEloState.h2h's own
# docstring and elo_cs2.py's equivalent for the shared rationale).
# Grid-searched (scripts/test_lol_h2h_signal.py) against the real 5,604-match
# crawl: a real, if SMALLER than CS2/Valorant, smooth basin (Brier 0.20727
# pure-Elo -> 0.20680 at H2H_PRIOR_WEIGHT=48, the minimum -> 0.20682 at
# weight=64) -- LoL's own Elo-only Brier is already the lowest (strongest)
# of the 3 titles, leaving less residual signal for h2h to add; low
# prior_weight values (heavy h2h influence) measurably REGRESS Brier here
# (e.g. weight=2: +0.01415), unlike CS2/Valorant, since a real prior meeting
# or two is far noisier relative to LoL's already-strong Elo signal. A high
# weight (mostly-Elo, h2h only nudging at the margin) is what's validated
# and shipped.
H2H_PRIOR_WEIGHT = 48.0


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


def _blend_h2h(state: LolEloState, team_a: str, team_b: str, dist: SeriesDistribution) -> SeriesDistribution:
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
# (scripts/test_lol_rest_signal.py) against the real 5,604-match
# walk-forward: same direction as CS2/Valorant (more rest measurably helps),
# but the SMALLEST magnitude of all 3 titles (Brier 0.20727 -> 0.20705 at
# REST_POINTS_PER_DAY=50-70/REST_CAP_DAYS=1) -- same pattern as this title's
# own H2H_PRIOR_WEIGHT finding: LoL's Elo-only signal is already the
# strongest of the 3 titles, leaving the least residual room for a
# secondary signal to add. cap_days=1 effectively makes this a binary
# "had at least 1 real day off before this match" signal rather than a
# continuous rest-days scale (a real, smooth basin, not a single-cell spike
# -- values from cap=1 through the plateau at points=50-70 all held together).
REST_POINTS_PER_DAY = 60.0
REST_CAP_DAYS = 1


def _rest_bonus(team: str, match_date: str, last_played: dict) -> float:
    last = last_played.get(team)
    if last is None:
        return 0.0
    rest_days = (datetime.date.fromisoformat(match_date[:10]) - datetime.date.fromisoformat(last[:10])).days
    return REST_POINTS_PER_DAY * min(max(rest_days, 0), REST_CAP_DAYS)


def _blend_rest(team_a: str, team_b: str, dist: SeriesDistribution, match_date: str | None, last_played: dict) -> SeriesDistribution:
    if not match_date:
        return dist
    bonus_a = _rest_bonus(team_a, match_date, last_played)
    bonus_b = _rest_bonus(team_b, match_date, last_played)
    if bonus_a == bonus_b:
        return dist
    diff = implied_elo_diff(dist.map_p) + (bonus_a - bonus_b)
    map_p = map_win_prob(diff, 0.0)
    return SeriesDistribution(map_p=map_p, best_of=dist.best_of, dist=series_score_distribution(map_p, dist.best_of))


def _blend_player(state: LolEloState, dist: SeriesDistribution, team_a: str, team_b: str, resolver) -> SeriesDistribution:
    """Blends in the PLAYER-model series probability at PLAYER_BLEND_WEIGHT
    (see elo_lol.py::K_PLAYER -- a real but MODEST, Valorant-tier effect).
    Uses each team's most recent real lineup, since an upcoming match has no
    scoreboard of its own. Returns `dist` UNCHANGED whenever either lineup is
    unknown -- which is the majority (coverage is 16.4% historically), so a
    match with no resolvable lineup keeps the pure team prediction."""
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
    """Returns None (no real prediction possible) rather than a degenerate
    50/50 guess dressed up as a real number -- REAL BUG this guards against
    (found live 2026-07-20, user-reported: LoL recommended bets showing huge
    "edges" that were really just market price vs. an uninformative flat
    50.0% model estimate): LolEloState.get() falls back to the same
    BASE_RATING default for any team never seen in a real settled match
    (training crawl or live-polled history) -- when NEITHER team_a nor
    team_b has one, map_win_prob() compares two identical unknowns and
    returns EXACTLY 0.5, not a real prediction. Confirmed live: 19/22 (86%)
    of LoL's currently-active matches hit this the day KXLOLGAME/KXLOLMAP
    opened up far more lower-tier coverage than this app's Primary-tier-only
    training crawl has ever seen (LCK/LPL/LEC/LCS-LTA/Worlds/MSI only) --
    CS2/Valorant have the same latent gap at a much lower real rate (12%/0%
    respectively when checked the same way), fixed there too. Checking real
    membership in state.ratings (not just whether the computed value equals
    0.5) is what distinguishes this degenerate case from a genuinely close,
    well-informed 50/50 call between two real, evenly-matched rated teams --
    only ONE team being unrated is left alone (a new entrant assumed
    average is normal, standard Elo behavior, not degenerate).

    Also requires MIN_GAMES real map observations for BOTH teams (per-map,
    not per-series, since update_ratings now updates per real map -- see
    its own docstring). Real data (Brier by min-games-played bucket, checked
    live 2026-07-20 against the real 5,604-match historical crawl): 0.20727
    at >=0 games, steadily improving to 0.20364 at >=5, 0.20245 at >=10,
    0.20216 at >=20 -- a real, smooth, monotonic improvement (unlike
    Valorant's own slight uptick at >=20) -- >=5 keeps the vast majority of
    predictions eligible (4,414 of 4,804 post-warmup) while already
    capturing most of the real gain."""
    state = _cache.get("state")
    if state is None or not best_of:
        return None

    # POOL ROUTING (tier expansion, task #32): if the CLEAN Primary-only pool
    # can price both teams, use it -- Primary-vs-Primary predictions are then
    # byte-identical to the pre-expansion model and take zero pollution. Only
    # when a team never appears in Primary (a lower-tier Kalshi team) do we
    # fall back to the EXPANDED pool, which is the only pool that can price it
    # at all. Either way the same MIN_GAMES gate and blend stack apply.
    if state.games_played(team_a) >= MIN_GAMES and state.games_played(team_b) >= MIN_GAMES:
        pool, last_played, resolver = state, _cache.get("last_played_date", {}), _cache.get("lineup_resolver")
    else:
        exp = _cache.get("state_exp")
        # Resolve each side onto the spelling that owns its history BEFORE this
        # gate. Resolving only in get_team_rating leaves this reading the raw
        # market spelling, so a team whose history lives under another spelling
        # still fails MIN_GAMES and the whole match stays unpriced -- this gate,
        # not the rating lookup, is what decides if a market can be priced.
        team_a = resolve_team_name(team_a)
        team_b = resolve_team_name(team_b)
        if exp is None or exp.games_played(team_a) < MIN_GAMES or exp.games_played(team_b) < MIN_GAMES:
            return None
        pool, last_played, resolver = exp, _cache.get("last_played_date_exp", {}), _cache.get("lineup_resolver_exp")

    dist = _blend_h2h(pool, team_a, team_b, predict_series(pool, team_a, team_b, best_of))
    dist = _blend_rest(team_a, team_b, dist, match_date, last_played)
    return _blend_player(pool, dist, team_a, team_b, resolver)


def explain_series(team_a: str, team_b: str, best_of: int, match_date: str | None = None) -> dict | None:
    """Stage-by-stage build of the series number, so the reasoning drawer can
    report the SAME components the price was actually made of.

    Measured 2026-08-07: 20 of 63 live LoL matches have the blends moving the
    number by 5pp or more, up to 17.3pp, and the drawer said nothing about any
    of it -- it recited the two team ratings and then quoted a model number the
    ratings alone do not produce. Same defect Valorant had.

    Mirrors get_series_distribution's POOL ROUTING exactly rather than
    re-deriving it. That is not optional here: LoL prices lower-tier teams from
    the EXPANDED pool, and reading the primary pool instead reports a baseline
    the price never used. A first pass at this measurement did exactly that and
    produced phantom 40pp "moves" from a fake 50.0% baseline -- which is the
    very 1500/1500 bug get_matchup_ratings was written to stop.
    """
    state = _cache.get("state")
    if state is None or not best_of:
        return None
    if state.games_played(team_a) >= MIN_GAMES and state.games_played(team_b) >= MIN_GAMES:
        pool, last_played, resolver = state, _cache.get("last_played_date", {}), _cache.get("lineup_resolver")
        pool_name = "primary"
    else:
        exp = _cache.get("state_exp")
        team_a = resolve_team_name(team_a)
        team_b = resolve_team_name(team_b)
        if exp is None or exp.games_played(team_a) < MIN_GAMES or exp.games_played(team_b) < MIN_GAMES:
            return None
        pool, last_played, resolver = exp, _cache.get("last_played_date_exp", {}), _cache.get("lineup_resolver_exp")
        pool_name = "expanded"

    base = predict_series(pool, team_a, team_b, best_of)
    after_h2h = _blend_h2h(pool, team_a, team_b, base)
    after_rest = _blend_rest(team_a, team_b, after_h2h, match_date, last_played)
    final = _blend_player(pool, after_rest, team_a, team_b, resolver)

    wins_a, total = pool.h2h_record(team_a, team_b) if hasattr(pool, "h2h_record") else (0, 0)
    return {
        "dist": final,
        "pool": pool_name,
        "p_elo_only": base.prob_series_win_a(),
        "p_after_h2h": after_h2h.prob_series_win_a(),
        "p_after_rest": after_rest.prob_series_win_a(),
        "p_final": final.prob_series_win_a(),
        "h2h_wins_a": wins_a,
        "h2h_total": total,
    }

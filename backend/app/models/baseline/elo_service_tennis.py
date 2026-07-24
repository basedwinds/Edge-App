"""In-process cache of current Tennis player Elo ratings -- parallel to
elo_service_mma.py, sourced from the offline historical match cache
(app/ingestion/tennis_data.py::load_matches(), backed by
data/tennisdata_matches_cache.json + data/tennisexplorer_matches_cache.json,
see scripts/build_tennis_match_cache.py) rather than a live DB table.

Same known limitation as elo_service_mma.py: ratings go stale for any
player whose most recent match happened after the historical cache was last
rebuilt -- no incremental "append this week's results" path yet, only a
full re-run of the cache-builder script.

model_validated: false, same as every other sport's baseline Elo in this
app, until scripts/backtest_moneyline_tennis.py's go/no-go check runs
against real historical odds."""
import logging

from app.ingestion import tennis_data
from app.models.baseline.elo_tennis import TennisEloState, predict_and_update, win_prob

log = logging.getLogger("elo_service_tennis")

_cache: dict = {"state": None}


def refresh_ratings():
    matches = tennis_data.load_matches()
    state = TennisEloState()
    for m in matches:
        predict_and_update(state, m)
    _cache["state"] = state
    log.info("tennis elo ratings refreshed: %d players rated, %d matches", len(state.overall_ratings), len(matches))


def get_player_rating(player_key: str, surface: str | None = None) -> float | None:
    state = _cache.get("state")
    if state is None:
        return None
    return state.blended_rating(player_key, surface)


def get_player_match_count(player_key: str) -> int:
    """Real prior-match count this player's CURRENT rating is built from --
    0 means they've never appeared in either offline training source
    (tennis-data.co.uk / tennisexplorer), so their rating is pure
    BASE_RATING (1500), not an actual estimate of their skill. Used to gate
    model_prob (see tennis_markets.py) -- REAL FINDING (2026-07-19, user-
    reported): a genuinely 0-match player facing a real, rated opponent
    below 1500 produces a mathematically "correct" but practically
    meaningless high win probability, since the 1500 side isn't actually
    known to be average, just unmeasured. Validated with a real walk-forward
    check before picking this as the cutoff (not guessed): bucketing the
    full backtest by the less-experienced player's real prior-match count,
    Elo's own Brier gap vs. the market is 0.073 at 0 prior matches vs. 0.046
    at 1-2 and a much smaller 0.014 once both players have 50+ -- the 0-vs-
    any-real-history jump is by far the steepest cliff in the data, more
    than double the very next bucket, which is why 0 (not some larger
    minimum) is the line drawn here."""
    state = _cache.get("state")
    if state is None:
        return 0
    return state.overall_counts.get(player_key, 0)


def get_match_win_prob(player_a_key: str, player_b_key: str, surface: str | None = None) -> float | None:
    """Player_a's win probability off CURRENT ratings -- does NOT update
    anything (live scoring, not training)."""
    state = _cache.get("state")
    if state is None:
        return None
    a_r = state.blended_rating(player_a_key, surface)
    b_r = state.blended_rating(player_b_key, surface)
    return win_prob(a_r, b_r)

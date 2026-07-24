"""In-process cache of current UFC fighter Elo ratings -- parallel to
elo_service_mlb.py, but sourced from the offline historical scrape
(data/ufc_fight_cache.json, see scripts/build_ufc_fight_cache.py) rather
than a live DB table -- MmaFight in the DB only ever holds a handful of
upcoming, not-yet-fought cards (see poller_mma.py::refresh_mma_fights),
never the full fight history needed to train ratings from scratch.

Known, documented limitation (not fixed yet): ratings go stale for any
fighter whose most recent fight happened AFTER the historical cache was
last rebuilt -- there's no incremental "append this week's results" path
yet, only a full re-run of scripts/build_ufc_fight_cache.py. Flagged here
rather than silently ignored; worth adding an incremental-append mode to
that script if this app starts scoring live cards regularly.

Same "not validated to beat the market" status as every other sport's Elo
in this app -- see elo_mma.py's own docstring for the real, honestly-modest
accuracy this module's ratings achieve (56.2% pure win/loss, before the
validated age adjustment; the age adjustment is a real, checked-in-isolation
AND checked-to-actually-improve-Brier addition, not a guess -- see
elo_mma.py's own docstring for the full derivation).
"""
import datetime as dt
import logging

from app.ingestion import ufc_data
from app.models import mma_features
from app.models.baseline.elo_mma import MmaEloState, age_adjustment_elo, predict_and_update, win_prob

log = logging.getLogger("elo_service_mma")

_cache: dict = {"state": None, "bios": None}


def refresh_ratings():
    fights = ufc_data.load_fights()
    bios = ufc_data.load_fighter_bios()
    state = MmaEloState()
    for f in fights:
        predict_and_update(state, f)  # ages omitted here on purpose -- training the RATING itself is age-agnostic (age only shifts the returned prediction, see elo_mma.py), and this loop doesn't use the returned prediction at all
    _cache["state"] = state
    _cache["bios"] = bios
    log.info("mma elo ratings refreshed: %d fighters rated, %d fights", len(state.ratings), len(fights))


def get_fighter_rating(fighter_id: str) -> float | None:
    state = _cache.get("state")
    if state is None:
        return None
    return state.get(fighter_id)


def get_current_age(fighter_id: str) -> float | None:
    """Fighter's real age as of today, from their ufcstats bio DOB -- used
    both internally (get_fight_win_prob's age adjustment) and by the
    reasoning endpoint (mma_markets.py) to display the same number it was
    computed with, rather than recomputing it a second way."""
    bios = _cache.get("bios") or {}
    dob = mma_features.parse_dob(bios.get(fighter_id, {}).get("dob"))
    return (dt.date.today() - dob).days / 365.25 if dob else None


def get_fight_win_prob(fighter_a_id: str, fighter_b_id: str) -> float | None:
    """Fighter_a's pre-fight win probability off CURRENT ratings, age-
    adjusted using each fighter's real age as of today -- does NOT update
    anything (live scoring, not training)."""
    state = _cache.get("state")
    if state is None:
        return None
    a_r = state.get(fighter_a_id) + age_adjustment_elo(get_current_age(fighter_a_id))
    b_r = state.get(fighter_b_id) + age_adjustment_elo(get_current_age(fighter_b_id))
    return win_prob(a_r, b_r)

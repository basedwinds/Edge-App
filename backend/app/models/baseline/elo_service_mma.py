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
    anything (live scoring, not training).

    None if EITHER fighter has never fought in the rating history. MmaEloState.get
    silently returns BASE_RATING (1500) for an unknown id, which is fine while
    training but is a fabricated input at scoring time, and it was reaching the
    UI as if it were a real read:

      * both fighters unrated -> 1500 v 1500 -> exactly 0.500. Against Kalshi's
        0.30 on Giovanna Canuto (a UFC debutant) that showed as a +20.5pp edge
        and drew a real $10 suggested stake -- a coin flip dressed up as a model.
      * one unrated -> WORSE, because it looks confident rather than neutral: a
        1650-rated veteran against a debutant defaulted to 1500 returns ~0.71,
        with nothing behind the 1500 at all. The debutant may be an undefeated
        regional champion; the model has simply never heard of them.

    24 active moneyline markets were in that state when this was added (six
    debutants across the August cards). Returning None routes them to the
    normal "no baseline yet" path, which is the honest answer.

    Fighters with 1-2 prior fights are deliberately still priced: their rating
    is noisy but it is at least DERIVED FROM RESULTS. That was a hypothesis when
    written; it has since been MEASURED and a minimum-fights floor was REJECTED
    (scripts/check_mma_min_fights_threshold.py, 8,624 walk-forward fights):

        min prior fights   n      Brier (vs .25 uninformed)   accuracy
        1-2                2215   0.2374  [0.2326, 0.2420]    60.2%  [58.2, 62.3]
        3-5                1681   0.2365  [0.2307, 0.2425]    59.4%
        6-10               1329   0.2330  [0.2270, 0.2391]    60.0%

    The 1-2 bucket is among the BEST buckets on accuracy, and its Brier CI sits
    entirely below the uninformed 0.25. Blocking it would throw away real signal.

    The 0-prior guard above is separately confirmed by the same run. Splitting
    that bucket in two:

        both unrated (0 v 0)        347   0.2463  [0.2385, 0.2539]   52.9%  [47.7, 58.2]
        one unrated, other 10+      146   0.2485  [0.2272, 0.2701]   53.4%  [45.9, 61.6]

    Both CIs touch 0.25 and both accuracy CIs span 50% -- no measurable skill,
    which is exactly what a 1500 stand-in should produce. The second row is the
    case flagged above as the WORSE one, and the data agrees: the bigger the
    experience gap, the more confident the number and the less it knows.

    Honest limit: those are skill-vs-uninformed comparisons, not edge-vs-market.
    ufcstats carries no odds and no free structured historical MMA odds source
    exists, so the house rule of validating against the market cannot be met
    here yet -- the app's own tracker held 35 settled MMA moneylines across 3
    days when this was run. That is also why "one unrated vs a rated opponent"
    is NOT unblocked despite showing aggregate skill (n=1290, 58.9%): beating a
    coin flip is not grounds to stake, and the high-gap half of that same group
    shows nothing at all.
    """
    state = _cache.get("state")
    if state is None:
        return None
    if fighter_a_id not in state.ratings or fighter_b_id not in state.ratings:
        return None
    a_r = state.get(fighter_a_id) + age_adjustment_elo(get_current_age(fighter_a_id))
    b_r = state.get(fighter_b_id) + age_adjustment_elo(get_current_age(fighter_b_id))
    return win_prob(a_r, b_r)

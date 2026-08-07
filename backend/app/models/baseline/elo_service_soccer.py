"""In-process cache of current Soccer team attack/defense ratings -- parallel
to elo_service_tennis.py, sourced from the offline historical match cache
(app/ingestion/soccer_data.py::load_matches(), backed by
data/football_data_matches_cache.json + data/espn_mls_matches_cache.json,
see scripts/build_soccer_match_cache.py) rather than a live DB table.

ONE SoccerRatingState per league (E0/SP1/I1/D1/F1/MLS), NOT a single shared
pool like Tennis's cross-tier player Elo -- an EPL team's attack rating isn't
comparable to a Ligue 1 team's without cross-league goal-scoring-rate
normalization, which is out of scope for v1 (see the approved build plan).

Same known limitation as elo_service_tennis.py: ratings go stale for any
team whose most recent match happened after the historical cache was last
rebuilt -- no incremental "append this week's results" path yet, only a
full re-run of the cache-builder script.

model_validated: false, same as every other sport's baseline in this app,
until scripts/backtest_moneyline_soccer.py's go/no-go check runs against
real historical odds -- and even a GO result there can only ever apply to
the 5 football-data.co.uk-sourced leagues, never MLS (no free odds source
exists for it, see SoccerMatch's docstring in app/db/models.py).

Every team-name argument here (and every training-data team name) is run
through market_matcher_soccer.py::canonical_team_key before touching the
rating dict -- REAL BUG this fixes (caught live 2026-07-19, this app's own
first end-to-end poller run): a raw exact-string lookup silently treated
"Houston Dynamo" (a live Polymarket listing's own spelling) as a completely
different, 0-history team from "Houston Dynamo FC" (ESPN's spelling, the
actual training data), for every MLS team whose platform-rendered name
wasn't byte-identical to ESPN's. Canonicalizing on BOTH sides (train-time
AND lookup-time) is what actually closes this, not just canonicalizing one
side."""
import logging

from app.ingestion import soccer_data
from app.ingestion.market_matcher_soccer import canonical_team_key
from app.models.baseline.elo_soccer import MatchGoalDistribution, SoccerRatingState, predict_and_update, predict_half, predict_match

log = logging.getLogger("elo_service_soccer")

_cache: dict = {"states_by_league": {}}


# Exhibition squads are not clubs. football-data's MLS feed includes the
# All-Star Game, whose participants are "MLS All-Stars" / "Liga MX All-Stars"
# plus a visiting European club -- 5 matches, all MLS.
#
# REAL CONTAMINATION this removes (found 2026-08-06 while mapping cross-league
# bridges, which is how it surfaced: Arsenal and Atletico Madrid showed up as
# the ONLY "teams" shared between MLS and E0/SP1/SP2): both clubs were carrying
# a rating inside the MLS pool built from a single blowout exhibition -- Arsenal
# from one 5-0, Atletico from one 3-0 -- and every real MLS side that played an
# All-Star squad had its own rating moved by a result against a team that does
# not exist. season_sim_wnba already guards the identical case ("ESPN tags the
# All-Star game REG too ... restrict to real franchises"); soccer did not.
_EXHIBITION_MARKERS = ("all-stars", "all stars", "allstars")


def _is_exhibition(match: dict) -> bool:
    return any(
        marker in (match.get(side) or "").lower()
        for side in ("home_team", "away_team")
        for marker in _EXHIBITION_MARKERS
    )


def refresh_ratings():
    matches = [m for m in soccer_data.load_matches() if not _is_exhibition(m)]
    states: dict[str, SoccerRatingState] = {}
    for m in matches:
        league = m["league"]
        state = states.setdefault(league, SoccerRatingState())
        canonical_match = {
            **m,
            "home_team": canonical_team_key(m["home_team"]),
            "away_team": canonical_team_key(m["away_team"]),
        }
        predict_and_update(state, canonical_match)
    _cache["states_by_league"] = states
    for league, state in states.items():
        log.info("soccer ratings refreshed for %s: %d teams rated", league, len(state.attack_log))


def get_team_match_count(league: str, team: str) -> int:
    """Real prior-match count this team's CURRENT rating is built from -- 0
    means they've never appeared in the offline training source for this
    league (e.g. a newly-promoted club, or a name-matching miss -- see
    market_matcher_soccer.py), so their rating is pure league-average, not an
    actual estimate of their strength. Used to gate model_prob the same way
    elo_service_tennis.py::get_player_match_count gates Tennis's 0-history
    case (real, validated finding there: an unmeasured 1500-rated opponent
    produces a mathematically "correct" but practically meaningless
    probability) -- not yet re-validated specifically for Soccer's 0-match
    case in this app's own backtest, but the same underlying reasoning
    applies directly (a league-average PRIOR is not a real per-team
    estimate) so the gate ships from day one rather than waiting to
    rediscover the same finding."""
    state = _cache["states_by_league"].get(league)
    if state is None:
        return 0
    return state.get_count(canonical_team_key(team))


def _either_unrated(league: str, home_team: str, away_team: str) -> bool:
    return (
        get_team_match_count(league, home_team) == 0
        or get_team_match_count(league, away_team) == 0
    )


def get_match_distribution(league: str, home_team: str, away_team: str) -> MatchGoalDistribution | None:
    """Home/away goal distribution off CURRENT ratings -- does NOT update
    anything (live scoring, not training). None if either team has no prior
    matches in this league's training data.

    The gate was previously in soccer_markets.py's LIST endpoint only, so the
    REASONING endpoint would happily build "Model expected goals" text and a
    narrative insight for a team the model has never seen -- two paths, two
    different answers for the same fixture. Enforcing it in the service makes
    that impossible to get wrong from any caller."""
    if _either_unrated(league, home_team, away_team):
        return None
    state = _cache["states_by_league"].get(league)
    if state is None:
        return None
    return predict_match(state, canonical_team_key(home_team), canonical_team_key(away_team))


def get_half_distribution(league: str, home_team: str, away_team: str, half: int) -> MatchGoalDistribution | None:
    """Same real CURRENT-ratings source as get_match_distribution, derated
    to one half via elo_soccer.py::predict_half (added 2026-07-19 for the
    First Half/Second Half market family -- see that function's own
    docstring for the real first-half-goal-share constant behind this).
    Same unrated-team gate as get_match_distribution."""
    if _either_unrated(league, home_team, away_team):
        return None
    state = _cache["states_by_league"].get(league)
    if state is None:
        return None
    return predict_half(state, canonical_team_key(home_team), canonical_team_key(away_team), half)


def get_rating_state(league: str) -> SoccerRatingState | None:
    """Raw state access for callers that need more than the single-match
    read APIs above -- currently only season_sim_soccer.py's Monte Carlo,
    which needs the full attack/concede rating dict to simulate every
    pairing in a league, not just one match at a time. Caller is
    responsible for canonicalizing team names (via canonical_team_key)
    before using them against this state -- the dict keys here are
    ALWAYS canonical, same as every other function in this module."""
    return _cache["states_by_league"].get(league)

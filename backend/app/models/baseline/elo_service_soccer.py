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
import json as _json
import logging
import re as _re
from pathlib import Path as _Path

from app.ingestion import soccer_data
from app.ingestion.cache_memo import memoize_on_files as _memoize_on_files
from app.ingestion.market_matcher_soccer import canonical_team_key
from app.models.baseline.elo_soccer import (
    MatchGoalDistribution,
    SoccerRatingState,
    home_advantage_for_league,
    predict_and_update,
    predict_half,
    predict_match,
)

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
    last_played: dict[tuple[str, str], str] = {}
    for m in matches:
        league = m["league"]
        state = states.get(league)
        if state is None:
            # Home advantage is per-league where a league earned its own
            # validated term (elo_soccer.home_advantage_for_league); everything
            # else gets the global constant. Built HERE, at the one place a
            # league's state is created, so no caller can construct a state
            # that silently reverts to the global value.
            state = states[league] = SoccerRatingState(home_log=home_advantage_for_league(league))
        canonical_match = {
            **m,
            "home_team": canonical_team_key(m["home_team"]),
            "away_team": canonical_team_key(m["away_team"]),
        }
        date = m.get("match_date") or ""
        for side in ("home_team", "away_team"):
            key = (league, canonical_match[side])
            if date > last_played.get(key, ""):
                last_played[key] = date
        predict_and_update(state, canonical_match)
    _cache["states_by_league"] = states
    _cache["last_played"] = last_played
    for league, state in states.items():
        log.info("soccer ratings refreshed for %s: %d teams rated", league, len(state.attack_log))


# How far back a club's last appearance in a division may be before its rating
# there is treated as dead rather than current.
#
# REAL BUG THIS EXISTS FOR (found 2026-08-08 while smoke-testing cup pricing).
# This cache holds three decades of football-data, so a club keeps a rating in
# every division it has EVER played in, forever. Two things went wrong at once:
#
#   * STALE: L.R. Vicenza was priced off its Serie A rating. Vicenza's last
#     Serie A match in this cache is 2001-06-17. It is a Serie C club now.
#     Catania (last I1 2014) and Ravenna (last I2 2008) were the same.
#   * WRONG TIER: a club with ratings in BOTH divisions was resolved by dict
#     iteration order, not by which one it currently plays in. Monza has a 2025
#     Serie A rating AND a 2026 Serie B rating, and got priced as a Serie A
#     club against a Serie B opponent -- overrating it, and mislabelling the tie
#     as cross-tier when it was not. Padova had a 1996 Serie A rating competing
#     with its current Serie B one.
#
# Neither failure is visible in the output: both produce a confident, plausible
# probability for the wrong club strength. Two seasons is the tolerance because
# a club can miss a single season through relegation and return; three years
# means it is gone.
MAX_RATING_STALENESS_DAYS = 730


def last_played(league: str, team: str) -> str | None:
    return (_cache.get("last_played") or {}).get((league, canonical_team_key(team)))


# parents[4] is the repo root: .../backend/app/models/baseline/ -> 4 up. Getting
# this wrong makes _load_aliases() swallow the OSError and return {}, which is a
# SILENT no-op -- the disambiguation below just never fires. Asserted at import
# rather than trusted.
_ALIAS_PATH = _Path(__file__).resolve().parents[4] / "data" / "soccer_espn_aliases.json"


def _country_of(league: str) -> str:
    """The country a league code belongs to, i.e. the code with its division
    number stripped: E0/E1/E2/E3 -> E, SP1/SP2 -> SP, URU1 -> URU."""
    return _re.sub(r"\d+$", "", league or "")


@_memoize_on_files(lambda: [_ALIAS_PATH])
def _load_aliases() -> dict:
    """Verified ESPN->pool alias map (scripts/build_soccer_espn_aliases.py),
    derived from date-aligned fixtures. Each entry carries BOTH a team name and
    the league it was verified in; the league is what disambiguates a colliding
    name. Read-only -- shared object."""
    try:
        return _json.loads(_ALIAS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # LOUD, because the failure is not. An unreadable alias file makes the
        # cross-country disambiguation below silently stop firing, and the only
        # symptom is Liverpool quietly resolving to Uruguay again. A wrong
        # parents[] index did exactly this on the first cut of this function.
        log.error("soccer aliases unreadable at %s (%s) -- cross-country name "
                  "collisions will resolve by recency and can pick the wrong "
                  "country", _ALIAS_PATH, exc)
        return {}


def resolve_league(team: str, as_of: str | None = None,
                   max_staleness_days: int = MAX_RATING_STALENESS_DAYS) -> str | None:
    """Which division a club should currently be rated in -- the one it played
    in MOST RECENTLY, and only if that was recent enough to still describe the
    club. Returns None rather than a stale or arbitrary answer.

    This lives in the service, not in each caller, because the callers are
    exactly where it was already got wrong once.

    CROSS-COUNTRY COLLISIONS ARE BROKEN BY THE ALIAS MAP, NOT BY RECENCY.
    canonical_team_key is not unique across leagues: "liverpool" is both
    Liverpool FC and Liverpool Montevideo, "nacional" is both Nacional Madeira
    and Nacional de Montevideo. Recency is the RIGHT tie-break inside one
    country -- that is how a promoted club moves E1 -> E0 -- but across
    countries it just picks whoever played last, and a year-round Uruguayan
    season beats an off-season Premier League club. Measured 2026-08-13: of 282
    colliding club keys, 272 collide only across divisions of one country (the
    intended case) and 10 collide across countries (the bug).

    So the alias map's verified league is consulted ONLY when the hits actually
    span more than one country, which leaves the 272 same-country cases byte
    identical. The alias league is used to pick the COUNTRY, not the division,
    because the alias records where a club was when it was verified and it may
    since have been promoted or relegated.

    Found via the UEFA strength re-fit, which ranked Uruguay above England
    because Liverpool's Champions League results were being credited to
    Liverpool Montevideo.
    """
    import datetime

    key = canonical_team_key(team)
    played = _cache.get("last_played") or {}
    if not played and _cache.get("states_by_league"):
        # Ratings are loaded but last-played is not. That means this process
        # populated its cache from a build BEFORE last_played existed, and every
        # call here will refuse every club SILENTLY -- cup pricing just returns
        # empty with no error anywhere. Seen for real on 2026-08-08: the live
        # worker served 191 cup rows all unpriced while the same code priced 9
        # of them fine in a fresh process. Loud, because the failure is not.
        log.error("soccer resolve_league: ratings loaded but last_played is EMPTY -- "
                  "stale in-process cache, refresh_ratings() must be re-run or every "
                  "club will be refused")
        return None
    hits = [(date, league) for (league, t), date in played.items() if t == key and date]

    if not hits:
        # NAME TRANSLATION, the alias map's other job. A feed may simply spell a
        # club differently from the training data -- ESPN's UEFA scoreboard says
        # "Olympiacos" where its own domestic Greek feed (and football-data) say
        # "Olympiakos". With no hits there is no collision to break, so the
        # disambiguation below never runs and the club looks unrated. Retry once
        # under the verified alias name before giving up.
        entry = _load_aliases().get(team)
        alias_team = (entry or {}).get("team")
        if alias_team:
            akey = canonical_team_key(alias_team)
            if akey != key:
                hits = [(date, league) for (league, t), date in played.items()
                        if t == akey and date]
    if not hits:
        return None

    # Only intervene when the name genuinely spans countries; same-country
    # collisions are promotion/relegation and recency already handles them.
    if len({_country_of(lg) for _, lg in hits}) > 1:
        entry = _load_aliases().get(team) or _load_aliases().get(canonical_team_key(team))
        alias_league = (entry or {}).get("league")
        if alias_league:
            same_country = [h for h in hits if _country_of(h[1]) == _country_of(alias_league)]
            if same_country:
                hits = same_country

    date, league = max(hits)
    try:
        seen = datetime.date.fromisoformat(date)
    except ValueError:
        return None
    ref = datetime.date.fromisoformat(as_of) if as_of else datetime.date.today()
    if (ref - seen).days > max_staleness_days:
        return None  # club has left every division this app models
    return league


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

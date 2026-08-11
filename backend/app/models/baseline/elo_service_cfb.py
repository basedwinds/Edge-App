"""In-process cache of current college-football Elo ratings -- parallel to
elo_service_wnba.py, with one structural difference that matters.

WNBA/NBA rebuild ratings purely from their DB game rows, which is fine because
those tables hold whole seasons. CFB CANNOT do that: the poller only keeps a
~90-day window (espn_cfb_client.FORWARD_DAYS), so the DB never contains prior
seasons -- and elo_cfb ships SEASON_REGRESSION = 0.0 precisely because college
ratings are supposed to CARRY FORWARD year over year. Replaying DB rows alone
would start every team at 1500 in week 1 and throw away the exact signal the
constants were validated on.

So the replay is seeded from data/cfb_game_cache.json (4,836 FBS games,
2021-2025 -- the same file the constants were derived from) and then continues
through the current season's DB rows. Games already present in the cache are
skipped by id when the DB rows are applied, so a game cannot be counted twice
if the window overlaps the cache's tail.
"""
import json
import logging
from pathlib import Path

from app.db.database import SessionLocal
from app.db.models import CfbGame
from app.models.baseline.elo_cfb import (
    EloState,
    effective_home_field_adv,
    update_ratings,
    win_prob,
)

log = logging.getLogger("elo_service_cfb")

# parents[4] == the repo root (this file is backend/app/models/baseline/), so the
# cache resolves to <repo>/data. parents[3] would point at backend/data, which
# does not exist -- same path racing_ratings.py uses.
_DATA_DIR = Path(__file__).resolve().parents[4] / "data"
_CACHE_FILE = _DATA_DIR / "cfb_game_cache.json"

_cache: dict = {"state": None, "fbs_share": {}}


def _historical_games() -> list[dict]:
    """Prior-season games from the derivation cache. Empty (with a warning) if
    the file is missing -- the model still runs, it just starts flat, which is
    worth a log line rather than a silent quality drop."""
    try:
        raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.warning("cfb elo: historical cache unreadable at %s -- ratings will start flat", _CACHE_FILE)
        return []
    rows = list(raw.values()) if isinstance(raw, dict) else list(raw)
    out = []
    for g in rows:
        if g.get("home_score") is None or g.get("away_score") is None:
            continue
        out.append({
            "id": str(g.get("id")),
            "season": int(g["season"]),
            "gameday": g["date"],
            "home_team": g["home_abbr"],
            "away_team": g["away_abbr"],
            "home_score": g["home_score"],
            "away_score": g["away_score"],
            "neutral": 1 if g.get("neutral") else 0,
        })
    return out


def _db_games() -> list[dict]:
    session = SessionLocal()
    try:
        return [
            {
                "id": g.id, "season": g.season, "gameday": g.gameday,
                "home_team": g.home_team, "away_team": g.away_team,
                "home_score": g.home_score, "away_score": g.away_score,
                "neutral": g.neutral or 0,
            }
            for g in session.query(CfbGame).filter(CfbGame.game_type.in_(("REG", "POST"))).all()
        ]
    finally:
        session.close()


# Below this share of a team's games played against opponents in a mapped FBS
# conference, its rating is not on the same scale as the rest of the league.
#
# Elo only makes two ratings comparable when the pool is CONNECTED: strength has
# to flow between teams through actual results. A programme that has just moved
# up from FCS carries a rating earned almost entirely inside the division it
# left. Measured on the cache: NDSU is 63-11 across 74 games with SIX of those
# 74 opponents in an FBS conference (8%), median opponent Elo 1432 -- against
# UNLV at 92% and Ohio State at 96%, median opponent 1753. Its 1849 is a real
# number about a different population.
#
# Exactly 5 of 138 mapped teams fall below this line, and all five are recent
# FCS-to-FBS moves (NDSU, SAC, DEL, MOST, KENN) -- a coherent group, not noise.
#
# NOT a rating adjustment. Shrinking toward the mean was considered and
# rejected: the bias could not be measured. Poorly-connected teams play so few
# FBS games that the sample is n=54, and the neighbouring connectivity band
# moves the opposite way (+7.1pp), while well-connected teams (n=1,915) are
# calibrated to 0.0pp. There is no honest magnitude to fit, so this flags the
# rating as out-of-scale and lets the caller decline to stake it, rather than
# inventing a correction.
MIN_FBS_CONNECTIVITY = 0.50


def fbs_connectivity(team: str) -> float | None:
    """Share of this team's played games that were against a team in a mapped
    FBS conference. None when the team has no games on record."""
    return _cache.get("fbs_share", {}).get(team)


def is_weakly_connected(team: str) -> bool:
    """True when this team's rating was built mostly outside the FBS pool, so it
    is not comparable to the ratings it will be priced against."""
    share = fbs_connectivity(team)
    return share is not None and share < MIN_FBS_CONNECTIVITY


def refresh_ratings():
    hist = _historical_games()
    seen = {g["id"] for g in hist}
    # De-dupe by ESPN event id: the poller's back-window can overlap the cache's
    # tail, and replaying a game twice would double its rating impact.
    live = [g for g in _db_games() if g["id"] not in seen]
    games = hist + live
    games.sort(key=lambda g: (g["season"], g["gameday"], g["id"]))

    state = EloState()
    applied = 0
    for g in games:
        state.start_season_if_new(g["season"])
        if g.get("home_score") is not None and g.get("away_score") is not None:
            adv = effective_home_field_adv(bool(g.get("neutral")))
            update_ratings(state, g["home_team"], g["away_team"], g["home_score"], g["away_score"], adv)
            applied += 1
    # Connectivity to the FBS pool, computed off the same replay so it can never
    # disagree with the ratings it qualifies.
    try:
        from app.models.season_sim_cfb import load_conferences
        conf = load_conferences()
    except Exception:
        conf = {}
    if conf:
        played: dict[str, int] = {}
        vs_fbs: dict[str, int] = {}
        for g in games:
            if g.get("home_score") is None or g.get("away_score") is None:
                continue
            for me, opp in ((g["home_team"], g["away_team"]), (g["away_team"], g["home_team"])):
                played[me] = played.get(me, 0) + 1
                if conf.get(opp):
                    vs_fbs[me] = vs_fbs.get(me, 0) + 1
        _cache["fbs_share"] = {t: vs_fbs.get(t, 0) / n for t, n in played.items() if n}
    _cache["state"] = state
    log.info(
        "cfb elo ratings refreshed: %d teams rated from %d games (%d historical + %d live)",
        len(state.ratings), applied, len(hist), len(live),
    )


# --- CROSS-TIER CORRECTION -----------------------------------------------------
# P5 conferences. Everything else FBS (American, C-USA, MAC, Mountain West, Sun
# Belt) plus the Independents is treated as the other pool.
_P5_CONFERENCES = frozenset({
    "Atlantic Coast Conference", "Big 12 Conference", "Big Ten Conference",
    "Pac-12 Conference", "Southeastern Conference",
})

# Elo points added to the P5 side of a P5-vs-non-P5 game.
#
# WHY THIS EXISTS. G5/FCS teams play overwhelmingly each other, so their rating
# pool floats free of the P5 scale and nothing inside it ever pulls it back.
# Measured walk-forward over 4,836 real games (2021-2025), predictions taken
# only once both teams had 8+ games:
#
#     cross-tier, oriented on the G5 side   n= 578  pred 0.3460 actual 0.2474  +9.86pp
#     G5 at home vs P5                      n= 170  pred 0.5201 actual 0.4294  +9.07pp
#     P5 at home vs G5                      n= 408  pred 0.7265 actual 0.8284 -10.19pp
#     G5 v G5                               n=1023  pred 0.5803 actual 0.5543  +2.61pp
#     P5 v P5                               n=1449  pred 0.5854 actual 0.5549  +3.05pp
#
# The two cross-tier rows are MIRROR IMAGES -- the sign flips with orientation
# and the magnitude holds -- which is pool drift, not noise. Same-tier games
# carry only the ~3pp home-favourite tilt present everywhere, so the defect is
# specifically cross-tier. `is_weakly_connected` does not catch it (it returns
# False for CONN, an Independent rated 1639 against a mostly-G5 slate).
#
# 100 is an INTERIOR optimum, fitted on the 637 cross-tier games:
#
#     D      pred   actual    bias     brier
#     0     0.6596  0.6986  -0.0390   0.16824
#    75     0.6779  0.6986  -0.0207   0.16031
#   100     0.6827  0.6986  -0.0159   0.15996   <- best
#   125     0.6868  0.6986  -0.0118   0.16060
#   150     0.6904  0.6986  -0.0082   0.16215
#
# Leave-one-season-out: 4 of 4 held-out seasons improve over D=0 (picks
# 100/75/100/100). Bias -3.9pp -> -1.6pp, Brier ~5% relative.
#
# PREDICTION-TIME ONLY. refresh_ratings() updates Elo from the UNADJUSTED
# probability, deliberately: correcting a pool and then feeding the correction
# back into that same pool's ratings would re-scale it against itself and the
# offset would drift toward zero.
CROSS_TIER_ELO_ADJ = 100.0
_conferences_cache: dict | None = None


def _conference_of(team: str) -> str | None:
    """Lazy read of data/cfb_conferences.json. Loaded HERE rather than imported
    from season_sim_cfb because that module imports this one."""
    global _conferences_cache
    if _conferences_cache is None:
        import json
        from pathlib import Path
        # parents[4] is the REPO ROOT from backend/app/models/baseline/ --
        # parents[3] is `backend`, which has no data/ dir. Caught by testing the
        # lookup rather than the path: every adjustment silently returned 0.
        p = Path(__file__).resolve().parents[4] / "data" / "cfb_conferences.json"
        try:
            # The FILE is {conference: [team abbreviations]} -- it has to be
            # INVERTED to {abbr: conference}, the same way
            # season_sim_cfb.load_conferences does. Reading it raw made every
            # lookup return None and every adjustment silently 0, which is
            # exactly what a broken correction looks like from the outside:
            # nothing changes and nothing errors.
            raw = json.loads(p.read_text(encoding="utf-8"))
            _conferences_cache = {abbr: conf for conf, abbrs in raw.items()
                                  for abbr in (abbrs or []) if abbr}
        except Exception:
            log.warning("cfb conferences unreadable; cross-tier correction disabled")
            _conferences_cache = {}
    return _conferences_cache.get(team)


def cross_tier_adjustment(home_team: str, away_team: str) -> float:
    """Elo to add to the HOME side. 0 unless exactly one side is P5.

    Fails to 0 for any team whose conference is unknown, so an unmapped name
    can never invent an adjustment."""
    ch, ca = _conference_of(home_team), _conference_of(away_team)
    if ch is None or ca is None:
        return 0.0
    hp5, ap5 = ch in _P5_CONFERENCES, ca in _P5_CONFERENCES
    if hp5 == ap5:
        return 0.0
    return CROSS_TIER_ELO_ADJ if hp5 else -CROSS_TIER_ELO_ADJ
# -------------------------------------------------------------------------------


def get_home_win_prob(home_team: str, away_team: str, neutral: bool = False) -> float | None:
    """P(home team wins). None when ratings aren't warm yet -- callers leave the
    market unpriced rather than pricing off a cold cache.

    THE SINGLE CHOKE POINT for CFB win probability: game markets, the season
    win-total sim and the conference sim all price through here (season_sim_cfb
    calls it in both simulate() and simulate_conferences()). That is why the
    cross-tier correction lives here and not at a call site -- the `rating()`
    use in simulate() is only a ranking TIEBREAK, not a price."""
    state = _cache.get("state")
    if state is None:
        return None
    adv = effective_home_field_adv(neutral) + cross_tier_adjustment(home_team, away_team)
    return win_prob(state.get(home_team), state.get(away_team), adv)


def rating(team: str) -> float | None:
    state = _cache.get("state")
    return None if state is None else state.get(team)


def is_rated(team: str) -> bool:
    """Whether this team has ever been seen. A team absent from both the cache
    and the DB gets BASE_RATING from EloState.get, which would silently price an
    unknown FCS opponent as league-average -- callers should check this first."""
    state = _cache.get("state")
    return bool(state and team in state.ratings)

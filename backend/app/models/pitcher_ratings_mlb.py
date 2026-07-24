"""Starting-pitcher quality signal for the MLB baseline model -- unlike
NFL/NBA, MLB's single-game outcome is dominated by the starting-pitcher
matchup, not just team strength (538's own public MLB Elo methodology blends
team Elo with a separate starter rating for this reason). Checked for a
REAL, non-redundant signal on this app's own historical data BEFORE being
wired in (see scripts/check_mlb_pitcher_signal.py), same "verify before
build" discipline as this project's NFL EPA-mismatch validation:

- era_diff (away starter's ERA minus home starter's ERA, so positive favors
  the home team) had a POSITIVE logistic-regression coefficient in 10/10
  seasons (2016-2025) when fit jointly with elo_diff, and correlates with
  elo_diff at only r=0.305 -- real signal, not redundant with team Elo.
- The full blended model (this module's adjustment folded into elo_mlb.py's
  win_prob) beat team-Elo-alone on the SAME walk-forward games: Brier
  0.24250 vs 0.24291 (n=15,371) -- a real, if modest, improvement, matching
  the "real but modest" magnitude of every other validated signal in this
  app (EPA-mismatch, divisional-total-suppression, etc.), not a claimed
  edge over the market.

Treated as a BASELINE/structural input (folded directly into elo_mlb's
win_prob, same category as Denver altitude/neutral-site handling), not a
situational/news-layer factor -- the starting pitcher is a known, confirmed
fact well before gametime (MLB Stats API's probablePitcher hydrate,
confirmed live days ahead), not an uncertain in-flight signal.

PRIOR-SEASON FALLBACK (2026-07-17, added after auditing coverage): the
current-season-only version above left 32.6% of games with NO pitcher signal
at all (n=22,764) -- 13.5% too early in the season for any snapshot to
exist yet, 19% where at least one starter (rookie call-up, mid-season trade,
return from injury/IL) hadn't cleared MIN_IP this season. Checked whether
falling back to that pitcher's FINAL prior-season ERA (when it exists and
clears MIN_IP) carries real signal before building it, same discipline as
the original signal: real but weaker correlation (r=0.075 vs outcome, 0.093
vs margin, vs 0.089/current-season's own check) on the 3,842 rescued games,
and a pooled logistic regression against elo_diff gives a real, smaller
conversion factor (6.02 Elo points/ERA-diff-unit vs 9.73 for current-season
data -- stale-by-a-year data reasonably carries less weight). Confirmed the
full blend beats team-Elo-alone on exactly those 3,842 rescued games (Brier
0.2419 vs 0.2422) before shipping -- real, if smaller than the current-
season signal's own improvement, matching this app's "real but modest"
pattern throughout. Applied ONLY when current-season data is unavailable/
too thin, never blended with or overriding real current-season data.
"""
import datetime as dt

from app.clients import statsapi_mlb_client

MIN_IP = 15.0  # below this, a single-season ERA is too noisy to trust (same threshold used in the validation script)

# IP-weighted starter-only ERA across all of 2025 (this app's own cached
# data, not a guessed "modern era average") -- used only to CAP absurd
# small-sample ERA outliers (e.g. a starter's first outing being a 1-inning
# 9-run disaster), not as a rating input itself. Recent-season range checked
# was 4.02-4.54 (2019/2022/2025) -- picked the most recent real season
# rather than averaging across a range that includes rule-change eras
# (universal DH 2022, pitch clock 2023).
LEAGUE_AVG_ERA = 4.17
ERA_OUTLIER_CAP = LEAGUE_AVG_ERA * 3  # matches the validation script's cap

# Derived from a pooled logistic regression of outcome ~ elo_diff + era_diff
# on all 15,371 qualifying games 2016-2025 (raw, non-standardized units --
# see check_mlb_pitcher_signal.py): coef_era / coef_elo = 9.73. Used at full
# strength (not discounted to a "conservative fraction" the way the smaller-
# sample Denver-altitude/Utah-altitude bonuses were) since this fit pools
# 15k+ games with 100%-consistent-sign per-season coefficients -- a
# meaningfully larger, more stable sample than those single-team estimates.
ERA_DIFF_TO_ELO_POINTS = 9.73

# Prior-season-fallback conversion factor -- derived the SAME way (pooled
# logistic regression) but on the 3,842 games where only a prior-season ERA
# was available, n too small and the signal too era-stale to reuse the
# current-season coefficient blindly. See module docstring.
ERA_DIFF_TO_ELO_POINTS_PRIOR_SEASON = 6.02

# Conservatism lever instead: a hard cap on the total Elo-point swing one
# game's starters can contribute (~1.8x HOME_FIELD_ADV) -- bounds the effect
# of any one still-small in-season sample rather than discounting the fitted
# slope itself, matching EPA-mismatch's own "cap the ceiling, not the
# coefficient" pattern in epa_mismatch_rules.py. Same cap for both current-
# and prior-season data (the smaller PRIOR_SEASON coefficient already bounds
# typical swings lower; this remains the hard backstop for both).
MAX_PITCHER_ELO_POINTS = 40.0


def pitcher_elo_adjustment(
    home_era: float | None, away_era: float | None, home_ip: float, away_ip: float,
    home_is_prior_season: bool = False, away_is_prior_season: bool = False,
) -> float:
    """Returns an Elo-point adjustment (added to the home team's effective
    rating before win_prob) -- 0.0 if either starter has too little
    innings (current OR prior season, whichever's stats were passed in) to
    trust their ERA, or ERA data is missing entirely (unknown = no
    adjustment, not a guessed default). If EITHER side's ERA came from a
    prior season, the whole game's adjustment uses the smaller, prior-
    season-calibrated conversion factor -- simpler and more conservative
    than trying to blend two different factors within one game, and the
    common case (one fresh rookie starter vs. an established current-season
    starter) is exactly the situation this app has the LEAST current-season
    read on."""
    if home_era is None or away_era is None:
        return 0.0
    if home_ip < MIN_IP or away_ip < MIN_IP:
        return 0.0
    home_era = min(home_era, ERA_OUTLIER_CAP)
    away_era = min(away_era, ERA_OUTLIER_CAP)
    era_diff = away_era - home_era  # positive favors home (lower ERA = better)
    conversion = ERA_DIFF_TO_ELO_POINTS_PRIOR_SEASON if (home_is_prior_season or away_is_prior_season) else ERA_DIFF_TO_ELO_POINTS
    return max(-MAX_PITCHER_ELO_POINTS, min(MAX_PITCHER_ELO_POINTS, era_diff * conversion))


class PitcherRatingCache:
    """In-process cache of pitching stats, refreshed on a TTL -- same role
    as scoring_ratings_service.py's cache for NFL. Live use pulls TWO bulk
    calls (current season + prior season, each one call for every pitcher,
    not per-pitcher) via statsapi_mlb_client.get_pitching_stats_by_date_range
    -- the prior-season call is refreshed far less often since that data
    never changes mid-season."""

    def __init__(self, ttl_seconds: int = 6 * 3600, prior_season_ttl_seconds: int = 24 * 3600):
        self._ttl = ttl_seconds
        self._prior_season_ttl = prior_season_ttl_seconds
        self._season: int | None = None
        self._stats_by_pitcher_id: dict[str, dict] = {}
        self._fetched_at: dt.datetime | None = None
        self._prior_season: int | None = None
        self._prior_stats_by_pitcher_id: dict[str, dict] = {}
        self._prior_fetched_at: dt.datetime | None = None

    def _refresh_if_stale(self, season: int, season_start: dt.date):
        now = dt.datetime.utcnow()
        stale = (
            self._fetched_at is None
            or self._season != season
            or (now - self._fetched_at).total_seconds() > self._ttl
        )
        if not stale:
            return
        today = dt.date.today()
        splits = statsapi_mlb_client.get_pitching_stats_by_date_range(
            f"{season_start:%Y-%m-%d}", f"{today:%Y-%m-%d}", season
        )
        self._stats_by_pitcher_id = _splits_to_stats(splits)
        self._season = season
        self._fetched_at = now

    def _refresh_prior_season_if_stale(self, season: int):
        prior_season = season - 1
        now = dt.datetime.utcnow()
        stale = (
            self._prior_fetched_at is None
            or self._prior_season != prior_season
            or (now - self._prior_fetched_at).total_seconds() > self._prior_season_ttl
        )
        if not stale:
            return
        # Generous full-season window (mirrors build_mlb_schedule_cache.py's
        # own March-November range) -- prior season is fully final, so the
        # exact end date doesn't matter as long as it's past the World Series.
        splits = statsapi_mlb_client.get_pitching_stats_by_date_range(
            f"{prior_season}-03-01", f"{prior_season}-11-30", prior_season
        )
        self._prior_stats_by_pitcher_id = _splits_to_stats(splits)
        self._prior_season = prior_season
        self._prior_fetched_at = now

    def get_adjustment(self, season: int, season_start: dt.date, home_pitcher_id, away_pitcher_id) -> float:
        self._refresh_if_stale(season, season_start)
        home = self._stats_by_pitcher_id.get(str(home_pitcher_id)) if home_pitcher_id else None
        away = self._stats_by_pitcher_id.get(str(away_pitcher_id)) if away_pitcher_id else None
        home_is_prior = away_is_prior = False

        if (home is None or home["ip"] < MIN_IP) and home_pitcher_id:
            self._refresh_prior_season_if_stale(season)
            prior = self._prior_stats_by_pitcher_id.get(str(home_pitcher_id))
            if prior and prior["ip"] >= MIN_IP:
                home = prior
                home_is_prior = True
        if (away is None or away["ip"] < MIN_IP) and away_pitcher_id:
            self._refresh_prior_season_if_stale(season)
            prior = self._prior_stats_by_pitcher_id.get(str(away_pitcher_id))
            if prior and prior["ip"] >= MIN_IP:
                away = prior
                away_is_prior = True

        return pitcher_elo_adjustment(
            home["era"] if home else None,
            away["era"] if away else None,
            home["ip"] if home else 0.0,
            away["ip"] if away else 0.0,
            home_is_prior, away_is_prior,
        )

    def get_combined_era(self, season: int, season_start: dt.date, home_pitcher_id, away_pitcher_id) -> float | None:
        """Average of both starters' CURRENT-SEASON ERA -- used by the RFI
        (run-in-1st-inning) model, see game_lines_mlb.py::prob_rfi. Deliberately
        does NOT fall back to prior-season data the way get_adjustment does:
        the real signal check (derive_mlb_f5_rfi_constants.py) only validated
        current-season combined ERA against real RFI outcomes, so this stays
        faithful to exactly what was checked rather than extending the fallback
        to an untested case. None (not a guess) when either starter lacks a
        current-season snapshot clearing MIN_IP -- game_lines_mlb.py::prob_rfi
        falls back to the flat league-average RFI rate in that case."""
        self._refresh_if_stale(season, season_start)
        home = self._stats_by_pitcher_id.get(str(home_pitcher_id)) if home_pitcher_id else None
        away = self._stats_by_pitcher_id.get(str(away_pitcher_id)) if away_pitcher_id else None
        if home is None or away is None or home["ip"] < MIN_IP or away["ip"] < MIN_IP:
            return None
        return (home["era"] + away["era"]) / 2.0


def _splits_to_stats(splits: list[dict]) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for s in splits:
        pid = s.get("player", {}).get("id")
        stat = s.get("stat", {})
        ip, era = stat.get("inningsPitched"), stat.get("era")
        if pid is None or ip is None or era is None:
            continue
        try:
            stats[str(pid)] = {"era": float(era), "ip": float(ip)}
        except ValueError:
            continue  # era "-.--" placeholder for 0 IP
    return stats

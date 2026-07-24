"""Free, rule-based Soccer "nothing to play for" motivation adjustment --
parallel to injury_rules_soccer.py, reusing the same NewsAdjustment/Factor
schema, but a genuinely different signal: NOT injury-driven, driven by real
league-table stakes (see app/clients/espn_soccer_client.py::fetch_standings,
confirmed live 2026-07-19: a single request per league returns the whole
real table -- rank/points/games played per team).

Real, well-documented soccer phenomenon: a team mathematically safe from
relegation AND with no realistic continental-qualification hope late in the
season has genuinely less on the line than a team still fighting for either
outcome -- same underlying "motivation gap" idea as
situational_nba.py's playoff-race logic, applied to soccer's own real table
structure (relegation zone + European-qualification zone, not a playoff
seed line).

Scope, deliberately narrow:
- Only fires in the season's real business end (games_remaining <=
  FINAL_STRETCH_GAMES_REMAINING below) -- mirrors situational_nba.py's own
  FINAL_STRETCH_MONTH gate: this effect is a late-season phenomenon, not a
  September one, and early-season standings are mostly noise anyway (early
  in the table, games_played is low and rank swings on a single result).
- Only fires when exactly ONE side has real stakes and the other doesn't --
  two teams both fighting for the same thing, or both already safe/*
  hopeless, have no real MOTIVATION DIFFERENTIAL between them even though
  both have "a lot on the line" or "nothing on the line" in an absolute
  sense.
- European-qualification zone size (EUROPEAN_ZONE_SIZE below) is a rough,
  round-number approximation (top 6 for every league) -- NOT each country's
  real, exact UEFA competition slot count, which varies by country and by
  season (UEFA coefficient rankings) and isn't fetched live here. Flagged
  as rough, same honesty tier as this app's other hand-picked situational
  constants (e.g. injury_rules_nba.py's own PPG tiers).
- MLS excluded entirely -- see espn_soccer_client.py's own docstring on why
  (conference-split table, a genuinely different real structure)."""
from app.models.news_adjustment.schema import Factor, NewsAdjustment, clamp_adjustment

# (teams - 1) * 2 for a double round-robin -- same real season lengths
# season_sim_soccer.py's own docstring already establishes for these 5
# leagues.
SEASON_LENGTH = {"E0": 38, "SP1": 38, "I1": 38, "D1": 34, "F1": 34}

# Real automatic-relegation-zone size, same values as
# season_sim_soccer.py::RELEGATION_ZONE_SIZE (not re-imported directly to
# avoid a cross-module dependency for 5 stable, well-known integers).
RELEGATION_ZONE_SIZE = {"E0": 3, "SP1": 3, "I1": 3, "D1": 2, "F1": 2}

# Rough top-N approximation of "still has continental-qualification hope"
# -- see module docstring on why this isn't each country's exact real slot
# count.
EUROPEAN_ZONE_SIZE = 6

# Real cushion beyond the exact cutoff line still counted as "in the fight"
# -- a team sitting 2-3 places outside the literal line, this late in the
# season, is still realistically playing for it, not mathematically closer
# to safe/hopeless than to contention. Round numbers, not fitted.
RELEGATION_BUFFER = 3
EUROPEAN_BUFFER = 2

FINAL_STRETCH_GAMES_REMAINING = 8  # roughly the season's final quarter for a 34-38 game season

MOTIVATION_PP = 2.0  # flat, modest -- no real historical dataset exists to fit a magnitude against


def _has_real_stakes(rank: int, team_count: int, league: str) -> bool:
    relegation_zone_size = RELEGATION_ZONE_SIZE.get(league, 3)
    still_fighting_relegation = rank > team_count - relegation_zone_size - RELEGATION_BUFFER
    still_fighting_europe = rank <= EUROPEAN_ZONE_SIZE + EUROPEAN_BUFFER
    return still_fighting_relegation or still_fighting_europe


def compute_motivation_adjustment(
    home_team: str, away_team: str, home_standing: dict | None, away_standing: dict | None,
    league: str, team_count: int,
) -> NewsAdjustment | None:
    """home_standing/away_standing: {rank, points, games_played} from
    espn_soccer_client.fetch_standings(league), or None if that team wasn't
    found in the real table (e.g. a name-matching miss -- degrades to no
    adjustment, not a guess)."""
    if home_standing is None or away_standing is None or league not in SEASON_LENGTH:
        return None
    season_length = SEASON_LENGTH[league]
    home_remaining = season_length - home_standing["games_played"]
    away_remaining = season_length - away_standing["games_played"]
    if home_remaining > FINAL_STRETCH_GAMES_REMAINING or away_remaining > FINAL_STRETCH_GAMES_REMAINING:
        return None

    home_has_stakes = _has_real_stakes(home_standing["rank"], team_count, league)
    away_has_stakes = _has_real_stakes(away_standing["rank"], team_count, league)

    if home_has_stakes == away_has_stakes:
        return None  # no real motivation DIFFERENTIAL between the two sides

    if home_has_stakes:
        net_pp = clamp_adjustment(MOTIVATION_PP)  # away has nothing to play for -> favors home
        factor = Factor(
            factor=f"{away_team} (rank {away_standing['rank']}/{team_count}, {away_remaining} games left) has nothing "
                   f"left to play for; {home_team} (rank {home_standing['rank']}) still does",
            direction="favor_home", weight="minor", rationale=f"{MOTIVATION_PP}pp flat late-season motivation gap.",
        )
    else:
        net_pp = clamp_adjustment(-MOTIVATION_PP)  # home has nothing to play for -> favors away
        factor = Factor(
            factor=f"{home_team} (rank {home_standing['rank']}/{team_count}, {home_remaining} games left) has nothing "
                   f"left to play for; {away_team} (rank {away_standing['rank']}) still does",
            direction="favor_away", weight="minor", rationale=f"{MOTIVATION_PP}pp flat late-season motivation gap.",
        )
    return NewsAdjustment(adjustment_pct=net_pp, confidence="low", factors=[factor], requires_review=False)

"""Free weather adjustment (Open-Meteo, no key) for outdoor stadiums.

Deliberately small and low-confidence: severe wind/cold's well-documented
effect is on SCORING (favors the under, suppresses passing efficiency) --
that's a totals-market signal, and totals aren't built yet (Phase 4). For
moneyline specifically the direction is genuinely murkier (depends on which
team is more run-heavy/cold-weather-tested, data we don't have for free), so
this applies only a small, capped "home team is more used to its own
conditions" nudge in bad weather rather than a confident directional call.

One specific, well-documented exception to that "direction is murky" caveat:
an AWAY team based in a dome/retractable-roof stadium is measurably less
acclimated to genuine cold than a team that plays outdoors every week,
regardless of run/pass mix -- the classic case is a dome team (Saints,
Vikings, Lions, Falcons, Colts, Raiders, Rams/Chargers) traveling to
Buffalo/Green Bay/Chicago in December. Retractable-roof teams (Cardinals,
Cowboys, Texans, Colts) are folded into the same bucket rather than given a
separate discounted tier -- every current retractable-roof team is also a
warm/moderate-climate city, so they're realistically about as under-exposed
to true cold as a dome team is. Known, deliberate gap: warm-climate teams
with a fully outdoor stadium (Miami, Tampa Bay, Jacksonville) are NOT
flagged here even though they're similarly cold-unacclimated in practice --
that would need a climate classification this project doesn't have for free
yet, separate from the roof-type data used everywhere else in this module.
"""
import dataclasses

from app.clients import weather_client
from app.data.stadiums import STADIUMS
from app.models.news_adjustment.schema import Factor, NewsAdjustment, clamp_adjustment

WEATHER_MAX_ADJUSTMENT_PP = 2.5  # bumped from 1.5 to leave room for the away-acclimation bonus below
WIND_SEVERE_MPH = 20.0
WIND_NOTABLE_MPH = 15.0
COLD_SEVERE_F = 20.0
COLD_NOTABLE_F = 32.0

DOME_LIKE_ROOF_VALUES = {"dome", "closed"}
OUTDOOR_ROOF_VALUES = {"outdoors", "open"}

AWAY_UNACCLIMATED_ROOF_TYPES = {"dome", "retractable"}
AWAY_UNACCLIMATED_BONUS_PP = 1.0  # only added when cold is already at least "notable" (see below)

# Points to shave off the expected TOTAL (game_lines.py) at full severity --
# the well-documented "clearer" effect this module's win-probability nudge
# above deliberately doesn't attempt (see module docstring: "that's a
# totals-market signal"). Now that totals are built (Phase 4), this closes
# that gap. Unlike MARGIN_STD/TOTAL_STD in game_lines.py, this constant is
# NOT derived from this project's own historical data (no free historical-
# weather dataset on hand, only Open-Meteo's forward-looking forecast) --
# it's a deliberately conservative, hand-picked number in the same spirit as
# this project's other unvalidated-but-reasonable constants (e.g. the
# original TRAVEL_MAX_ADJUSTMENT_PP), flagged honestly rather than presented
# as fitted. A good candidate to validate against real data later if a free
# historical-weather source turns up.
TOTAL_WEATHER_MAX_SUPPRESSION_PTS = 4.0


@dataclasses.dataclass
class WeatherSeverity:
    severity: float  # 0.0-1.0
    wind_mph: float
    cold_f: float
    cold_notable: bool
    notes: list[str]


def _is_outdoor(home_team: str, game_roof: str | None) -> bool:
    if game_roof in DOME_LIKE_ROOF_VALUES:
        return False
    if game_roof in OUTDOOR_ROOF_VALUES:
        return True
    # Blank/unknown (typical for future games at retractable-roof stadiums,
    # since the open/close call isn't made until game week) -- fall back to
    # the stadium's static classification, treating "retractable" as "skip".
    stadium = STADIUMS.get(home_team)
    return bool(stadium) and stadium["roof_type"] == "outdoor"


def is_dome_game(home_team: str, game_roof: str | None) -> bool:
    """Used by game_lines.py's dome total-boost -- deliberately conservative
    like _is_outdoor above: a blank/unknown roof at a RETRACTABLE stadium
    counts as neither dome nor outdoor (genuine uncertainty until the
    open/close call is made week-of), only a real dome or an explicitly
    closed retractable roof counts."""
    if game_roof in DOME_LIKE_ROOF_VALUES:
        return True
    if game_roof is not None:
        return False
    stadium = STADIUMS.get(home_team)
    return bool(stadium) and stadium["roof_type"] == "dome"


def is_turf_game(game_surface: str | None) -> bool:
    """Used by game_lines.py's turf total-boost (checked independent of
    is_dome_game above -- see game_lines.py module docstring for the real,
    roof-controlled data behind this). Deliberately conservative: an
    unknown/blank surface (common for games far enough out that the field
    prep hasn't been finalized) counts as NOT turf, same "unknown = no
    adjustment" convention as the rest of this module -- there's no static
    per-team fallback like is_dome_game has, since surface can change with
    stadium renovations and this project doesn't maintain that table."""
    if not game_surface:
        return False
    return game_surface.strip().lower() != "grass"


def _fetch_severity(home_team: str, game_roof: str | None, game_date_iso: str) -> WeatherSeverity | None:
    """Shared forecast fetch + severity scoring, consumed by both
    compute_weather_adjustment (win-probability nudge) and
    compute_total_points_adjustment (totals-market suppression) below --
    one Open-Meteo call per game, not two."""
    if not _is_outdoor(home_team, game_roof):
        return None
    stadium = STADIUMS.get(home_team)
    if stadium is None:
        return None

    forecast = weather_client.fetch_daily_forecast(stadium["lat"], stadium["lon"], game_date_iso)
    if forecast is None:
        return None  # game too far out for a forecast yet

    wind = forecast["wind_mph"]
    cold = forecast["temp_min_f"]

    severity = 0.0
    notes = []
    if wind >= WIND_SEVERE_MPH:
        severity = max(severity, 1.0)
        notes.append(f"{wind:.0f}mph wind forecast")
    elif wind >= WIND_NOTABLE_MPH:
        severity = max(severity, 0.5)
        notes.append(f"{wind:.0f}mph wind forecast")

    cold_notable = cold <= COLD_NOTABLE_F
    if cold <= COLD_SEVERE_F:
        severity = max(severity, 1.0)
        notes.append(f"{cold:.0f}°F forecast low")
    elif cold_notable:
        severity = max(severity, 0.4)
        notes.append(f"{cold:.0f}°F forecast low")

    if severity <= 0:
        return None
    return WeatherSeverity(severity=severity, wind_mph=wind, cold_f=cold, cold_notable=cold_notable, notes=notes)


def compute_weather_adjustment(
    home_team: str, away_team: str, game_roof: str | None, game_date_iso: str
) -> NewsAdjustment | None:
    sev = _fetch_severity(home_team, game_roof, game_date_iso)
    if sev is None:
        return None

    adjustment_pct = sev.severity * (WEATHER_MAX_ADJUSTMENT_PP - AWAY_UNACCLIMATED_BONUS_PP)  # favors home
    notes = list(sev.notes)

    away_stadium = STADIUMS.get(away_team)
    unacclimated = (
        sev.cold_notable and away_stadium is not None and away_stadium["roof_type"] in AWAY_UNACCLIMATED_ROOF_TYPES
    )
    if unacclimated:
        adjustment_pct += AWAY_UNACCLIMATED_BONUS_PP
        notes.append(f"{away_team} plays home games under a {away_stadium['roof_type']} roof, rarely exposed to true cold")

    factor = Factor(
        factor=f"Forecast for game day: {', '.join(notes)}",
        direction="favor_home",
        weight="minor" if sev.severity < 1.0 and not unacclimated else "moderate",
        rationale="Open-Meteo forecast at the stadium's coordinates; small nudge only -- weather's clearer "
        "effect is on total points scored (see compute_total_points_adjustment)"
        + (". Away team's own dome/retractable-roof home stadium adds a small cold-acclimation penalty." if unacclimated else ""),
    )
    return NewsAdjustment(
        adjustment_pct=clamp_adjustment(adjustment_pct),
        confidence="low",
        factors=[factor],
        requires_review=False,
    )


def compute_total_points_adjustment(home_team: str, away_team: str, game_roof: str | None, game_date_iso: str) -> float:
    """Points to SUBTRACT from the totals model's expected_total (see
    game_lines.py) -- 0.0 if no notable wind/cold forecast. Unlike
    compute_weather_adjustment above, this isn't capped/discounted for
    "murky direction" -- suppressed scoring in bad weather is the well-
    documented, undisputed part of the weather effect."""
    sev = _fetch_severity(home_team, game_roof, game_date_iso)
    if sev is None:
        return 0.0
    return sev.severity * TOTAL_WEATHER_MAX_SUPPRESSION_PTS

"""Investigation script (not a registered backtest): does wind direction
RELATIVE TO EACH PARK'S OWN ORIENTATION carry a real signal for MLB totals,
where raw wind speed alone (checked in game_lines_mlb.py's module docstring)
did not? Real, well-documented phenomenon in principle (Wrigley Field's
"wind blowing out" reputation) -- this checks whether it's actually there in
10 years... well, 5 years (2021-2025) of real data.

Reuses mlb_weather_cache.json's already-cached wind_dir (Open-Meteo's
`wind_direction_10m`, METEOROLOGICAL convention: the direction the wind is
blowing FROM) -- no new API calls needed. Combines with the real, sourced
ORIENTATION_DEG (home-plate-to-center-field bearing, mlb_ballparks.py) to
compute an "out factor": +1.0 when wind blows directly OUT toward center
field (from behind home plate), -1.0 when directly IN (from center field
toward home plate), 0 for a pure crosswind. `out_wind_mph = wind_mph *
out_factor` is the effective blowing-out wind component, signed.

Checked against the residual AFTER park factor AND temperature (isolating
whatever neither already explains, same "control for what's already
validated" pattern as every other signal check in this app).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402
import math  # noqa: E402

import numpy as np  # noqa: E402

from app.data.mlb_ballparks import ORIENTATION_DEG  # noqa: E402
from app.models import game_lines_mlb as G  # noqa: E402

SCHEDULE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_schedule_cache.json"
WEATHER_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_weather_cache.json"


def out_factor(wind_from_deg: float, park_orientation_deg: float) -> float:
    """+1.0 = wind blowing directly OUT toward center field, -1.0 = directly
    IN, 0 = pure crosswind. Wind FROM direction that blows OUT is the park
    orientation + 180 (wind originates from behind home plate)."""
    blowing_out_from_deg = (park_orientation_deg + 180.0) % 360.0
    diff = math.radians(wind_from_deg - blowing_out_from_deg)
    return math.cos(diff)


def main():
    games = {g["id"]: g for g in json.loads(SCHEDULE_PATH.read_text())}
    weather = json.loads(WEATHER_CACHE_PATH.read_text())
    print(f"{len(weather)} games with real weather data, {len(ORIENTATION_DEG)} parks with real sourced orientation")

    out_winds, residuals = [], []
    skipped_no_orientation = 0
    for gid, w in weather.items():
        g = games.get(gid)
        if g is None or g.get("home_score") is None:
            continue
        team = g["home_team"]
        if team not in ORIENTATION_DEG:
            skipped_no_orientation += 1
            continue
        actual = g["home_score"] + g["away_score"]
        expected = G.expected_total(team, w["temp_f"])
        of = out_factor(w["wind_dir"], ORIENTATION_DEG[team])
        out_winds.append(w["wind_mph"] * of)
        residuals.append(actual - expected)

    out_winds = np.array(out_winds)
    residuals = np.array(residuals)
    n = len(residuals)
    print(f"n={n} games scored (skipped {skipped_no_orientation}, no orientation on file)")
    print(f"out_wind_mph range: [{out_winds.min():.1f}, {out_winds.max():.1f}], mean={out_winds.mean():.2f}")
    print()

    r = np.corrcoef(out_winds, residuals)[0, 1]
    print(f"Correlation out_wind_mph (signed, +out/-in) vs residual (after park+temp): {r:.4f}")

    # Half-split chronological sign-consistency check, same discipline as
    # every other new-signal check this session.
    gamedays = [weather[gid]["gameday"] for gid, g in games.items() if gid in weather and g.get("home_score") is not None and g["home_team"] in ORIENTATION_DEG]
    order = np.argsort(gamedays)
    mid = n // 2
    r1 = np.corrcoef(out_winds[order][:mid], residuals[order][:mid])[0, 1]
    r2 = np.corrcoef(out_winds[order][mid:], residuals[order][mid:])[0, 1]
    print(f"First half: {r1:.4f}  |  Second half: {r2:.4f}  |  same sign as full: {(r1 > 0) == (r2 > 0) == (r > 0)}")
    print()

    print("Actual total runs by out_wind_mph bucket (raw, not adjusted):")
    actual_totals = np.array([games[gid]["home_score"] + games[gid]["away_score"] for gid in weather
                               if games.get(gid) and games[gid].get("home_score") is not None and games[gid]["home_team"] in ORIENTATION_DEG])
    for lo, hi in [(-30, -10), (-10, -3), (-3, 3), (3, 10), (10, 30)]:
        mask = (out_winds >= lo) & (out_winds < hi)
        if mask.sum() < 20:
            continue
        print(f"  {lo:>4} to {hi:>4} mph out: n={mask.sum():>5}  avg_total={actual_totals[mask].mean():.3f}")

    # Sanity check against a well-known real case: Wrigley Field specifically
    cubs_mask = np.array([games[gid]["home_team"] == "CHC" for gid in weather
                           if games.get(gid) and games[gid].get("home_score") is not None and games[gid]["home_team"] in ORIENTATION_DEG])
    if cubs_mask.sum() > 30:
        r_cubs = np.corrcoef(out_winds[cubs_mask], residuals[cubs_mask])[0, 1]
        print(f"\nWrigley Field (CHC) specifically, n={cubs_mask.sum()}: correlation out_wind vs residual = {r_cubs:.4f} "
              f"(sanity check against Wrigley's famous wind reputation)")


if __name__ == "__main__":
    main()

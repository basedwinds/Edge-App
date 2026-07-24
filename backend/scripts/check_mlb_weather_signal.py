"""Investigation script (not a registered backtest): does real historical
game-time temperature/wind carry a signal for MLB total runs, beyond the
already-validated park factor? Closes a real gap this app has had since the
NFL weather module was built (weather_rules.py's own docstring: no free
historical-weather dataset existed, so its total-suppression constant was
hand-picked, never fitted against real outcomes). Open-Meteo's archive API
(see build_mlb_weather_cache.py) fixes that for MLB.

Checks the RESIDUAL after PARK_FACTOR (actual_total - expected_total(home_team))
against weather, not raw total -- isolates whatever variance park factor
doesn't already explain, same "control for what's already validated" logic
as checking era_diff's correlation with elo_diff before trusting the
starting-pitcher signal wasn't just restating team strength.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402

import numpy as np  # noqa: E402
from sklearn.linear_model import LinearRegression  # noqa: E402

from app.models import game_lines_mlb as G  # noqa: E402

SCHEDULE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_schedule_cache.json"
WEATHER_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_weather_cache.json"


def main():
    games = {g["id"]: g for g in json.loads(SCHEDULE_PATH.read_text())}
    weather = json.loads(WEATHER_CACHE_PATH.read_text())
    print(f"{len(weather)} games with real weather data")

    temps, winds, wind_dirs, residuals, actual_totals, expected_totals = [], [], [], [], [], []
    for gid, w in weather.items():
        g = games.get(gid)
        if g is None or g.get("home_score") is None or g.get("away_score") is None:
            continue
        actual_total = g["home_score"] + g["away_score"]
        expected = G.expected_total(g["home_team"])
        temps.append(w["temp_f"])
        winds.append(w["wind_mph"])
        wind_dirs.append(w["wind_dir"])
        residuals.append(actual_total - expected)
        actual_totals.append(actual_total)
        expected_totals.append(expected)

    n = len(residuals)
    temps = np.array(temps)
    winds = np.array(winds)
    residuals = np.array(residuals)
    actual_totals = np.array(actual_totals)
    print(f"n={n} games with both weather and a final score")
    print(f"temp_f range: [{temps.min():.0f}, {temps.max():.0f}], mean={temps.mean():.1f}")
    print(f"wind_mph range: [{winds.min():.1f}, {winds.max():.1f}], mean={winds.mean():.1f}")
    print()

    print("=== Against RAW actual total (before park factor) ===")
    print(f"Correlation temp_f vs actual_total: {np.corrcoef(temps, actual_totals)[0, 1]:.4f}")
    print(f"Correlation wind_mph vs actual_total: {np.corrcoef(winds, actual_totals)[0, 1]:.4f}")
    print()

    print("=== Against RESIDUAL (actual_total - park-factor-adjusted expected_total) ===")
    r_temp = np.corrcoef(temps, residuals)[0, 1]
    r_wind = np.corrcoef(winds, residuals)[0, 1]
    print(f"Correlation temp_f vs residual: {r_temp:.4f}")
    print(f"Correlation wind_mph vs residual: {r_wind:.4f}")
    print()

    # Fit both together (they might interact/be correlated with each other)
    X = np.column_stack([temps, winds])
    reg = LinearRegression().fit(X, residuals)
    pred = reg.predict(X)
    resid_std_before = residuals.std()
    resid_std_after = (residuals - pred).std()
    print(f"Linear fit: temp coef={reg.coef_[0]:.4f} runs/°F, wind coef={reg.coef_[1]:.4f} runs/mph, "
          f"intercept={reg.intercept_:.4f}")
    print(f"Residual std BEFORE weather: {resid_std_before:.4f}  |  AFTER: {resid_std_after:.4f}")
    print(f"(TOTAL_STD in game_lines_mlb.py, park-factor-adjusted only, is {G.TOTAL_STD})")
    print()

    # Half-split chronological sign-consistency check (mirrors the bullpen-
    # fatigue/RFI checks -- same discipline, not a per-season check since
    # this is a brand-new signal not yet validated any other way).
    mid = n // 2
    idx_sorted = np.argsort([w["gameday"] for gid, w in weather.items() if games.get(gid) and games[gid].get("home_score") is not None])
    temps_sorted = temps[idx_sorted]
    residuals_sorted = residuals[idx_sorted]
    r1 = np.corrcoef(temps_sorted[:mid], residuals_sorted[:mid])[0, 1]
    r2 = np.corrcoef(temps_sorted[mid:], residuals_sorted[mid:])[0, 1]
    print(f"Temp-vs-residual correlation, first half chronologically: {r1:.4f}  |  second half: {r2:.4f}  |  "
          f"same sign: {(r1 > 0) == (r2 > 0) == (r_temp > 0)}")

    # Bucketed view for a sanity check beyond the raw correlation coefficient
    print()
    print("Actual total runs by temperature bucket (raw, not park-adjusted):")
    for lo, hi in [(0, 50), (50, 60), (60, 70), (70, 80), (80, 90), (90, 130)]:
        mask = (temps >= lo) & (temps < hi)
        if mask.sum() < 20:
            continue
        print(f"  {lo}-{hi}F: n={mask.sum():>5}  avg_total={actual_totals[mask].mean():.3f}")


if __name__ == "__main__":
    main()

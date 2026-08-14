"""Does AIR DENSITY carry a totals signal beyond the temperature slope already
shipped?

THE PHYSICAL CLAIM. A batted ball decelerates against drag proportional to air
density, so the same contact carries further through thinner air. This app
already ships TEMP_SLOPE (+0.0365 runs per degree F), which captures part of
that -- warm air is thinner. But density is a function of temperature AND
pressure AND humidity, and the other two are not in the model at all. Humidity
in particular is counter-intuitive: humid air is LESS dense, because a water
molecule (18) is lighter than the nitrogen (28) and oxygen (32) it displaces,
so muggy days should play slightly bigger, not smaller.

    P_sat = 6.1078 * 10^(7.5*Tc / (Tc + 237.3))      hPa   (Tetens)
    P_v   = RH/100 * P_sat                            hPa
    rho   = (P_d / (R_d * T)) + (P_v / (R_v * T))     kg/m^3

THE ONLY QUESTION THAT MATTERS IS INCREMENTAL. Density is strongly (negatively)
correlated with temperature by construction, so a raw density-vs-total
correlation would mostly re-measure TEMP_SLOPE and look like a discovery. This
tests density against the RESIDUAL after PARK_FACTOR and TEMPERATURE and
OUT-WIND are all applied -- the same "control for what is already validated"
method that admitted wind direction and rejected raw wind speed. The redundancy
correlation against temperature is printed alongside, so the reader can see how
much room was even left to explain.

NO NEW DATA SOURCE. Humidity and surface pressure come back from the SAME free
Open-Meteo archive call the temperature/wind cache is built from -- see
build_mlb_weather_cache.py, which now stores them.

WHAT WOULD MAKE THIS SHIPPABLE: a slope whose sign matches the physics (thinner
air -> MORE runs, so a NEGATIVE coefficient on density), that clears its own
noise floor on n, and that survives a first-half/second-half split of the data
with a consistent sign -- the same three tests the temperature and wind-direction
constants had to pass. Failing any one of them, it stays unshipped and this
script is the record of why.

Run: backend/.venv/Scripts/python.exe scripts/check_mlb_air_density_signal.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json  # noqa: E402
import math  # noqa: E402

import numpy as np  # noqa: E402

import app.models.game_lines_mlb as G  # noqa: E402
from app.data.mlb_ballparks import ORIENTATION_DEG  # noqa: E402

DATA = Path(__file__).resolve().parents[2] / "data"
WEATHER_PATH = DATA / "mlb_weather_density_cache.json"
SCHEDULE_PATH = DATA / "mlb_schedule_cache.json"

R_D = 287.058  # J/(kg*K), dry air
R_V = 461.495  # J/(kg*K), water vapour


def air_density(temp_f: float, rh_pct: float, pressure_hpa: float) -> float:
    tc = (temp_f - 32.0) * 5.0 / 9.0
    tk = tc + 273.15
    p_sat = 6.1078 * 10 ** (7.5 * tc / (tc + 237.3))     # hPa
    p_v = max(0.0, min(rh_pct, 100.0)) / 100.0 * p_sat   # hPa
    p_d = max(pressure_hpa - p_v, 0.0)                   # hPa
    return (p_d * 100.0) / (R_D * tk) + (p_v * 100.0) / (R_V * tk)


def out_factor(wind_dir_deg: float, orientation_deg: float) -> float:
    """+1 straight out to centre, -1 straight in. Identical to
    check_mlb_wind_direction_signal.py -- the control must match the shipped
    constant's own definition or the residual is not the one being corrected."""
    return math.cos(math.radians((wind_dir_deg - (orientation_deg + 180.0)) % 360.0))


def _fit_through_origin(x: np.ndarray, y: np.ndarray) -> float:
    return float(np.sum(x * y) / np.sum(x * x))


def main() -> None:
    if not WEATHER_PATH.exists():
        raise SystemExit(f"missing {WEATHER_PATH} -- run build_mlb_weather_cache.py first")

    weather = json.loads(WEATHER_PATH.read_text())
    games = {str(g["id"]): g for g in json.loads(SCHEDULE_PATH.read_text())}

    dens, resid, temps, seasons = [], [], [], []
    skipped = 0
    for gid, w in weather.items():
        g = games.get(str(gid))
        if g is None or g.get("home_score") is None or g.get("away_score") is None:
            skipped += 1
            continue
        if w.get("rh_pct") is None or w.get("pressure_hpa") is None or w.get("temp_f") is None:
            skipped += 1
            continue
        team = w["team"]
        if team not in ORIENTATION_DEG:
            skipped += 1
            continue
        ow = w["wind_mph"] * out_factor(w["wind_dir"], ORIENTATION_DEG[team])
        # FULL control: park + temperature + out-wind, i.e. everything already shipped.
        expected = G.expected_total(team, w["temp_f"], ow)
        dens.append(air_density(w["temp_f"], w["rh_pct"], w["pressure_hpa"]))
        resid.append((g["home_score"] + g["away_score"]) - expected)
        temps.append(w["temp_f"])
        seasons.append(g["season"])

    d = np.array(dens)
    r = np.array(resid)
    t = np.array(temps)
    n = len(d)
    print(f"{n} games with full weather (skipped {skipped})")
    print(f"air density: mean {d.mean():.4f} kg/m3  sd {d.std():.4f}  "
          f"range {d.min():.4f}..{d.max():.4f}")
    print(f"REDUNDANCY -- density vs temperature: r = {np.corrcoef(d, t)[0,1]:+.4f}")
    print("  (strongly negative is expected and is the whole reason this tests the")
    print("   residual rather than the raw total)")
    print()

    dc = d - d.mean()
    corr = float(np.corrcoef(dc, r)[0, 1])
    se = 1.0 / math.sqrt(n)
    slope = _fit_through_origin(dc, r)
    print(f"density (centred) vs residual-after-park+temp+wind: r = {corr:+.4f}")
    print(f"  noise floor ~1/sqrt(n) = {se:.4f}   -> {abs(corr)/se:.2f} SE from zero")
    print(f"  through-origin slope: {slope:+.3f} runs per kg/m3")
    print(f"  across the observed density range that is "
          f"{slope * (d.max() - d.min()):+.3f} runs end to end")
    print()

    print("SIGN CHECK: physics says thinner air -> more carry -> MORE runs, so a")
    print("real effect must be NEGATIVE (higher density, fewer runs).")
    print(f"  observed sign: {'NEGATIVE (matches physics)' if slope < 0 else 'POSITIVE (contradicts physics)'}")
    print()

    mid = n // 2
    for label, sl in (("first half", slice(0, mid)), ("second half", slice(mid, n))):
        sub_d, sub_r = dc[sl], r[sl]
        print(f"  {label:<12} n={len(sub_d):<6} r={np.corrcoef(sub_d, sub_r)[0,1]:+.4f}  "
              f"slope={_fit_through_origin(sub_d, sub_r):+.3f}")
    print()
    print("VERDICT: ship only if the sign matches physics, the correlation clears")
    print("the noise floor by a real margin, AND both halves agree in sign. A slope")
    print("that only exists in one half is the failure mode that killed the global")
    print("soccer goal scale and the esports idle-decay adjustment.")


if __name__ == "__main__":
    main()

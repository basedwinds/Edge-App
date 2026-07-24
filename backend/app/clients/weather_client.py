"""Free, no-auth weather forecast API (confirmed live 2026-07-14, no key
needed): https://api.open-meteo.com/v1/forecast

Only forecasts ~16 days out -- for games further away this simply returns no
matching date, and the caller treats that as "no weather signal yet" rather
than an error.
"""
import datetime

from app.clients.base import get_json

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# In-process cache, added 2026-07-16: this was a live, UNCACHED network call
# on every single invocation -- fine when it was only reached for open
# total/team_total markets (none open yet for the regular season), but the
# new predicted-score feature (see markets.py::_predict_score) calls the
# same weather path for every moneyline row too, which meant up to ~130
# live Open-Meteo calls per /markets request. Caught by timing the endpoint
# live (22.9s) before shipping, not assumed slow. A stadium+date's forecast
# doesn't meaningfully change minute to minute, so a short TTL is safe.
_CACHE_TTL = datetime.timedelta(minutes=30)
_cache: dict[tuple[float, float, str], tuple[datetime.datetime, dict | None]] = {}


def fetch_daily_forecast(lat: float, lon: float, date_iso: str) -> dict | None:
    """Returns {temp_min_f, temp_max_f, wind_mph, precip_pct} for date_iso,
    or None if that date isn't within the available forecast window."""
    key = (lat, lon, date_iso)
    now = datetime.datetime.utcnow()
    cached = _cache.get(key)
    if cached is not None and now - cached[0] < _CACHE_TTL:
        return cached[1]

    url = (
        f"{FORECAST_URL}?latitude={lat}&longitude={lon}"
        "&daily=temperature_2m_max,temperature_2m_min,wind_speed_10m_max,precipitation_probability_max"
        "&temperature_unit=fahrenheit&wind_speed_unit=mph&forecast_days=16"
    )
    data = get_json(url)
    daily = data.get("daily", {})
    dates = daily.get("time", [])
    if date_iso not in dates:
        result = None
    else:
        idx = dates.index(date_iso)
        result = {
            "temp_min_f": daily["temperature_2m_min"][idx],
            "temp_max_f": daily["temperature_2m_max"][idx],
            "wind_mph": daily["wind_speed_10m_max"][idx],
            "precip_pct": daily["precipitation_probability_max"][idx],
        }
    _cache[key] = (now, result)
    return result


# Hourly forecast lookup, added for MLB's real, validated temperature- and
# wind-direction-vs-totals signals (see game_lines_mlb.py's TEMP_SLOPE/
# OUT_WIND_SLOPE derivation) -- unlike fetch_daily_forecast's min/max, this
# needs the reading AT FIRST PITCH specifically, since both effects were
# validated hour-by-hour against Open-Meteo's ARCHIVE API the same way (see
# scripts/build_mlb_weather_cache.py). Separate cache/function rather than
# reusing fetch_daily_forecast's shape, which callers (NFL's weather_rules.py)
# already depend on.
_hourly_cache: dict[tuple[float, float, str], tuple[datetime.datetime, dict | None]] = {}


def fetch_hourly_weather(lat: float, lon: float, local_hour_iso: str, tz: str) -> dict | None:
    """Returns {temp_f, wind_mph, wind_dir} for `local_hour_iso`
    ("YYYY-MM-DDTHH:00" in the ballpark's own local time, `tz` an IANA zone
    name), or None if that hour isn't within the forecast's ~16-day window
    yet -- treated as "no signal yet", not an error, same convention as
    fetch_daily_forecast. `wind_dir` is METEOROLOGICAL convention (direction
    the wind is blowing FROM), matching Open-Meteo's archive API used to
    derive OUT_WIND_SLOPE."""
    key = (lat, lon, local_hour_iso)
    now = datetime.datetime.utcnow()
    cached = _hourly_cache.get(key)
    if cached is not None and now - cached[0] < _CACHE_TTL:
        return cached[1]

    url = (
        f"{FORECAST_URL}?latitude={lat}&longitude={lon}"
        "&hourly=temperature_2m,wind_speed_10m,wind_direction_10m"
        f"&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone={tz.replace('/', '%2F')}&forecast_days=16"
    )
    data = get_json(url)
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    result = None
    if local_hour_iso in times:
        idx = times.index(local_hour_iso)
        result = {
            "temp_f": hourly["temperature_2m"][idx],
            "wind_mph": hourly["wind_speed_10m"][idx],
            "wind_dir": hourly["wind_direction_10m"][idx],
        }
    _hourly_cache[key] = (now, result)
    return result

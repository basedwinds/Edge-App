"""Soccer match data ingestion -- merges two heterogeneous free sources into
one common match-dict stream, parallel to tennis_data.py's tennisdata+
tennisexplorer merge, same "parallel modules per sport" architecture call.

- football-data.co.uk (see app/clients/football_data_client.py): EPL/La
  Liga/Serie A/Bundesliga/Ligue 1 (division codes E0/SP1/I1/D1/F1), full
  match history with real opening+closing bookmaker odds -- backtestable.
- ESPN's free scoreboard API (see app/clients/espn_soccer_client.py): MLS
  results only, NO odds -- never backtestable, see SoccerMatch's docstring
  in app/db/models.py.

Like tennis_data.py, this module only ever READS the local JSON caches built
by scripts/build_soccer_match_cache.py -- never hits either source directly
at request time. elo_service_soccer.py trains its walk-forward ratings
directly off load_matches()'s in-memory list; historical matches are NOT
written into the soccer_matches DB table (that table is for LIVE/upcoming
matches only, derived from Kalshi/Polymarket listings -- see
market_catalog_soccer.py -- same split as TennisMatch's real usage: the DB
tracks live state, the JSON cache is the training set)."""
from __future__ import annotations

from app.ingestion import international_data

import json
import re
import unicodedata
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
FOOTBALL_DATA_CACHE_PATH = DATA_DIR / "football_data_matches_cache.json"
ESPN_MLS_CACHE_PATH = DATA_DIR / "espn_mls_matches_cache.json"


def _safe_float(value) -> float | None:
    """football-data.co.uk leaves a cell blank when a bookmaker didn't quote
    that market for that match -- not a real 0.0 odds value."""
    try:
        v = float(value)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _safe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _season_label(start_year: int) -> str:
    return f"{start_year}-{start_year + 1}"


def _row_to_football_data_match(row: pd.Series) -> dict | None:
    date_raw = row.get("Date")
    if pd.isna(date_raw):
        return None
    # football-data.co.uk uses DD/MM/YY in older seasons and DD/MM/YYYY in
    # newer ones on the SAME column -- dayfirst=True handles both, pandas
    # infers the 2- vs 4-digit year per-value.
    try:
        match_date = pd.to_datetime(str(date_raw), dayfirst=True).date().isoformat()
    except (ValueError, TypeError):
        return None
    home_team, away_team = row.get("HomeTeam"), row.get("AwayTeam")
    if not home_team or not away_team or pd.isna(home_team) or pd.isna(away_team):
        return None
    ft_result = row.get("FTR") if pd.notna(row.get("FTR")) else None
    div, season_start = row.get("_div"), row.get("_season_start_year")

    # Closing odds ("...C..." columns) only exist in more recent seasons --
    # fall back to the opening-line average when absent, never to a single
    # unaveraged book (see football_data_client.py's module docstring).
    home_odds = _safe_float(row.get("AvgCH")) or _safe_float(row.get("AvgH"))
    draw_odds = _safe_float(row.get("AvgCD")) or _safe_float(row.get("AvgD"))
    away_odds = _safe_float(row.get("AvgCA")) or _safe_float(row.get("AvgA"))
    over25_odds = _safe_float(row.get("AvgC>2.5")) or _safe_float(row.get("Avg>2.5"))
    under25_odds = _safe_float(row.get("AvgC<2.5")) or _safe_float(row.get("Avg<2.5"))
    ah_line = _safe_float(row.get("AHCh"))
    if ah_line is None:
        ah_line = _safe_float(row.get("AHh"))
    ah_home_odds = _safe_float(row.get("AvgCAHH")) or _safe_float(row.get("AvgAHH"))
    ah_away_odds = _safe_float(row.get("AvgCAHA")) or _safe_float(row.get("AvgAHA"))

    return {
        "source": "football-data.co.uk",
        "source_match_id": f"fd:{div}:{match_date}:{home_team}:{away_team}",
        "league": div,
        "season": _season_label(int(season_start)) if pd.notna(season_start) else None,
        "match_date": match_date,
        "home_team": str(home_team).strip(),
        "away_team": str(away_team).strip(),
        "home_goals_ft": _safe_int(row.get("FTHG")),
        "away_goals_ft": _safe_int(row.get("FTAG")),
        "home_goals_ht": _safe_int(row.get("HTHG")),
        "away_goals_ht": _safe_int(row.get("HTAG")),
        "result_ft": ft_result,
        "home_odds": home_odds,
        "draw_odds": draw_odds,
        "away_odds": away_odds,
        # Backtest-only extras (Phase C spread/total extension) -- not part
        # of the SoccerMatch DB schema, only ever read from this cache.
        "total_over_2_5_odds": over25_odds,
        "total_under_2_5_odds": under25_odds,
        "ah_line": ah_line,
        "ah_home_odds": ah_home_odds,
        "ah_away_odds": ah_away_odds,
    }


def load_football_data_matches() -> list[dict]:
    if not FOOTBALL_DATA_CACHE_PATH.exists():
        return []
    return json.loads(FOOTBALL_DATA_CACHE_PATH.read_text())


def build_football_data_cache(end_year: int | None = None) -> list[dict]:
    from app.clients import football_data_client

    matches = []
    # Top-flight (E0/SP1/I1/D1/F1) AND second-tier (E1/SP2/I2/D2/F2) --
    # second tier added 2026-07-19 specifically so a promoted team has a
    # real rating (see football_data_client.py's own SECOND_DIVISIONS
    # docstring). Each division trains into its OWN separate rating pool
    # (keyed by this dict's `league` field), same "one state per league"
    # convention as everywhere else in this app -- no special-casing needed
    # here, elo_service_soccer.py's refresh_ratings() already partitions by
    # league automatically.
    all_divisions = {**football_data_client.DIVISIONS, **football_data_client.SECOND_DIVISIONS}
    for div in all_divisions:
        df = football_data_client.fetch_division_history(div, end_year=end_year)
        for _, row in df.iterrows():
            m = _row_to_football_data_match(row)
            if m is not None:
                matches.append(m)

    # EXTRA-FORMAT divisions (Brazil, Argentina, Mexico, Japan) come from a
    # structurally different feed -- one file per COUNTRY, all seasons, its own
    # column names -- so they have their own reader. fetch_extra_division
    # normalises onto the SAME columns the loop above produces, which is why
    # _row_to_football_data_match needs no special case here.
    for div in football_data_client.EXTRA_DIVISIONS:
        df = football_data_client.fetch_extra_division(div)
        for _, row in df.iterrows():
            m = _row_to_football_data_match(row)
            if m is not None:
                matches.append(m)
    matches.sort(key=lambda m: m["match_date"])
    FOOTBALL_DATA_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FOOTBALL_DATA_CACHE_PATH.write_text(json.dumps(matches))
    return matches


def load_espn_mls_matches() -> list[dict]:
    if not ESPN_MLS_CACHE_PATH.exists():
        return []
    return json.loads(ESPN_MLS_CACHE_PATH.read_text())


# ESPN-sourced leagues football-data.co.uk does not carry (2026-08-12). One
# cache file per league, written by scripts/build_espn_soccer_league_caches.py
# -- see that script for why the leagues were chosen and, more importantly, why
# each one's `season` label is DERIVED from its own match-month histogram rather
# than assumed (SEASON_REGRESSION fires on every season-string change).
#
# Globbed rather than listed so adding a league is one entry in the script's
# ESPN_ONLY_LEAGUES map plus a crawl -- nothing to keep in sync here. A missing
# cache degrades to "that league has no ratings", never to a crash.
ESPN_LEAGUE_CACHE_GLOB = "espn_soccer_*_matches_cache.json"


def load_espn_league_matches() -> list[dict]:
    out: list[dict] = []
    for path in sorted(DATA_DIR.glob(ESPN_LEAGUE_CACHE_GLOB)):
        try:
            out.extend(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue  # a half-written cache must not take the whole stream down
    return out


def build_espn_mls_cache(start_year: int = 2018) -> list[dict]:
    import datetime as dt

    from app.clients import espn_soccer_client

    end = dt.date.today()
    start = dt.date(start_year, 1, 1)
    raw_events = espn_soccer_client.fetch_season_range(start, end)
    matches = espn_soccer_client.parse_final_events(raw_events)
    # De-dupe: fetch_season_range's chunking can return the same event twice
    # if a match falls exactly on a chunk boundary window overlap.
    seen = set()
    deduped = []
    for m in matches:
        if m["source_match_id"] in seen:
            continue
        seen.add(m["source_match_id"])
        deduped.append(m)
    deduped.sort(key=lambda m: m["match_date"])
    ESPN_MLS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ESPN_MLS_CACHE_PATH.write_text(json.dumps(deduped))
    return deduped


def load_matches() -> list[dict]:
    """Full merged, chronologically-sorted stream -- what elo_service_soccer.py
    trains the walk-forward attack/defense ratings on. Each league (E0/SP1/
    I1/D1/F1/MLS) gets its own rating pool at the service layer, but this
    function returns everything together since that's the natural
    checkpoint-and-resume unit at the cache layer, same as tennis_data.py."""
    # Competitive internationals join the stream tagged "INTL" (2026-08-09).
    # National teams only ever play each other, so the per-league pooling this
    # function feeds is exactly right for them rather than a compromise -- and
    # routing them through here means ratings, resolve_league and every pricing
    # path work with no parallel service. Empty when the cache has not been
    # built, which degrades to "no INTL ratings" instead of breaking the rest.
    matches = (load_football_data_matches() + load_espn_mls_matches()
               + load_espn_league_matches() + international_data.load_matches())
    matches.sort(key=lambda m: (m["match_date"], m["league"], m["source_match_id"]))
    return matches


_TEAM_NAME_RE = re.compile(r"[^a-z0-9 ]")


def normalize_team_name(name: str | None) -> str | None:
    """Lowercase + accent-strip + strip punctuation, for exact-ish
    cross-source team-name joins (football-data.co.uk vs a live Kalshi/
    Polymarket listing render the SAME club differently -- e.g. "Man United"
    vs "Manchester United"). This alone does NOT solve that problem (see
    market_matcher_soccer.py's hardcoded alias table, which normalizes
    THROUGH this function first) -- it only collapses whitespace/case/
    accent/punctuation noise, not real name variants.

    REAL BUG this fixes: an earlier version only stripped non-[a-z0-9 ]
    characters directly, which DELETES an accented letter entirely instead
    of transliterating it (e.g. ESPN's "CF Montréal" -> "cf montral", not
    "cf montreal") -- same accent-stripping mistake tennis_data.py's
    normalize_player_key already knew to avoid via NFKD decomposition, not
    proactively applied here at first."""
    if not name or not name.strip():
        return None
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _TEAM_NAME_RE.sub("", stripped.lower()).strip()

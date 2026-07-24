"""Valorant match data ingestion -- scrapes vlr.gg directly (no official API
exists, confirmed live 2026-07-19: vlr.gg loads with ZERO Cloudflare/bot
gating, unlike HLTV, the analogous CS2 site that blocked the earlier
standalone CS2 betting-model project). Parallel to tennis_data.py/
soccer_data.py's "the live listing IS the schedule" pattern -- vlr.gg's own
/matches (upcoming) and /matches/results (completed) pages are scraped
directly with BeautifulSoup (already a dependency, see ufcstats_client.py),
same html.parser usage.

Real DOM structure confirmed live 2026-07-19 (saved+inspected raw HTML, not
assumed): each match is an `<a class="match-item">` inside a `<div
class="wf-card">`, preceded by a `<div class="wf-label mod-large">` date
header shared across every match-item in that card. Team names live in
`.match-item-vs-team-name .text-of`; scores in
`.match-item-vs-team-score` (present as real text even on vlr.gg's
"spoiler-free" pages -- confirmed live, the number is in the raw HTML
regardless of the CSS/JS `sp-mask` spoiler-cover class); the winning side
carries an extra `mod-winner` class on its team div.

best_of is NOT exposed on either listing page (confirmed live) -- only a
match's own detail page states it reliably, and scraping every detail page
just to get one integer is expensive for a 5-minute poll cycle. Same
"backfill from a real market signal instead of guessing" pattern as
MmaFight.scheduled_rounds (see poller_mma.py::_infer_scheduled_rounds_from_kalshi):
here the highest map_number seen across live KXVALORANTMAP markets for a
match is used instead (see poller_valorant.py).

estimated_start_time built from the listing's own date+time text is a rough
UTC-assumption ESTIMATE (vlr.gg's anonymous/no-cookie response gives no
explicit UTC offset or timezone data attribute, confirmed live -- checked
for data-utc-ts/timestamp attributes, none present) -- always overwritten by
Kalshi's own real occurrence_datetime once a market is polled, same "genuine
estimate, always overwrite while upcoming" convention as every other sport's
estimated_start_time in this app.
"""
from __future__ import annotations

import datetime as dt
import re

import httpx
from bs4 import BeautifulSoup

MATCHES_URL = "https://www.vlr.gg/matches"
RESULTS_URL = "https://www.vlr.gg/matches/results"

_client = httpx.Client(timeout=30.0, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"})

_MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}


def _parse_date_label(label_text: str) -> str | None:
    """"Mon, July 20, 2026" -> "2026-07-20". Returns None for anything
    unrecognized rather than guessing (e.g. vlr.gg's own "Today"/"Yesterday"
    tag text, which is a SEPARATE element from the date label itself and
    never fed into this function)."""
    m = re.search(r"(\w+)\s+(\d{1,2}),\s+(\d{4})", label_text)
    if not m:
        return None
    month_name, day, year = m.groups()
    month = _MONTHS.get(month_name)
    if not month:
        return None
    try:
        return dt.date(int(year), month, int(day)).isoformat()
    except ValueError:
        return None


def _parse_time_to_utc_estimate(match_date: str, time_text: str) -> str | None:
    """"4:00 AM" + "2026-07-20" -> a rough ISO UTC instant. See module
    docstring: this is a best-effort UTC assumption, not a confirmed
    timezone -- deliberately rough, always superseded by Kalshi's real
    occurrence_datetime once available."""
    m = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)", time_text.strip(), re.IGNORECASE)
    if not m:
        return None
    hour, minute, meridiem = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if meridiem == "PM" and hour != 12:
        hour += 12
    if meridiem == "AM" and hour == 12:
        hour = 0
    try:
        date_part = dt.date.fromisoformat(match_date)
    except ValueError:
        return None
    return dt.datetime(date_part.year, date_part.month, date_part.day, hour, minute, tzinfo=dt.timezone.utc).isoformat()


def _event_name(match_item) -> str:
    event_div = match_item.find("div", class_="match-item-event")
    if event_div is None:
        return ""
    series_div = event_div.find("div", class_="match-item-event-series")
    series_text = series_div.get_text(strip=True) if series_div else ""
    full_text = event_div.get_text(" ", strip=True)
    if series_text and full_text.startswith(series_text):
        full_text = full_text[len(series_text):].strip()
    return full_text


def _parse_match_item(match_item, match_date: str | None) -> dict | None:
    href = match_item.get("href", "")
    m = re.match(r"/(\d+)/", href)
    if not m or not match_date:
        return None
    source_match_id = m.group(1)

    team_divs = match_item.find_all("div", class_="match-item-vs-team")
    if len(team_divs) != 2:
        return None

    def _team_name(team_div) -> str:
        name_div = team_div.find("div", class_="match-item-vs-team-name")
        text_div = name_div.find("div", class_="text-of") if name_div else None
        return text_div.get_text(strip=True) if text_div else ""

    def _team_score(team_div) -> int | None:
        score_div = team_div.find("div", class_="match-item-vs-team-score")
        if score_div is None:
            return None
        text = score_div.get_text(strip=True)
        return int(text) if text.isdigit() else None

    team_a, team_b = _team_name(team_divs[0]), _team_name(team_divs[1])
    if not team_a or not team_b:
        return None
    score_a, score_b = _team_score(team_divs[0]), _team_score(team_divs[1])

    winner = None
    if "mod-winner" in team_divs[0].get("class", []):
        winner = "team_a"
    elif "mod-winner" in team_divs[1].get("class", []):
        winner = "team_b"

    time_div = match_item.find("div", class_="match-item-time")
    time_text = time_div.get_text(strip=True) if time_div else ""
    estimated_start_time = _parse_time_to_utc_estimate(match_date, time_text) if time_text else None

    return {
        "source": "vlr",
        "source_match_id": source_match_id,
        "event_name": _event_name(match_item),
        "match_date": match_date,
        "estimated_start_time": estimated_start_time,
        "team_a": team_a,
        "team_b": team_b,
        "maps_won_a": score_a,
        "maps_won_b": score_b,
        "winner": winner,
    }


def infer_best_of_from_score(maps_won_a: int | None, maps_won_b: int | None) -> int | None:
    """vlr.gg's match LISTING pages (both the live /matches page and an
    event's own /event/matches/.../?series_id=all page) never state best_of
    directly (confirmed live 2026-07-19 -- a REAL gap this app's historical
    crawl hit: scripts/build_valorant_match_cache.py's own 1,741-match cache
    came back with best_of missing on every single row, only discovered when
    scripts/derive_valorant_elo_constants.py's grid search loaded zero usable
    matches). Rather than an extra per-match page fetch (1,741 more requests
    just for one integer), a decided match's own final map tally already
    determines it exactly: the winning side's map count IS the series'
    clinch threshold -- 1 map won -> Bo1, 2 -> Bo3, 3 -> Bo5. Real,
    deducible fact, not a guess. Returns None for a still-undecided match or
    an unrecognized tally (should not happen for a real completed series)."""
    if maps_won_a is None or maps_won_b is None:
        return None
    highest = max(maps_won_a, maps_won_b)
    return {1: 1, 2: 3, 3: 5}.get(highest)


def parse_matches_from_html(html: str) -> list[dict]:
    """Used by both the live poller (fetch_upcoming_matches/
    fetch_recent_results below) AND scripts/build_valorant_match_cache.py's
    historical crawl -- confirmed live 2026-07-19: an event's own
    /event/matches/{id}/{slug}/?series_id=all page uses the EXACT SAME
    `match-item`/`wf-label mod-large` DOM shape as vlr.gg's live /matches
    listing, so this one parser correctly handles both without
    modification."""
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find("div", class_="col-container") or soup
    matches: list[dict] = []
    current_date: str | None = None
    for el in container.find_all(True, recursive=True):
        classes = el.get("class") or []
        if "wf-label" in classes and "mod-large" in classes:
            parsed = _parse_date_label(el.get_text(" ", strip=True))
            if parsed:
                current_date = parsed
        elif "match-item" in classes:
            row = _parse_match_item(el, current_date)
            if row is not None:
                matches.append(row)
    return matches


def fetch_upcoming_matches() -> list[dict]:
    """vlr.gg's live /matches listing IS the schedule -- see module
    docstring. maps_won_a/b and winner will be None/None/None for genuinely
    upcoming matches (score divs render as an em-dash, non-digit text)."""
    resp = _client.get(MATCHES_URL)
    resp.raise_for_status()
    return parse_matches_from_html(resp.text)


def fetch_recent_results() -> list[dict]:
    """vlr.gg's /matches/results listing, first page only (most recent
    results -- enough to catch a match transitioning from upcoming to
    decided between poll cycles; a full historical backfill is a separate,
    not-yet-built scraping effort, see elo_service_valorant.py's own
    docstring)."""
    resp = _client.get(RESULTS_URL)
    resp.raise_for_status()
    return parse_matches_from_html(resp.text)

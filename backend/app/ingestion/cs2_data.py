"""CS2 match data ingestion -- scrapes liquipedia.net directly. Confirmed
live 2026-07-19: Liquipedia's counterstrike wiki loads with ZERO Cloudflare/
bot gating (unlike HLTV, which blocked the earlier standalone CS2 betting-
model project -- that block is HLTV-specific, not CS2-wide). Liquipedia does
NOT expose a Cargo/cargoquery API action (confirmed live via
api.php?action=paraminfo -- "cargoquery" isn't in the real action list, and
Special:CargoQuery returns "No such special page"; Liquipedia runs its own
custom API modules instead, e.g. lpstatisticsapi, none of which expose match
schedules), so this scrapes the Liquipedia:Matches page's rendered HTML
directly instead, same "plain HTML scraping, no paid API" approach as
ufcstats_client.py.

Real DOM structure confirmed live 2026-07-19 (saved+inspected raw HTML, not
assumed): each match is a `<div class="match-info">` containing:
  - `.match-info-countdown .timer-object[data-timestamp]` -- a real UNIX
    epoch second (NOT a guessed local-time string like vlr.gg's listing --
    Liquipedia gives a precise, unambiguous UTC instant directly), plus
    `data-finished="finished"` once the match is decided.
  - `.match-info-header-opponent .block-team .name` -- team display name (x2)
    -- often an ABBREVIATED display form (e.g. "FLY" for "FlyQuest"); the
    team link's `title`/`href` carries the real full name, captured
    separately as team_a_full/team_b_full for matching (see
    market_matcher_cs2.py).
  - `.match-info-header-scoreholder-lower` -- "(Bo3)"/"(Bo5)"/"(Bo1)" text,
    giving best_of DIRECTLY on the schedule listing -- unlike Valorant/vlr.gg,
    no ladder-market backfill is needed here.
  - `.match-info-header-scoreholder-score` (x2) -- real map-count score once
    decided; `.match-info-header-winner` class marks the winning side.
  - `.match-info-tournament-name` -- tournament/event name.

No unique per-match numeric id is exposed on this listing (unlike vlr.gg's
URL-embedded match id) -- source_match_id is synthesized from the
tournament+team href slugs+timestamp, same "live:" synthetic-id fallback
category this app already uses elsewhere (e.g.
market_catalog_soccer.py::find_or_create_upcoming_match), just built from
real, stable Liquipedia identifiers rather than today's date.

Liquipedia:Matches shows BOTH upcoming AND recently-decided matches on the
SAME page (confirmed live) -- no separate /results page needed, unlike
vlr.gg's split /matches vs /matches/results."""
from __future__ import annotations

import datetime as dt
import logging
import time

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("cs2_data")

MATCHES_URL = "https://liquipedia.net/counterstrike/Liquipedia:Matches"

_client = httpx.Client(
    timeout=30.0,
    headers={"User-Agent": "nfl-edge-app/0.1 (personal research project; contact via GitHub)"},
)


def _slug(href: str | None) -> str:
    return (href or "").rsplit("/", 1)[-1]


def _team_info(opponent_div) -> dict | None:
    """REAL edge case found live (2026-07-19, a genuine scheduled EPL Series 8
    match): a team without its own Liquipedia page yet is rendered as a
    "redlink" -- title becomes "Atreides (page does not exist)" and href
    becomes an "action=edit&redlink=1" URL instead of a real page link.
    Falls back to the display_name text (still real, just not yet a full
    wiki article) for both full_name and the de-dupe slug in that case."""
    block = opponent_div.find("div", class_="block-team")
    if block is None:
        return None
    name_span = block.find("span", class_="name")
    link = name_span.find("a") if name_span else None
    if link is None:
        return None
    display_name = link.get_text(strip=True)
    href = link.get("href") or ""
    title = link.get("title") or ""
    if "action=edit" in href or "does not exist" in title:
        return {"display_name": display_name, "full_name": display_name, "slug": display_name.lower().replace(" ", "_")}
    return {"display_name": display_name, "full_name": title or display_name, "slug": _slug(href)}


def _best_of(match_info) -> int | None:
    lower = match_info.find("span", class_="match-info-header-scoreholder-lower")
    if lower is None:
        return None
    text = lower.get_text(strip=True)
    for n in (1, 3, 5, 7, 9):
        if f"Bo{n}" in text:
            return n
    return None


def _scores(match_info) -> tuple[int | None, int | None]:
    scoreholder = match_info.find("div", class_="match-info-header-scoreholder")
    if scoreholder is None:
        return None, None
    score_spans = scoreholder.find_all("span", class_="match-info-header-scoreholder-score")
    if len(score_spans) != 2:
        return None, None
    texts = [s.get_text(strip=True) for s in score_spans]
    return (int(texts[0]) if texts[0].isdigit() else None, int(texts[1]) if texts[1].isdigit() else None)


def _maps_from_scoreholder(a, b, best_of):
    """Convert a best-of-1 scoreholder into MAPS WON.

    THE FIELD MEANS TWO DIFFERENT THINGS AND ONLY ONE OF THEM IS MAPS. In a Bo3
    the scoreholder reads 2-1 and those are maps. In a Bo1 there is only one map,
    so Liquipedia puts the ROUND score there instead -- a real stored row reads
    13-1. Both were written straight into maps_won_a/b, a column every grader
    reads as maps.

    Nothing had noticed because CS2 map scores were 93% missing anyway, so the
    one poisoned row never met a grader. It would have: series_handicap on a Bo1
    would compute a 12-map margin and clear any line, and series_total would call
    a one-map match a 14-map series.

    A Bo1 winner won 1 map to 0, always. Only rewrites when the value cannot be a
    map count (max > 1); a scoreholder already reading 1-0 passes through
    untouched, and an unknown best_of is left alone rather than guessed at.
    """
    if best_of != 1 or a is None or b is None:
        return a, b
    if max(a, b) <= 1:
        return a, b
    if a == b:
        return None, None   # a tied round score decides no map -- say nothing
    return (1, 0) if a > b else (0, 1)


def _per_map_results(match_info) -> list[dict]:
    """Extracts real per-map results (map name + winner) from a bracket
    popup's own detailed score breakdown, when present -- confirmed live
    2026-07-20 (map-pool-aware rating investigation): `.brkts-popup-body-
    grid-row` is one row per real map played, with a real `.generic-label
    [data-label-type]` on EITHER side (team_a's own result first, team_b's
    own mirrored result last -- "result-win"/"result-loss", never both win
    or both loss for a genuinely decided map) and the real map name in the
    middle `.brkts-popup-body-grid-row-detail`'s own `<a title="...">` link
    to Liquipedia's own real map page (e.g. "Ancient", "Nuke", "Mirage").
    Returns [] if this popup has no such detailed grid -- the live
    Liquipedia:Matches page's own compact match-info div never has this,
    only a genuine bracket-page popup does, same "historical crawl only for
    now" scope as this app's other per-map-name work."""
    rows = match_info.find_all("div", class_="brkts-popup-body-grid-row")
    results = []
    for row in rows:
        labels = row.find_all("div", class_="generic-label", recursive=False)
        if len(labels) != 2:
            continue
        label_a, label_b = labels[0].get("data-label-type"), labels[1].get("data-label-type")
        if label_a == "result-win":
            winner = "team_a"
        elif label_b == "result-win":
            winner = "team_b"
        else:
            continue
        detail = row.find("div", class_="brkts-popup-body-grid-row-detail")
        map_link = detail.find("a", title=True) if detail else None
        if map_link is None:
            continue
        results.append({"map_name": map_link.get("title"), "winner": winner})
    return results


def _parse_match_info(match_info, default_event_name: str = "", default_tournament_slug: str = "") -> dict | None:
    """Generic across BOTH real DOM shapes this app scrapes from
    liquipedia.net: the live Liquipedia:Matches page's own `<div
    class="match-info">` wrapper, AND a tournament (sub)page's bracket/
    matchlist popup (`<div class="brkts-popup ... brkts-match-info-popup">`)
    -- confirmed live 2026-07-19 (see scripts/build_cs2_match_cache.py's own
    docstring for the historical-crawl discovery): both wrappers contain the
    exact same internal `match-info-countdown`/`match-info-header`/etc.
    children, just under a differently-named OUTER class, which this
    function never actually checks -- it only ever searches for children BY
    CLASS within whatever container is passed in, so both shapes parse
    identically. Bracket popups have no `.match-info-tournament-name` child
    (the tournament is already known from the page being crawled, not
    repeated per-match) -- default_event_name/default_tournament_slug cover
    that case; the live Matches page's own real tournament span still wins
    when present (never overridden)."""
    header = match_info.find("div", class_="match-info-header")
    if header is None:
        return None
    opponents = header.find_all("div", class_="match-info-header-opponent")
    if len(opponents) != 2:
        return None
    team_a, team_b = _team_info(opponents[0]), _team_info(opponents[1])
    if team_a is None or team_b is None:
        return None

    timer = match_info.find("span", class_="timer-object")
    timestamp = timer.get("data-timestamp") if timer else None
    if not timestamp or not timestamp.isdigit():
        return None
    start_dt = dt.datetime.fromtimestamp(int(timestamp), tz=dt.timezone.utc)
    is_finished = bool(timer.get("data-finished"))

    tournament_span = match_info.find("span", class_="match-info-tournament-name")
    tournament_link = tournament_span.find("a") if tournament_span else None
    event_name = tournament_link.get_text(strip=True) if tournament_link else default_event_name
    tournament_slug = _slug(tournament_link.get("href")) if tournament_link else default_tournament_slug

    best_of = _best_of(match_info)
    score_a, score_b = _scores(match_info) if is_finished else (None, None)
    score_a, score_b = _maps_from_scoreholder(score_a, score_b, best_of)
    winner = None
    if is_finished:
        winner_div = header.find("div", class_="match-info-header-winner")
        if winner_div in opponents:
            winner = "team_a" if opponents.index(winner_div) == 0 else "team_b"

    source_match_id = f"{tournament_slug}:{team_a['slug']}:{team_b['slug']}:{timestamp}"

    return {
        "source": "liquipedia",
        "source_match_id": source_match_id,
        "event_name": event_name,
        "match_date": start_dt.date().isoformat(),
        "estimated_start_time": start_dt.isoformat(),
        "team_a": team_a["full_name"],
        "team_a_display": team_a["display_name"],
        "team_b": team_b["full_name"],
        "team_b_display": team_b["display_name"],
        "best_of": best_of,
        "maps_won_a": score_a,
        "maps_won_b": score_b,
        "winner": winner,
        "maps": _per_map_results(match_info) if is_finished else [],
    }


# --- 429 COOLDOWN -------------------------------------------------------------
# Liquipedia is rate-limiting this endpoint (confirmed live 2026-08-11: a single
# request returned 429). The poller calls this every 5 minutes, so without a
# cooldown the app fires ~288 refused requests a day at a host that has ALREADY
# IP-banned it once, over a CoD crawl. That ban's lesson was recorded at the time
# as "retry-forever was worse than the ban", and this module was still doing
# exactly that.
#
# Backs off exponentially from 30 minutes to a 12-hour ceiling, honouring
# Retry-After when the server sends one, and resets the moment a request
# succeeds. Returns [] while cooling down rather than raising: CS2 does not
# depend on this feed any more (measured the same day -- 0 of 78 upcoming
# fixtures come from Liquipedia, they arrive via Kalshi/Polymarket, and best_of
# is 73/78 covered without it), so a quiet skip is honest rather than lossy.
_COOLDOWN_MIN_S = 30 * 60
_COOLDOWN_MAX_S = 12 * 60 * 60
_cooldown_until = 0.0
_cooldown_step = 0.0


def _in_cooldown() -> bool:
    return time.monotonic() < _cooldown_until


def _start_cooldown(retry_after: str | None) -> None:
    global _cooldown_until, _cooldown_step
    wait = None
    if retry_after:
        try:
            wait = float(retry_after)
        except ValueError:
            wait = None
    if wait is None:
        _cooldown_step = min(max(_cooldown_step * 2, _COOLDOWN_MIN_S), _COOLDOWN_MAX_S)
        wait = _cooldown_step
    _cooldown_until = time.monotonic() + wait
    log.warning("liquipedia rate-limited; backing off %.0f min (no requests until then)", wait / 60)


def _clear_cooldown() -> None:
    global _cooldown_until, _cooldown_step
    _cooldown_until, _cooldown_step = 0.0, 0.0


def fetch_matches() -> list[dict]:
    """Liquipedia:Matches's own live listing IS the schedule (both upcoming
    AND recently-decided) -- see module docstring.

    Returns [] while rate-limited; see the cooldown block above."""
    if _in_cooldown():
        log.info("liquipedia still in cooldown; skipping fetch")
        return []
    resp = _client.get(MATCHES_URL)
    if resp.status_code == 429:
        _start_cooldown(resp.headers.get("Retry-After"))
        return []
    resp.raise_for_status()
    _clear_cooldown()
    soup = BeautifulSoup(resp.text, "html.parser")
    matches = []
    for match_info in soup.find_all("div", class_="match-info"):
        row = _parse_match_info(match_info)
        if row is not None:
            matches.append(row)
    return matches


def parse_matches_from_html(html: str, default_event_name: str = "", default_tournament_slug: str = "") -> list[dict]:
    """Used by scripts/build_cs2_match_cache.py for the historical crawl --
    a tournament (sub)page's real match popups are found by locating every
    `match-info-header` div and taking ITS OWN PARENT as the match_info-
    shaped container (confirmed live: in both the live-page and
    bracket-popup DOM shapes, `.match-info-countdown` and `.match-info-
    header` are siblings under the same immediate parent -- `.parent` reaches
    that shared container from either child reliably). Also matches the live
    page's own `<div class="match-info">` wrapper directly (its OWN
    `match-info-header` child's parent IS that same div), so this one
    function correctly covers both DOM shapes without needing to special-
    case which page produced the HTML."""
    soup = BeautifulSoup(html, "html.parser")
    seen_ids: set[str] = set()
    matches = []
    for header in soup.find_all("div", class_="match-info-header"):
        container = header.parent
        if container is None:
            continue
        row = _parse_match_info(container, default_event_name, default_tournament_slug)
        if row is not None and row["source_match_id"] not in seen_ids:
            seen_ids.add(row["source_match_id"])
            matches.append(row)
    return matches

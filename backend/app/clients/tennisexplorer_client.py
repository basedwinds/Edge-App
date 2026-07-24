"""tennisexplorer.com client -- free, unauthenticated, no bot/Cloudflare gate
(confirmed live 2026-07-18: plain httpx + a browser User-Agent loads real
HTML tables, no PoW/JS challenge like ufcstats.com needs). This is the
source that closes the "zero Challenger/ITF training data" gap the user's
earlier standalone tennis-model project hit -- confirmed live: real
Challenger-level results (matching real currently-open Kalshi/Polymarket
matches, e.g. Cordenons/Granby) going back to at least 2018, WITH real
embedded bookmaker odds (`coursew`/`course` table cells) at Challenger AND
ITF level -- something tennis-data.co.uk never has at any tier below tour
level. tennis-data.co.uk (see tennisdata_client.py) remains the source for
ATP/WTA tour-level (cleaner structured xlsx, no scrape fragility, already
has point-in-time rank); this client is used ONLY for Challenger/ITF.

Real structural finding from live testing: the site's `?type=` query param
(challenger-men, itf-women, etc.) does NOT actually filter server-side --
`type=all`, `type=challenger-men`, and `type=itf-men` for the same date all
returned byte-for-byte the same page (confirmed via diff). The real tier
signal is the TOURNAMENT SLUG itself: "-challenger" in the slug means
Challenger level, "-itf" means ITF level, anything else under a plain
`/atp-men/` or `/wta-women/` URL suffix is tour-level (already covered by
tennis-data.co.uk, skipped here to avoid double-counting/a second name-key
system for the same real matches). This means ONE request per day (not one
per tier) returns every tier's results for that day.

Known simplification, not yet fixed: this results-day page does NOT expose
per-tournament SURFACE (unlike the live "today's matches" listing page,
which shows a surface color swatch) -- surface would need a second request
per unique tournament slug (cheap: far fewer tournaments than matches, but
not built in this pass). Challenger/ITF rows from this source ship with
surface=None; tour-level rows keep tennis-data.co.uk's real Surface value
undisturbed. Flagged rather than guessed.
"""
from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.tennisexplorer.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def classify_tier(tourney_slug: str) -> str:
    if "-challenger" in tourney_slug:
        return "challenger"
    if "-itf" in tourney_slug:
        return "itf"
    return "tour"


def _tour_from_suffix(suffix: str) -> str | None:
    if suffix.startswith("atp-"):
        return "atp"
    if suffix.startswith("wta-"):
        return "wta"
    return None


class TennisExplorerClient:
    def __init__(self) -> None:
        self._client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TennisExplorerClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_tournament_draw(self, slug: str, year: int, tour_suffix: str) -> tuple[list[list[str]] | None, str | None]:
        """Real, freely-scrapable tournament bracket PLUS real surface
        (confirmed live 2026-07-19, e.g. /bastad/2026/atp-men/) -- used for
        tournament-winner futures (see bracket_sim_tennis.py). Both come off
        the SAME page (one request, not two) -- no bot gate, same as the
        rest of this client. Returns (rounds, surface); either half can be
        None independently (a tournament can have a surface listed with no
        draw yet, or vice versa)."""
        resp = self._client.get(f"{BASE_URL}/{slug}/{year}/{tour_suffix}/")
        return parse_draw_html(resp.text), parse_tournament_surface(resp.text)

    def get_results_day(self, year: int, month: int, day: int) -> list[dict]:
        """One request returns every tier's completed matches for this
        single calendar day (see module docstring on why `type=all` is used
        regardless of tier -- the param doesn't actually filter). Only
        Challenger/ITF rows are meaningful to the caller (see
        app/ingestion/tennis_data.py) -- tour-level rows are still parsed
        and returned here (cheap, already have the HTML) so a future caller
        could cross-check against tennis-data.co.uk if ever needed, but
        aren't currently persisted twice."""
        resp = self._client.get(
            f"{BASE_URL}/results/", params={"type": "all", "year": year, "month": month, "day": day}
        )
        match_date = f"{year:04d}-{month:02d}-{day:02d}"
        return parse_results_html(resp.text, match_date=match_date)


def parse_results_html(html: str, match_date: str | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.result")
    if not table:
        return []

    matches: list[dict] = []
    current_tourney: dict | None = None
    pending_row: dict | None = None

    for row in table.select("tr"):
        classes = row.get("class") or []
        if "head" in classes:
            link = row.select_one("td.t-name a")
            if not link or not link.get("href"):
                current_tourney = None
                continue
            href = link["href"].strip("/")
            parts = href.split("/")  # e.g. "nottingham-challenger/2018/atp-men"
            if len(parts) < 3:
                current_tourney = None
                continue
            slug, _season_str, suffix = parts[0], parts[1], parts[2]
            tour = _tour_from_suffix(suffix)
            if tour is None:
                current_tourney = None
                continue
            current_tourney = {
                "slug": slug,
                "tier": classify_tier(slug),
                "tour": tour,
                "tourney_name": link.get_text(strip=True),
            }
            pending_row = None
            continue

        if current_tourney is None:
            continue

        name_link = row.select_one("td.t-name a")
        result_cell = row.select_one("td.result")
        if not name_link or result_cell is None:
            continue

        score_cells = [c.get_text(strip=True) for c in row.select("td.score")]
        row_text = row.get_text(" ", strip=True)
        player = {
            "name": name_link.get_text(strip=True),
            "slug": name_link["href"].strip("/").rsplit("/", 1)[-1] if name_link.get("href") else None,
            "sets_won": result_cell.get_text(strip=True),
            "scores": score_cells,
            "is_retirement": bool(re.search(r"\bret\.?\b", row_text, re.IGNORECASE)),
        }
        odds_a = row.select_one("td.coursew")
        odds_b = row.select_one("td.course")

        if pending_row is None:
            pending_row = {
                "player": player,
                "odds_a": _parse_odds(odds_a.get_text(strip=True)) if odds_a else None,
                "odds_b": _parse_odds(odds_b.get_text(strip=True)) if odds_b else None,
                "match_detail_url": _match_detail_url(row),
            }
        else:
            matches.append(_build_match(current_tourney, pending_row, player, match_date))
            pending_row = None

    return matches


_SCORE_OR_WALKOVER_RE = re.compile(
    r"^\d+(\(\d+\))?-\d+(\(\d+\))?(,\s*\d+(\(\d+\))?-\d+(\(\d+\))?)*$|^w/o$", re.IGNORECASE
)


def parse_draw_html(html: str) -> list[list[str]] | None:
    """Parses tennisexplorer's tournament draw tab (confirmed live 2026-07-19
    against a real in-progress ATP 250 draw) -- an absolutely-positioned div
    grid, one column per round (`left` offset = round index), real content
    starting at `top` > 10px (the row at top=3 is just the round's text
    label, e.g. "1. round"/"quarterfinal"). Round 0 (the leftmost, widest
    column) is the FULL original bracket in play order -- every entrant's
    name (with seed/wildcard/qualifier tags like "[1]"/"[WC]"/"[Q]" still
    attached, harmless for name matching since a real player name never
    contains a literal "[") or the literal "bye". Every later round column
    interleaves each CONFIRMED occupant's name with the score of the match
    that got them there (skipped here via `_SCORE_OR_WALKOVER_RE` -- a
    walkover is real, not a score, but conveys no useful signal either) --
    the surviving names are returned in the SAME top-to-bottom bracket order
    as round 0, so round `r`'s occupant `i` is always the eventual winner of
    round `r-1`'s pair at indices `(2i, 2i+1)`. A round with fewer real
    occupants than its expected size (draw_size >> r) means that portion of
    the bracket hasn't finished yet -- see bracket_sim_tennis.py for how the
    caller uses this to start a Monte Carlo simulation from the deepest
    FULLY-resolved round rather than guessing at partial rounds.
    Returns None if the page has no draw section (tournament not found, or a
    round-robin/non-bracket format this parser doesn't support)."""
    soup = BeautifulSoup(html, "html.parser")
    draw_div = soup.select_one("#draw")
    if draw_div is None:
        return None

    by_left: dict[int, list[tuple[int, str]]] = {}
    for cell in draw_div.select("div"):
        style = cell.get("style", "")
        left_match = re.search(r"left:\s*(\d+)px", style)
        top_match = re.search(r"top:\s*(\d+)px", style)
        if not left_match or not top_match:
            continue
        text = re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()
        by_left.setdefault(int(left_match.group(1)), []).append((int(top_match.group(1)), text))

    # Real round columns have many entries; a stray 1-entry column (e.g. the
    # page's own "H2H" sidebar widget, confirmed live at a much larger `left`
    # offset than any real round) isn't one -- filtered out rather than
    # guessed at by position.
    lefts = sorted(k for k, v in by_left.items() if len(v) > 1)
    rounds: list[list[str]] = []
    for left in lefts:
        entries = sorted(by_left[left])
        names = [text for top, text in entries if top > 10 and text and not _SCORE_OR_WALKOVER_RE.match(text)]
        rounds.append(names)
    return rounds if rounds else None


def parse_tournament_surface(html: str) -> str | None:
    """Real surface for a tournament page (confirmed live 2026-07-19, e.g.
    Bastad -> "Clay") -- a small colored swatch (`td.s-color span[title]`)
    embedded in the page's own match-listing table, same Title-Case
    convention ("Hard"/"Clay"/"Grass") as tennis-data.co.uk's Surface column,
    which the rest of this app (elo_tennis.py's surface blending) already
    keys on verbatim -- confirmed matching casing, not just assumed.

    REAL BUG this fixes (caught live 2026-07-19, extending surface coverage
    past the 4 Grand Slams): a slug/year/tour_suffix that doesn't resolve to
    a real tournament does NOT 404 -- it 200s with an unrendered template
    page whose own `<title>` is the literal placeholder text
    "Tennis Explorer: [tournament]", confirmed live for 5 of 7 real
    currently-tracked tournament names tried (only 2 -- Palermo, Bastad --
    resolved to a real page). That placeholder page STILL has a
    `td.s-color span[title]` element somewhere on it (unrelated sidebar
    content), so the old version of this function returned a confident-
    looking "Clay" for EVERY one of those 5 failed lookups -- confirmed by
    testing a deliberately-nonsensical slug and getting the exact same
    "Clay" result. Guards against feeding a bogus surface into either the
    live surface backfill or the tournament-winner futures bracket sim
    (bracket_sim_tennis.py), which has been calling this same function
    since before this bug was found."""
    if "Tennis Explorer: [tournament]" in html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    span = soup.select_one("td.s-color span")
    return span.get("title") if span and span.get("title") else None


def _parse_odds(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _match_detail_url(row) -> str | None:
    link = row.select_one("a[href*='match-detail']")
    return link["href"] if link else None


def _build_match(tourney: dict, first: dict, second_player: dict, match_date: str | None) -> dict:
    p1, p2 = first["player"], second_player
    try:
        sets_a, sets_b = int(p1["sets_won"]), int(p2["sets_won"])
    except ValueError:
        sets_a = sets_b = None
    winner = None
    if sets_a is not None and sets_b is not None and sets_a != sets_b:
        winner = "a" if sets_a > sets_b else "b"
    return {
        "source_match_id": first["match_detail_url"] or f"{tourney['slug']}:{p1['slug']}:{p2['slug']}:{match_date}",
        "match_date": match_date,
        "tourney_slug": tourney["slug"],
        "tourney_name": tourney["tourney_name"],
        "tier": tourney["tier"],
        "tour": tourney["tour"],
        "player_a_name": p1["name"],
        "player_a_slug": p1["slug"],
        "player_b_name": p2["name"],
        "player_b_slug": p2["slug"],
        "winner": winner,
        "is_retirement": p1["is_retirement"] or p2["is_retirement"],
        "score": " ".join(f"{a}-{b}" for a, b in zip(p1["scores"], p2["scores"]) if a.strip() or b.strip()),
        "odds_a": first["odds_a"],
        "odds_b": first["odds_b"],
    }

"""Per-map Valorant results from vlr.gg, so map_winner bets can settle.

Companion to lol_map_results.py. LoL needed gol.gg because its map order had to
be INFERRED from ascending game ids; Valorant does not, because vlr.gg publishes
every map of a series on the match's own page in play order.

Why a fetch per match is acceptable here where it wasn't for the match list:
valorant_data.py reads vlr.gg's LIST page, which renders only the series score
(2-0), so per-map results were simply not available there. The match page has
them, and ValorantMatch.source_match_id already stores the real vlr match id, so
the URL is https://www.vlr.gg/{source_match_id} with nothing to search for. Only
matches that still need maps are fetched, so the cost is bounded by the backlog,
not by the catalogue.

Two structural details, both confirmed live:
  - `.vm-stats-game` is one block per map, in play order, PLUS one aggregate
    block with data-game-id="all" that must be skipped or it would be counted
    as an extra map.
  - team names inside those blocks are vlr.gg's own, and ValorantMatch rows for
    vlr-sourced matches were built from the same source, so they match exactly.
    Orientation is still done by name rather than by position, because the
    match page's team order is its own.

The safety property that makes this trustworthy is a CONSISTENCY CHECK rather
than a guard on the parse: the per-map tally derived here must equal the
maps_won_a/maps_won_b already stored for the series from the independent list
scrape. If the two disagree -- a mis-parse, a missed map, a reversed
orientation -- nothing is written for that match. A map bet left pending costs
nothing; a map bet graded off a mis-ordered series pays out the wrong side.
"""
from __future__ import annotations

import logging
import time

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("valorant_map_results")

MATCH_URL = "https://www.vlr.gg/{match_id}"
REQUEST_DELAY_SECONDS = 1.2
MAX_MATCHES_PER_CYCLE = 40

_client = httpx.Client(
    timeout=30.0,
    follow_redirects=True,
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"},
)


def _int(text: str | None) -> int | None:
    try:
        return int((text or "").strip())
    except (TypeError, ValueError):
        return None


def parse_map_results(html: str) -> list[dict]:
    """[{map_number, team_a_name, team_b_name, score_a, score_b}] in play order.

    Returns [] for anything unexpected -- a block without exactly two named
    teams and two integer scores is skipped rather than guessed at, and a
    skipped block would break the numbering, so the whole parse is abandoned.
    """
    soup = BeautifulSoup(html, "html.parser")
    blocks = [g for g in soup.select(".vm-stats-game") if g.get("data-game-id") != "all"]
    out = []
    for number, block in enumerate(blocks, start=1):
        names = [x.get_text(" ", strip=True) for x in block.select(".team-name")]
        scores = [_int(x.get_text(strip=True)) for x in block.select(".score")]
        if len(names) != 2 or len(scores) != 2 or any(s is None for s in scores):
            return []  # an unreadable map would renumber every map after it
        if scores[0] == scores[1]:
            return []  # no winner: an unplayed or forfeited map
        out.append({
            "map_number": number,
            "team_a_name": names[0],
            "team_b_name": names[1],
            "score_a": scores[0],
            "score_b": scores[1],
        })
    return out


def fetch_map_results(source_match_id: str) -> list[dict]:
    """Network fetch for one match -- call OUTSIDE the DB write lock."""
    resp = _client.get(MATCH_URL.format(match_id=source_match_id))
    if resp.status_code != 200:
        return []
    return parse_map_results(resp.text)


def collect_map_results(matches, max_matches: int = MAX_MATCHES_PER_CYCLE) -> dict:
    """{valorant_match_id: [map rows]} for the given matches. Network only."""
    out: dict[int, list[dict]] = {}
    for match in matches[:max_matches]:
        if not match.source_match_id:
            continue
        try:
            rows = fetch_map_results(match.source_match_id)
        except httpx.HTTPError:
            continue  # transient: retried next cycle, nothing recorded
        if rows:
            out[match.id] = rows
        time.sleep(REQUEST_DELAY_SECONDS)
    return out

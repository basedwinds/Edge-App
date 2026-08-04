"""Per-map LoL results, so "who wins map N" bets can settle.

bet_settlement's esports grader table carried the note that map_winner was
"deliberately absent: we store only the SERIES map score (maps_won_a/b), never
per-map winners". That was accurate -- LolMap/Cs2Map/ValorantMap and their
upsert helpers existed but nothing ever called them, so all three tables were
empty. It left 219 pending LoL map_winner bets with no path to settling.

gol.gg closes it for LoL specifically, because it publishes one PAGE PER GAME
and a game is a map. The series a game belongs to is (date, unordered team
pair) -- the same grouping lol_lower_tier.py already uses -- and the map ORDER
within that series is the ascending game id, since gol.gg allocates ids in play
order.

That ordering is the whole risk: if one game of a series is missing from the
cache, every later map shifts down a number and map 2 gets graded with map 3's
result. A wrong grade pays out the wrong side, so this refuses to write ANY map
for a series unless every id in the series' own id range was actually probed.
A probed-but-empty id is fine (a real gap in gol.gg's id space, cached as null);
an id never fetched is not, because it could be a game of this very series.

CS2 and Valorant are NOT covered and cannot be from here:
  - CS2's match source is Liquipedia, which is Cloudflare-gated.
  - Valorant's is vlr.gg's match LIST, which renders only the series score
    (see valorant_data.py) -- per-map results would need a fetch per match page.
Their map_winner bets (30 and 116) stay unsettleable, which is why the grader
is registered for LoL only.
"""
from __future__ import annotations

import collections
import json
import logging
import re
from pathlib import Path

log = logging.getLogger("lol_map_results")

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
CACHE_PATH = DATA_DIR / "lol_game_lineups_cache.json"


def _norm(x: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", x.lower()) if x else ""


def golgg_series_maps() -> dict[tuple, list[dict]]:
    """(date, sorted normalized pair) -> per-map rows in play order.

    Each row is {"map_number", "winner_name", "team_a_name", "team_b_name"},
    where the names are gol.gg's own spellings and winner_name is one of them.
    Series whose game ids are not provably contiguous are omitted entirely
    rather than returned with a possibly-shifted numbering.
    """
    if not CACHE_PATH.exists():
        return {}
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.exception("gol.gg game cache unreadable")
        return {}

    probed = {int(k) for k in raw if str(k).isdigit()}
    games: dict[int, dict] = {int(k): v for k, v in raw.items() if str(k).isdigit() and v}

    grouped: dict[tuple, list[int]] = collections.defaultdict(list)
    for gid, g in games.items():
        teams = g.get("teams") or []
        date = g.get("date")
        if len(teams) != 2 or not date:
            continue
        grouped[(date, tuple(sorted((_norm(teams[0]), _norm(teams[1])))))].append(gid)

    out: dict[tuple, list[dict]] = {}
    for key, gids in grouped.items():
        gids.sort()
        # Every id between the first and last game of this series must have been
        # probed. A hole means an unfetched id that could belong here, which
        # would silently renumber the maps after it.
        if any(i not in probed for i in range(gids[0], gids[-1] + 1)):
            continue
        rows = []
        for n, gid in enumerate(gids, start=1):
            g = games[gid]
            blue, red = g["teams"]
            rows.append({
                "map_number": n,
                "winner_name": blue if g.get("blue_won") else red,
                "team_a_name": blue,
                "team_b_name": red,
            })
        out[key] = rows
    return out


def maps_for_series(series_maps: dict, match_date: str | None,
                    team_a: str | None, team_b: str | None) -> list[dict]:
    """Per-map rows for one gol.gg series, looked up by its own date and pair."""
    if not match_date:
        return []
    key = (str(match_date)[:10], tuple(sorted((_norm(team_a), _norm(team_b)))))
    return series_maps.get(key, [])

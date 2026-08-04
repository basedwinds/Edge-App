"""Parser for a gol.gg per-game page (`/game/stats/{id}/page-game/`).

Moved here from scripts/build_lol_game_lineup_cache.py, which was a one-off
cache builder until lol_results_golgg.py started maintaining the same cache on
a schedule. Two consumers now depend on these selectors, so they live in one
place; the script imports this rather than carrying its own copy.

One page carries everything needed, so it is a single fetch per game:
  - real date (2025-07-27)
  - full team names WITH result ("T1 - WIN" / "Nongshim RedForce - LOSS"),
    which is what lets this join to this app's own LoL match rows
  - the tournament name, from the page title ("LCK 2025 Rounds 3-5 WEEK10")
  - both 5-player lineups, as `players/player-stats/` anchors in document
    order: the FIRST five are blue side, the LAST five red. Verified live
    against gol.gg's own `page-summary` tables on multiple real games.

Returns None rather than a partial row for ids with no game (gaps in the id
space are real, confirmed live at 75000) and for off-10 scoreboards, so a
caller never stores a guessed lineup or an unattributable result.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

LINEUP_SIZE = 5

_PLAYER_HREF = re.compile(r"players/player-stats/")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def team_and_result(text: str):
    """'T1 - WIN' -> ('T1', True). Splits on the LAST ' - ' so team names
    containing a hyphen survive intact."""
    if " - " not in text:
        return None, None
    name, _, res = text.rpartition(" - ")
    res = res.strip().upper()
    if res not in ("WIN", "LOSS"):
        return None, None
    return name.strip(), res == "WIN"


def parse_game(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    blue_el = soup.select_one(".blue-line-header")
    red_el = soup.select_one(".red-line-header")
    if blue_el is None or red_el is None:
        return None  # gap in the id space, or a page with no real game
    blue, blue_won = team_and_result(blue_el.get_text(" ", strip=True))
    red, red_won = team_and_result(red_el.get_text(" ", strip=True))
    if not blue or not red or blue_won is None or red_won is None:
        return None

    dates = _DATE.findall(html)
    if not dates:
        return None

    names = [a.get_text(strip=True) for a in soup.find_all("a", href=_PLAYER_HREF)]
    names = [n for n in names if n]
    if len(names) != LINEUP_SIZE * 2:
        return None  # off-10 scoreboard -> unknown rather than a guessed lineup

    title_el = soup.find("title")
    title = title_el.get_text(strip=True) if title_el else ""
    # "T1 vs NS summary - LCK 2025 Rounds 3-5 WEEK10 - Game of Legends"
    tournament = title.split(" - ")[1].strip() if title.count(" - ") >= 2 else None

    return {
        "date": dates[0],
        "teams": [blue, red],
        "blue_won": blue_won,
        "lineups": [names[:LINEUP_SIZE], names[LINEUP_SIZE:]],
        "tournament": tournament,
    }

"""One-off cache builder for CS2's real per-EVENT team rosters, for the
player-level rating build (2026-07-21, user-requested: "player level models").

REAL FIND (2026-07-21 feasibility check, task #22): Liquipedia's CS2
TOURNAMENT pages carry a full participant roster per team
(`.team-participant-card`), so the lineup that played a given event is
reachable in ~94 page fetches -- on the exact URLs
scripts/build_cs2_match_cache.py already crawls -- instead of one fetch per
match (8,839). Confirmed live on IEM Cologne 2026: Team Vitality ->
apEX/ZywOo/flameZ/mezii/ropz, with the coach (XTQZZZ) correctly separated
by its own `.team-participant-card__member-role-right` label.

The real approximation being made, stated honestly: a team's roster is
assumed STABLE for the duration of one event. That is largely true (rosters
change between events, not usually mid-tournament) but it is an
approximation, not a per-match ground truth -- a mid-event stand-in would be
missed. Cross-checkable against data/cs2_transfer_history_cache.json (14,849
real dated transfer events) if a specific event ever looks wrong. The
alternative (real per-match lineups) needs a per-match fetch this deliberately
avoids.

Only the MAIN roster is taken (`[data-toggle-area-content="1"]`) -- Liquipedia
renders "Subs" in a second toggle area, which is deliberately NOT collected:
a sub who didn't play shouldn't dilute a lineup's rating.

Run: backend/.venv/Scripts/python.exe scripts/build_cs2_event_roster_cache.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
TOURNAMENT_LIST_PATH = DATA_DIR / "cs2_tournament_list_cache.json"
OUTPUT_PATH = DATA_DIR / "cs2_event_rosters_cache.json"

BASE_URL = "https://liquipedia.net/counterstrike"
REQUEST_DELAY_SECONDS = 2.0  # same politeness delay as every other Liquipedia crawler in this app

_client = httpx.Client(
    timeout=30.0,
    headers={"User-Agent": "nfl-edge-app/0.1 (personal research project; contact via GitHub)"},
)

# Any member carrying one of these role labels is staff, not a player who
# actually played -- confirmed live (Coach). Compared case-insensitively.
NON_PLAYER_ROLES = {"coach", "assistant coach", "manager", "analyst", "head coach"}


def parse_rosters(html: str) -> dict[str, list[str]]:
    """{team_name: [player, ...]} for one real tournament page."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, list[str]] = {}
    for card in soup.select(".team-participant-card"):
        name_el = card.select_one(".team-participant-card__opponent .name a")
        if name_el is None:
            continue
        team = name_el.get_text(strip=True)
        if not team:
            continue
        # REAL BUG fixed here (2026-07-21, caught before trusting the output):
        # this used to REQUIRE `[data-toggle-area-content="1"]` and skip the
        # card entirely when absent -- but that wrapper only exists on pages
        # that render a Main/Subs switcher. Pages without subs list their
        # members directly in the card, so every such tournament silently
        # produced ZERO rosters (measured live: 78 of 94 tournaments empty,
        # including real ones like CS Asia Championships 2026 with 17 fully
        # populated teams). Now: use the Main toggle area when it exists (to
        # keep excluding subs), else fall back to the whole card.
        main = card.select_one('[data-toggle-area-content="1"]') or card
        players: list[str] = []
        for member in main.select(".team-participant-card__member"):
            role_el = member.select_one(".team-participant-card__member-role-right")
            if role_el is not None and role_el.get_text(strip=True).lower() in NON_PLAYER_ROLES:
                continue
            a = member.select_one(".block-player span.name a")
            if a is None:
                continue
            p = a.get_text(strip=True)
            if p and p not in players:
                players.append(p)
        if players:
            # Keep whatever real count the page lists -- don't force 5. A real
            # page can legitimately show 4 or 6 (stand-in periods, listed
            # 6-man rosters); silently padding/truncating would be inventing
            # data. The consumer decides how to handle off-5 lineups.
            out[team] = players
    return out


def main():
    tournaments = json.loads(TOURNAMENT_LIST_PATH.read_text(encoding="utf-8"))
    cache = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")) if OUTPUT_PATH.exists() else {}
    remaining = [t for t in tournaments if t["slug"] not in cache]
    print(f"{len(tournaments)} tournaments, {len(cache)} already cached, {len(remaining)} to fetch")

    for i, t in enumerate(remaining):
        slug = t["slug"]
        print(f"[{i + 1}/{len(remaining)}] {t['name']}...", end=" ", flush=True)
        try:
            resp = _client.get(f"{BASE_URL}/{slug}")
            resp.raise_for_status()
        except httpx.HTTPError as e:
            print(f"FAILED ({e}), will retry next run")
            continue
        rosters = parse_rosters(resp.text)
        cache[slug] = rosters
        sizes = [len(v) for v in rosters.values()]
        n5 = sum(1 for s in sizes if s == 5)
        print(f"{len(rosters)} teams ({n5} with exactly 5 players)")
        OUTPUT_PATH.write_text(json.dumps(cache), encoding="utf-8")
        time.sleep(REQUEST_DELAY_SECONDS)

    total_teams = sum(len(v) for v in cache.values())
    print(f"\nDone. {len(cache)} tournaments, {total_teams} team-rosters cached at {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

"""Resolves the real LoL lineup for a match, from gol.gg per-game scoreboards
(data/lol_game_lineups_cache.json, see scripts/build_lol_game_lineup_cache.py
-- the source that bypassed Leaguepedia's rate limit).

gol.gg has no stable match id shared with this app's Leaguepedia-sourced
cache, so games are indexed by (date, unordered normalized team pair). A LoL
series can be several games on one day between the same two teams; the
lineup for that series is the most common 5 across those games (teams rarely
sub mid-series). Names are oriented onto team_a/team_b by NAME, never by
gol.gg's blue/red order.

Two lookups, same split as valorant_lineups.py:
  - `for_match(date, team_a, team_b)` -- the training path (a played series).
  - `latest_for_team(team)` -- the live path (an upcoming match has no
    scoreboard, so use each team's most recent real lineup). Populated by
    `note_played()` during the chronological walk-forward, so it can never
    see a lineup from the future.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "data"
LINEUP_CACHE_PATH = DATA_DIR / "lol_game_lineups_cache.json"

LINEUP_SIZE = 5


def _norm(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", x.lower()) if x else ""


class LolLineupResolver:
    def __init__(self, cache: dict | None = None):
        if cache is None:
            cache = json.loads(LINEUP_CACHE_PATH.read_text(encoding="utf-8")) if LINEUP_CACHE_PATH.exists() else {}
        self._idx: dict = defaultdict(list)
        for v in cache.values():
            if v:
                self._idx[(v["date"], frozenset((_norm(v["teams"][0]), _norm(v["teams"][1]))))].append(v)
        self._latest: dict[str, list[str]] = {}
        self._for_match_cache: dict = {}

    def for_match(self, match_date: str | None, team_a: str, team_b: str):
        if not match_date:
            return None, None
        # Two DIFFERENT keys. The index is keyed unordered, because a cache
        # entry can list the pair either way round and both must find it. The
        # memo must be keyed ORDERED, because its value is the ordered tuple
        # (lineup_for_team_a, lineup_for_team_b).
        #
        # REAL BUG this fixes (user-reported 2026-08-05): memoizing the ordered
        # result under the unordered key meant that when the same pair appears
        # twice on one date with team_a/team_b swapped -- which happens
        # routinely, a Bo3 is stored as several rows and the sides are not
        # consistently ordered -- the second row read the first row's tuple
        # back REVERSED and handed each team its opponent's roster. Live case:
        # "Gen.G vs DN SOOPers" on 2026-08-04 got DN SOOPers rated with Gen.G's
        # lineup (Kiin/Canyon/Chovy/Ruler/Duro), which via the 0.4-weight
        # player blend turned DRX from a 70.6% favourite into a 45.9% dog and
        # produced a staked $10 bet on the wrong side.
        idx_key = (match_date, frozenset((_norm(team_a), _norm(team_b))))
        memo_key = (match_date, _norm(team_a), _norm(team_b))
        if memo_key in self._for_match_cache:
            return self._for_match_cache[memo_key]
        games = self._idx.get(idx_key)
        result = (None, None)
        if games:
            result = (self._side(games, team_a), self._side(games, team_b))
        self._for_match_cache[memo_key] = result
        return result

    @staticmethod
    def _side(games, team):
        cnt = Counter()
        nt = _norm(team)
        for g in games:
            for i in (0, 1):
                if _norm(g["teams"][i]) == nt:
                    cnt[tuple(g["lineups"][i])] += 1
        if not cnt:
            return None
        lu = list(cnt.most_common(1)[0][0])
        return lu if len(lu) == LINEUP_SIZE else None

    def note_played(self, team_a: str, team_b: str, lineup_a, lineup_b) -> None:
        if lineup_a:
            self._latest[_norm(team_a)] = list(lineup_a)
        if lineup_b:
            self._latest[_norm(team_b)] = list(lineup_b)

    def latest_for_team(self, team: str):
        return self._latest.get(_norm(team))

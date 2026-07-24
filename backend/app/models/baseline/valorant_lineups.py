"""Resolves the real Valorant lineup for a match.

Source: data/valorant_match_lineups_cache.json -- vlr.gg's own per-match
scoreboards (see scripts/build_valorant_lineup_cache.py), i.e. the exact 5
players who actually played. That is real per-match ground truth, unlike
CS2's per-EVENT roster approximation (cs2_lineups.py).

Two distinct lookups, because they answer different questions:

1. `for_match(source_match_id, team_a, team_b)` -- the historical/training
   path. The scoreboard for a match that has ALREADY been played.

2. `latest_for_team(team)` -- the LIVE path. An upcoming match has no
   scoreboard yet, so a live prediction uses each team's most recently seen
   real lineup instead. Populated by feeding played matches through
   `note_played()` in chronological order during the walk-forward.

Orientation onto team_a/team_b is always done by NAME, never by vlr.gg's row
order -- a silently flipped lineup would corrupt player ratings in both
directions at once. vlr.gg renders sponsored names with the canonical name
in parentheses ("Movistar KOI(KOI)", "JD Mall JDG Esports(JDG Esports)"),
so the parenthetical is tried as a variant; without that, every sponsored
team drops out of the join (measured live: 98.6% -> 99.8% join rate).
"""
from __future__ import annotations

import json
from pathlib import Path

from app.ingestion.market_matcher_valorant import team_names_match

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "data"
LINEUP_CACHE_PATH = DATA_DIR / "valorant_match_lineups_cache.json"

LINEUP_SIZE = 5


def _name_variants(name: str) -> list[str]:
    variants = [name]
    if "(" in name and name.rstrip().endswith(")"):
        inner = name[name.rfind("(") + 1:-1].strip()
        if inner:
            variants.append(inner)
    return variants


def _same_team(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return any(team_names_match(x, y) for x in _name_variants(a) for y in _name_variants(b))


class ValorantLineupResolver:
    def __init__(self, cache: dict | None = None):
        if cache is None:
            cache = json.loads(LINEUP_CACHE_PATH.read_text(encoding="utf-8")) if LINEUP_CACHE_PATH.exists() else {}
        self.cache = {k: v for k, v in cache.items() if v}
        self._latest: dict[str, list[str]] = {}

    def for_match(self, source_match_id, team_a: str, team_b: str):
        """(lineup_a, lineup_b) for an already-played match, or (None, None)
        when there's no scoreboard (forfeit/unplayed) or the names can't be
        confidently oriented."""
        entry = self.cache.get(str(source_match_id))
        if not entry:
            return None, None
        names, lus = entry["teams"], entry["lineups"]
        if _same_team(names[0], team_a) and _same_team(names[1], team_b):
            return lus[0], lus[1]
        if _same_team(names[0], team_b) and _same_team(names[1], team_a):
            return lus[1], lus[0]
        return None, None

    def note_played(self, team_a: str, team_b: str, lineup_a, lineup_b) -> None:
        """Records each side's most recent real lineup. Called in
        chronological order during the walk-forward, so `latest_for_team`
        always reflects the last lineup seen BEFORE the moment being
        predicted -- no lookahead."""
        if lineup_a:
            self._latest[team_a] = list(lineup_a)
        if lineup_b:
            self._latest[team_b] = list(lineup_b)

    def latest_for_team(self, team: str):
        """Most recent real lineup for this team, for predicting an UPCOMING
        match that has no scoreboard yet. Exact name first, then the same
        fuzzy/sponsored-name rule. None if this team has never been seen with
        a real lineup -- callers fall back to the team model rather than
        guessing membership."""
        if not team:
            return None
        found = self._latest.get(team)
        if found:
            return found
        for k, v in self._latest.items():
            if _same_team(k, team):
                return v
        return None

"""Resolves the real CS2 lineup that played a given match.

Two real sources, in priority order:

1. **Event rosters** (data/cs2_event_rosters_cache.json) -- Liquipedia's own
   per-tournament participant cards. Authoritative for that event, but only
   covers teams that appear on a tournament page with a populated roster:
   29.7% of this app's 8,839 real historical matches (measured live
   2026-07-21).

2. **Transfer-based reconstruction** (data/cs2_transfer_history_cache.json --
   14,854 real dated transfer events, each carrying the real player name(s)).
   For a match with no direct event roster, takes that team's NEAREST real
   event-roster anchor and rolls Liquipedia's own transfer log forward (or
   backward) to the match date:
     - rolling FORWARD past a transfer: "in" adds the player, "out" removes.
     - rolling BACKWARD past a transfer: the inverse ("in" means they had NOT
       joined yet, "out" means they were still on the roster).

   Reconstruction is deliberately gated on producing EXACTLY `LINEUP_SIZE`
   players. That gate is the whole point: Liquipedia's transfer log is not
   guaranteed complete, and drift accumulates the further a match sits from
   its anchor, so a reconstruction that doesn't land on a valid 5-man lineup
   is treated as unknown rather than guessed at. A wrong lineup is worse than
   no lineup -- it would feed real player ratings with fabricated membership.

Returns None when neither source yields a usable lineup; callers fall back to
the team-level model (see elo_cs2.py).
"""
from __future__ import annotations

import json
from pathlib import Path

from app.ingestion.market_matcher_cs2 import team_names_match

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "data"
ROSTER_CACHE_PATH = DATA_DIR / "cs2_event_rosters_cache.json"
TRANSFER_CACHE_PATH = DATA_DIR / "cs2_transfer_history_cache.json"

LINEUP_SIZE = 5
# How far from a real anchor a reconstruction is still trusted. Beyond this,
# too many unlogged roster moves can accumulate for the 5-man gate alone to
# catch. 180 days is a deliberately conservative default, flagged as a round
# number rather than a derived constant -- it is NOT grid-searched (there is
# no ground-truth lineup set to search against; the 5-man gate does the real
# validation work).
MAX_ANCHOR_DISTANCE_DAYS = 180


def _load(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


class Cs2LineupResolver:
    def __init__(self, rosters: dict | None = None, transfers: list | None = None,
                 tournament_dates: dict | None = None):
        self.rosters = rosters if rosters is not None else _load(ROSTER_CACHE_PATH, {})
        transfers = transfers if transfers is not None else _load(TRANSFER_CACHE_PATH, [])
        self.tournament_dates = tournament_dates or {}

        # team -> sorted [(date, direction, [players])]
        self.transfers_by_team: dict[str, list] = {}
        for e in transfers:
            if not e.get("players"):
                continue
            self.transfers_by_team.setdefault(e["team"], []).append(
                (e["date"], e["direction"], e["players"])
            )
        for t in self.transfers_by_team:
            self.transfers_by_team[t].sort(key=lambda x: x[0])

        # team -> sorted [(anchor_date, [players])], built from event rosters
        self.anchors_by_team: dict[str, list] = {}
        for slug, teams in self.rosters.items():
            adate = self.tournament_dates.get(slug)
            if not adate:
                continue
            for team, players in teams.items():
                if len(players) == LINEUP_SIZE:
                    self.anchors_by_team.setdefault(team, []).append((adate, list(players)))
        for t in self.anchors_by_team:
            self.anchors_by_team[t].sort(key=lambda x: x[0])

        self._direct_cache: dict = {}
        self._recon_cache: dict = {}

    # --- source 1: direct event roster -------------------------------------
    def _direct(self, slug: str, team: str, display: str | None):
        key = (slug, team)
        if key in self._direct_cache:
            return self._direct_cache[key]
        tr = self.rosters.get(slug) or {}
        found = None
        for cand in (team, display):
            if cand and cand in tr:
                found = tr[cand]
                break
        if found is None:
            for k, v in tr.items():
                if any(c and team_names_match(c, k) for c in (team, display)):
                    found = v
                    break
        self._direct_cache[key] = found
        return found

    # --- source 2: transfer-rolled reconstruction ---------------------------
    def _resolve_key(self, team: str, display: str | None, table: dict):
        for cand in (team, display):
            if cand and cand in table:
                return cand
        for k in table:
            if any(c and team_names_match(c, k) for c in (team, display)):
                return k
        return None

    def _reconstruct(self, team: str, display: str | None, date: str):
        key = (team, date)
        if key in self._recon_cache:
            return self._recon_cache[key]
        result = None
        akey = self._resolve_key(team, display, self.anchors_by_team)
        if akey:
            anchors = self.anchors_by_team[akey]
            best = min(anchors, key=lambda a: abs(_days(a[0], date)))
            if abs(_days(best[0], date)) <= MAX_ANCHOR_DISTANCE_DAYS:
                tkey = self._resolve_key(team, display, self.transfers_by_team)
                moves = self.transfers_by_team.get(tkey, []) if tkey else []
                roster = set(best[1])
                adate = best[0]
                if date >= adate:
                    for d, direction, players in moves:
                        if adate < d <= date:
                            for p in players:
                                roster.add(p) if direction == "in" else roster.discard(p)
                else:
                    for d, direction, players in reversed(moves):
                        if date < d <= adate:
                            for p in players:
                                roster.discard(p) if direction == "in" else roster.add(p)
                if len(roster) == LINEUP_SIZE:
                    result = sorted(roster)
        self._recon_cache[key] = result
        return result

    # --- public -------------------------------------------------------------
    def lineup(self, slug: str, team: str, display: str | None, date: str | None):
        """The real lineup for this team in this match, or None if neither a
        direct event roster nor a validated reconstruction is available."""
        direct = self._direct(slug, team, display)
        if direct:
            return direct
        if not date:
            return None
        return self._reconstruct(team, display, date)


def _days(a: str, b: str) -> int:
    import datetime as dt
    return (dt.date.fromisoformat(a[:10]) - dt.date.fromisoformat(b[:10])).days

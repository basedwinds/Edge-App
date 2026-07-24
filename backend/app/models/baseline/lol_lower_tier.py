"""Reconstructs lower-tier LoL series from gol.gg per-game results, for the
tier-expansion feature (task #32) -- so the ~50 Kalshi-listed teams that
never appear in Leaguepedia's Primary tier become priceable.

gol.gg games are aggregated into series by (date, unordered team pair).
Series already present in the Primary match cache (same date+pair) are
DROPPED -- the Primary cache has authoritative maps_won for those.

Known approximation (same as scripts/test_lol_tier_expansion.py's own note):
two distinct series between the same two teams on one calendar day merge into
one. Rare, and low-impact as Elo training signal.

These rows are consumed by elo_service_lol.py to train a SEPARATE, expanded
rating pool. That pool is used ONLY to price matches the clean Primary-only
pool can't -- so Primary-vs-Primary predictions stay byte-identical to the
pre-expansion model and take zero pollution (see the pollution check in
test_lol_tier_expansion.py: the naive single-pool merge cost +0.00039 Brier
on Primary matches; the two-pool design avoids even that)."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "data"
LINEUP_CACHE_PATH = DATA_DIR / "lol_game_lineups_cache.json"


def _norm(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", x.lower()) if x else ""


def build_lower_tier_matches(exclude_pairs: set) -> list[dict]:
    """gol.gg games -> match dicts (same shape elo_service_lol.py's own
    loaders produce), EXCLUDING any (date, normalized-pair) in
    `exclude_pairs` (the Primary + live matches already trained on).

    Each row carries a synthetic 'golgg:'-prefixed source_match_id so it
    dedupes cleanly and never collides with a real Leaguepedia/live id."""
    if not LINEUP_CACHE_PATH.exists():
        return []
    games = [v for v in json.loads(LINEUP_CACHE_PATH.read_text(encoding="utf-8")).values() if v]
    agg: dict = defaultdict(lambda: {"a": 0, "b": 0, "names": None})
    for g in games:
        t0, t1 = g["teams"]
        pair = tuple(sorted((_norm(t0), _norm(t1))))
        key = (g["date"],) + pair
        s = agg[key]
        a_is_t0 = _norm(t0) == pair[0]
        if s["names"] is None:
            s["names"] = (t0, t1) if a_is_t0 else (t1, t0)
        a_won = g["blue_won"] if a_is_t0 else (not g["blue_won"])
        s["a"] += 1 if a_won else 0
        s["b"] += 0 if a_won else 1

    rows = []
    for (date, na, nb), s in agg.items():
        if (date, na, nb) in exclude_pairs:
            continue
        aw, bw = s["a"], s["b"]
        if aw == bw:
            continue  # can't call a winner
        total = aw + bw
        best_of = 1 if total == 1 else 2 * max(aw, bw) - 1
        rows.append({
            "source_match_id": f"golgg:{date}:{na}:{nb}",
            "team_a": s["names"][0], "team_b": s["names"][1],
            "best_of": best_of, "winner": "team_a" if aw > bw else "team_b",
            "maps_won_a": aw, "maps_won_b": bw,
            "match_date": date,
            "sort_key": date,
        })
    return rows


def pair_key(match_date: str, team_a: str, team_b: str):
    return (match_date,) + tuple(sorted((_norm(team_a), _norm(team_b))))

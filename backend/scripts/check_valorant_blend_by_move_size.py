"""Do the Valorant h2h / player blends still help when they move the price A LOT?

WHY THIS EXISTS, and why the existing tests do not answer it. A user challenged a
recommended bet on Team Secret vs DetonatioN FocusMe: team Elo had Secret 114
points WORSE and 27% to win the series, the market said 35.5%, and the model said
51.4% -- so the whole +15.9pp edge, and three real stakes, came from two blends
moving the number +24pp.

Re-running the shipped validations confirms both blends are real and are at their
grid optimum (h2h prior_weight 10: Brier 0.22506 -> 0.22430; player w=0.4:
0.23064 -> 0.22742). But those are AVERAGE improvements of ~0.3% relative, spread
across every match. An average that small is entirely compatible with the blends
being helpful on the many matches they nudge by 1-2pp and harmful on the few they
swing by 20pp+ -- and it is precisely the 20pp+ swings that generate edges big
enough to clear the 10pp staking gate. So the aggregate number, which is what the
existing scripts report, cannot decide whether a bet like Team Secret's is
trustworthy.

This bins predictions by HOW FAR the blend moved them from the pure team-Elo
number, and compares Brier of blended vs Elo-only WITHIN each bin. If the blends
are worth staking on, they should keep their edge in the large-move bins. If they
only work on small nudges, the app should stop letting them override a large Elo
gap into a bet.

Mirrors elo_valorant.py's shipped rules exactly (per-map updates off the real
maps_won_a/maps_won_b split, K as shipped) and the production blend arithmetic
from elo_service_valorant._blend_h2h / _blend_player, so the baseline is the real
model rather than a strawman.

Run:  PYTHONPATH=. ./.venv/Scripts/python.exe scripts/check_valorant_blend_by_move_size.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingestion.valorant_data import infer_best_of_from_score  # noqa: E402
from app.models.baseline.elo_valorant import (  # noqa: E402
    BASE_RATING, K as SHIPPED_K, PLAYER_BLEND_WEIGHT, map_win_prob, series_score_distribution,
)
from app.models.calibration import brier_score  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "valorant_historical_match_cache.json"
LINEUP_CACHE_PATH = DATA_DIR / "valorant_match_lineups_cache.json"
WARMUP = 500
H2H_PRIOR_WEIGHT = 10.0   # as shipped, elo_service_valorant
K_PLAYER = 40             # grid optimum from test_valorant_player_level_signal

# Move-size bins in probability points. The last two are the ones that matter:
# a 10pp+ swing is what turns a losing proposition into one that clears the
# staking gate.
BINS = ((0.0, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 1.01))


def load_matches() -> list[dict]:
    rows = json.loads(MATCH_CACHE_PATH.read_text(encoding="utf-8"))
    rows = [r for r in rows if r["match_date"] >= "2020-01-01"]
    for r in rows:
        if not r.get("best_of"):
            r["best_of"] = infer_best_of_from_score(r.get("maps_won_a"), r.get("maps_won_b"))
    rows = [r for r in rows if r.get("best_of") and r.get("winner")]
    rows.sort(key=lambda r: r.get("estimated_start_time") or r["match_date"])
    return rows


def _canon(name: str) -> str:
    """vlr.gg renders SPONSORED names with the canonical one in parentheses
    ("JD Mall JDG Esports(JDG Esports)"), while the match cache stores the
    canonical form -- so the parenthetical has to be preferred or every
    sponsored team silently drops out of the join. Same handling as
    test_valorant_player_level_signal._name_variants."""
    n = (name or "").strip()
    if "(" in n and n.endswith(")"):
        n = n[n.rfind("(") + 1:-1].strip()
    return n.casefold()


def load_lineups() -> dict[str, dict]:
    """{vlr match id: {"teams": [...], "lineups": [[...], [...]]}}.

    Keyed by the same id the match cache uses as source_match_id -- verified:
    all 8,547 lineup entries join. The per-side assignment is resolved by TEAM
    NAME rather than by row order, because the lineup crawl's team order is not
    guaranteed to match the match cache's team_a/team_b.
    """
    if not LINEUP_CACHE_PATH.exists():
        return {}
    return json.loads(LINEUP_CACHE_PATH.read_text(encoding="utf-8"))


def lineups_for(entry: dict | None, team_a: str, team_b: str) -> tuple[list[str], list[str]] | None:
    if not entry:
        return None
    teams, lus = entry.get("teams") or [], entry.get("lineups") or []
    if len(teams) != 2 or len(lus) != 2:
        return None
    ca, cb = _canon(team_a), _canon(team_b)
    t0, t1 = _canon(teams[0]), _canon(teams[1])
    if ca == t0 and cb == t1:
        return lus[0], lus[1]
    if ca == t1 and cb == t0:
        return lus[1], lus[0]
    return None  # names don't line up -> don't guess which side is which


def series_prob(a_r: float, b_r: float, best_of: int) -> float:
    dist = series_score_distribution(map_win_prob(a_r, b_r), best_of)
    return sum(p for (a, b), p in dist.items() if a > b)


def run() -> None:
    matches = load_matches()
    lineups = load_lineups()
    print(f"{len(matches)} matches | {len(lineups)} lineup entries loaded")

    ratings: dict[str, float] = {}
    players: dict[str, float] = {}
    h2h: dict[tuple, tuple[int, int]] = {}
    rows: list[tuple[float, float, float]] = []  # (p_elo, p_blend, outcome)

    for m in matches:
        ta, tb, bo, winner = m["team_a"], m["team_b"], m["best_of"], m["winner"]
        a_r, b_r = ratings.get(ta, BASE_RATING), ratings.get(tb, BASE_RATING)
        p_elo = series_prob(a_r, b_r, bo)
        p = p_elo

        key = tuple(sorted((ta, tb)))
        wins_first, total = h2h.get(key, (0, 0))
        if total >= 1:
            wins_a = wins_first if ta == key[0] else (total - wins_first)
            p = (p * H2H_PRIOR_WEIGHT + wins_a) / (H2H_PRIOR_WEIGHT + total)

        mid = str(m.get("source_match_id") or "")
        pair = lineups_for(lineups.get(mid), ta, tb)
        la, lb = pair if pair else (None, None)
        if la and lb:
            sa = sum(players.get(x, BASE_RATING) for x in la) / len(la)
            sb = sum(players.get(x, BASE_RATING) for x in lb) / len(lb)
            p_player = series_prob(sa, sb, bo)
            p = (1.0 - PLAYER_BLEND_WEIGHT) * p + PLAYER_BLEND_WEIGHT * p_player

        y = 1.0 if winner == "team_a" else 0.0
        rows.append((p_elo, p, y))

        # ---- updates, mirroring the shipped rules --------------------------
        maps_a, maps_b = m.get("maps_won_a"), m.get("maps_won_b")
        pairs = []
        if maps_a is not None and maps_b is not None:
            pairs = [1.0] * int(maps_a) + [0.0] * int(maps_b)
        else:
            pairs = [y]
        for actual in pairs:
            ar, br = ratings.get(ta, BASE_RATING), ratings.get(tb, BASE_RATING)
            d = SHIPPED_K * (actual - map_win_prob(ar, br))
            ratings[ta], ratings[tb] = ar + d, br - d
        if la and lb:
            sa = sum(players.get(x, BASE_RATING) for x in la) / len(la)
            sb = sum(players.get(x, BASE_RATING) for x in lb) / len(lb)
            d = K_PLAYER * (y - map_win_prob(sa, sb))
            for x in la:
                players[x] = players.get(x, BASE_RATING) + d
            for x in lb:
                players[x] = players.get(x, BASE_RATING) - d
        h2h[key] = ((wins_first + (1 if (winner == "team_a") == (ta == key[0]) else 0)), total + 1)

    rows = rows[WARMUP:]
    print(f"\nevaluated {len(rows)} predictions (post-warmup)\n")
    print(f"{'move size':>14}{'n':>8}{'Brier elo':>12}{'Brier blend':>13}{'delta':>10}   verdict")
    for lo, hi in BINS:
        sub = [r for r in rows if lo <= abs(r[1] - r[0]) < hi]
        if len(sub) < 30:
            continue
        be = brier_score([r[0] for r in sub], [r[2] for r in sub])
        bb = brier_score([r[1] for r in sub], [r[2] for r in sub])
        d = bb - be
        # Bootstrap the DELTA: the big-move bins are the decision-relevant ones
        # and also the smallest, so an eyeballed gap is not enough.
        import random as _r
        rng = _r.Random(20260807); deltas = []
        for _ in range(2000):
            samp = [sub[rng.randrange(len(sub))] for _ in range(len(sub))]
            deltas.append(brier_score([x[1] for x in samp], [x[2] for x in samp])
                          - brier_score([x[0] for x in samp], [x[2] for x in samp]))
        deltas.sort(); lo_ci, hi_ci = deltas[50], deltas[1949]
        verdict = "blend HELPS" if hi_ci < 0 else ("blend HURTS" if lo_ci > 0 else "inconclusive")
        print(f"{f'{lo:.0%}-{hi:.0%}':>14}{len(sub):>8}{be:>12.5f}{bb:>13.5f}{d:>+10.5f}  [{lo_ci:+.5f},{hi_ci:+.5f}]  {verdict}")

    be = brier_score([r[0] for r in rows], [r[2] for r in rows])
    bb = brier_score([r[1] for r in rows], [r[2] for r in rows])
    print(f"\n{'ALL':>14}{len(rows):>8}{be:>12.5f}{bb:>13.5f}{bb - be:>+10.5f}")


if __name__ == "__main__":
    run()

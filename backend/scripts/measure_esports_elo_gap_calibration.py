"""Does the CS2 Elo-gap overconfidence (#196) also exist in LoL and Valorant?

NOT ASSUMED EITHER WAY. CS2 shipped GAP_SHRINK=0.80 after measuring a
significant miss at every gap above 50 Elo. Whether that transfers is an
empirical question per title, and this codebase has repeatedly found that esports
results do NOT transfer between titles: per-map Elo IMPROVED LoL and Valorant and
was REJECTED for CS2 on its own data; the patch adjustment was null for LoL;
idle-decay was null for CS2/Valorant but real for LoL. Copying 0.80 across would
be the mistake those findings warn about.

WHY LoL/VALORANT ARE STRUCTURALLY DIFFERENT HERE. Both KEPT per-map Elo updates
(a Bo3 won 2-1 is three Bernoulli observations, not one). More updates per match
means a different rating spread, so even an identical defect would need a
different lambda. `predict_series` itself is the same shape in all three -- the
per-map difference lives in the UPDATE.

PRODUCTION PRIMITIVES, NOT A REPLICA. The update runs through each title's own
`predict_and_update`, and the prediction uses production `map_win_prob` +
`series_score_distribution`. The only line that is mine is the lambda multiply. A
plain-Elo replica already produced a WRONG conclusion once in this project (the
#193 retraction), so this asserts its own fidelity instead of hoping:

    SELF-CHECK: at lambda=1.0 the locally-computed series probability must equal
    the distribution `predict_and_update` returns, to 1e-9, on every match. Any
    mismatch means the harness has drifted from production and the run ABORTS
    rather than reporting a number nobody can trust.

SCOPE. This measures the BASE Elo layer, which is where the CS2 fix was applied
(inside predict_series, beneath the service's h2h/rest/player blends). That is
the right layer for the DESCRIPTIVE question -- if there is no gap gradient in the
base, there is nothing for a shrink to fix and the blends cannot create one. If a
gradient IS found, the fit must then be re-checked with the service composition
before shipping, because the blends move the final number.

Run: backend/.venv/Scripts/python.exe scripts/measure_esports_elo_gap_calibration.py
"""
from __future__ import annotations

import json
import sys
from math import sqrt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATA = Path(__file__).resolve().parents[2] / "data"

BUCKETS = [(0, 49), (50, 99), (100, 149), (150, 199), (200, 299), (300, 10**9)]
MIN_BUCKET = 25


def wilson(k: int, n: int) -> tuple[float, float]:
    if not n:
        return (0.0, 1.0)
    p, z = k / n, 1.96
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def bucket_for(gap: float):
    for lo, hi in BUCKETS:
        if lo <= gap <= hi:
            return (lo, hi)
    return None


def label(b) -> str:
    lo, hi = b
    return f"{lo}+" if hi >= 10**9 else f"{lo}-{hi}"


def infer_best_of(row) -> int | None:
    """Valorant's cache carries no best_of. The match FORMAT is public before a
    match is played, so deriving it from the final score is not lookahead about
    the RESULT -- it recovers a fact that was known in advance. max maps won:
    1 -> Bo1, 2 -> Bo3, 3 -> Bo5."""
    a, b = row.get("maps_won_a"), row.get("maps_won_b")
    if a is None or b is None:
        return None
    m = max(a, b)
    return {1: 1, 2: 3, 3: 5}.get(m)


def load(title: str):
    rows = json.loads((DATA / f"{title}_historical_match_cache.json").read_text(encoding="utf-8"))
    out = []
    for r in rows:
        if r.get("winner") not in ("team_a", "team_b"):
            continue
        bo = r.get("best_of") or infer_best_of(r)
        if not bo:
            continue
        r = dict(r)
        r["best_of"] = bo
        out.append(r)
    out.sort(key=lambda r: r.get("estimated_start_time") or r.get("match_date") or "")
    return out


def series_prob_a(dist) -> float:
    """P(team_a wins the series) from a SeriesDistribution."""
    return sum(p for (a, b), p in dist.dist.items() if a > b)


def run(title: str, lam: float = 1.0, verify: bool = True):
    if title == "lol":
        from app.models.baseline.elo_lol import (
            BASE_RATING, LolEloState, map_win_prob, predict_and_update,
            series_score_distribution,
        )
        from app.models.baseline.elo_service_lol import MIN_GAMES
        State = LolEloState
    elif title == "valorant":
        from app.models.baseline.elo_valorant import (
            BASE_RATING, ValorantEloState, map_win_prob, predict_and_update,
            series_score_distribution,
        )
        from app.models.baseline.elo_service_valorant import MIN_GAMES
        State = ValorantEloState
    else:
        raise SystemExit(f"unknown title {title}")

    rows = load(title)
    state = State()
    games: dict[str, int] = {}
    out = []
    checked = 0

    for m in rows:
        a, b = m["team_a"], m["team_b"]
        ra, rb = state.get(a), state.get(b)
        ga, gb = games.get(a, 0), games.get(b, 0)
        gap_signed = ra - rb

        map_p = map_win_prob(gap_signed * lam, 0.0)
        local = series_score_distribution(map_p, m["best_of"])
        p_a_local = sum(p for (x, y), p in local.items() if x > y)

        prod = predict_and_update(state, m)   # production predict + production update
        if prod is None:
            continue
        if verify and lam == 1.0:
            # THE FIDELITY ASSERTION. If this ever fires the harness has drifted
            # from production and every number below would be fiction.
            p_a_prod = series_prob_a(prod)
            if abs(p_a_local - p_a_prod) > 1e-9:
                raise SystemExit(
                    f"*** {title}: harness diverges from production at lam=1 "
                    f"({p_a_local:.12f} vs {p_a_prod:.12f}) -- ABORT")
            checked += 1

        if min(ga, gb) >= MIN_GAMES:
            won_a = 1.0 if m["winner"] == "team_a" else 0.0
            if p_a_local >= 0.5:
                out.append((abs(gap_signed), p_a_local, won_a, m.get("match_date") or ""))
            else:
                out.append((abs(gap_signed), 1.0 - p_a_local, 1.0 - won_a, m.get("match_date") or ""))
        games[a] = ga + 1
        games[b] = gb + 1

    return out, checked, len(rows), MIN_GAMES


def main() -> None:
    for title in ("lol", "valorant"):
        rows, checked, total, min_games = run(title, 1.0)
        print(f"\n{'='*78}")
        print(f"{title.upper()}  --  {total} settled matches replayed, "
              f"{len(rows)} gated at MIN_GAMES={min_games}")
        print(f"harness verified identical to production on {checked} matches at lam=1.0")
        print(f"{'='*78}")
        print(f"{'gap':>9s} {'n':>7s} {'claimed':>9s} {'actual':>9s} {'miss':>8s} "
              f"{'95% CI':>17s}  sig")
        any_sig = False
        for bk in BUCKETS:
            r = [(p, o) for g, p, o, _ in rows if bucket_for(g) == bk]
            if len(r) < MIN_BUCKET:
                continue
            claimed = sum(p for p, _ in r) / len(r)
            wins = int(sum(o for _, o in r))
            actual = wins / len(r)
            lo, hi = wilson(wins, len(r))
            sig = not (lo <= claimed <= hi)
            any_sig = any_sig or sig
            print(f"{label(bk):>9s} {len(r):7d} {claimed:9.4f} {actual:9.4f} "
                  f"{actual-claimed:+8.4f} [{lo:.3f},{hi:.3f}]  {'YES' if sig else '-'}")
        wide = sum(1 for r in rows if r[0] >= 200)
        print(f"\nshare of gated predictions at gap >= 200: {wide}/{len(rows)} "
              f"({100*wide/max(len(rows),1):.1f}%)")
        print(f"VERDICT: {'a significant gradient exists -- proceed to a per-title fit' if any_sig else 'NO significant bucket -- nothing for a shrink to fix'}")


if __name__ == "__main__":
    main()

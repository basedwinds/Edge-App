"""Do REST or SERIES LENGTH predict a Call of Duty result beyond Elo?

WHY ONLY THESE TWO. elo_service_cod is deliberately thin -- no player blend, no
transfer-aware K, no map-pool ratings -- because breakingpoint.gg publishes no
lineups, no transfers and no map data to build them from. Adding those would be
inventing structure the source does not contain.

But two things the source DOES carry have never been tested:

  * REST -- every row has a real UTC datetime, so days-since-last-match is
    computable for both teams. CS2, LoL and Valorant all carry a validated rest
    bonus; nobody has checked whether CoD does.
  * SERIES LENGTH -- best_of is stated (5 for CDL, 7 for the Esports World
    Cup). A longer series should favour the stronger team more, because there
    is more chance for skill to express. If the Elo->series mapping already
    handles that correctly, best_of should carry NO residual signal. If it
    does carry signal, the race-to-k conversion is mis-specified.

THE TEST, and it is a residual test on purpose. Not "does the favourite win
more when rested" -- of course a favourite wins more, and that would just
re-measure Elo. Instead: take the model's own pre-match probability, compute
the RESIDUAL (actual - predicted), and ask whether the residual correlates with
the factor. A factor the model already accounts for leaves no residual.

Walk-forward throughout: ratings are updated only after each match is scored,
so no match informs its own prediction.

A NEGATIVE RESULT IS A REAL RESULT. Rest was REJECTED for several sports in
this app already. Finding nothing here means the thin model is thin because the
data is thin, not because it was left unfinished.
"""
from __future__ import annotations

import collections
import datetime
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.baseline.elo_cod import (  # noqa: E402
    CodEloState, map_win_prob, predict_and_update, series_p_from_map_p,
)

CACHE = Path(__file__).resolve().parents[2] / "data" / "cod_historical_match_cache.json"

# Both teams need this much history before a match is scored, matching
# elo_service_cod.MIN_GAMES -- otherwise warm-up noise dominates the residual.
MIN_GAMES = 3
REST_CAP_DAYS = 14  # beyond this it is an off-season, not "rest"


def _dt(row) -> datetime.datetime | None:
    raw = row.get("datetime") or row.get("match_date")
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def main() -> None:
    if not CACHE.exists():
        print(f"no cache at {CACHE}")
        return
    rows = json.loads(CACHE.read_text(encoding="utf-8"))
    rows = [r for r in rows if r.get("team_a") and r.get("team_b") and r.get("winner")]
    rows.sort(key=lambda r: (r.get("datetime") or r.get("match_date") or "", r["source_match_id"]))
    print(f"{len(rows)} decided matches\n")

    state = CodEloState()
    last_seen: dict[str, datetime.datetime] = {}
    samples: list[tuple[float, float, int]] = []  # (rest_diff, best_of, residual)

    for r in rows:
        a, b = r["team_a"], r["team_b"]
        winner_name = r.get("winner")
        winner = "team_a" if winner_name == a else "team_b" if winner_name == b else None
        if winner is None:
            continue
        best_of = r.get("best_of") or 5
        when = _dt(r)

        scoreable = (state.games_played(a) >= MIN_GAMES and state.games_played(b) >= MIN_GAMES
                     and when is not None and a in last_seen and b in last_seen)
        if scoreable:
            p = series_p_from_map_p(map_win_prob(state.get(a), state.get(b)), best_of)
            actual = 1.0 if winner == "team_a" else 0.0
            rest_a = min((when - last_seen[a]).days, REST_CAP_DAYS)
            rest_b = min((when - last_seen[b]).days, REST_CAP_DAYS)
            samples.append((float(rest_a - rest_b), float(best_of), actual - p))

        predict_and_update(state, {"team_a": a, "team_b": b,
                                   "best_of": best_of, "winner": winner})
        if when is not None:
            last_seen[a] = when
            last_seen[b] = when

    if len(samples) < 200:
        print(f"only {len(samples)} scorable matches -- too few to conclude")
        return
    print(f"{len(samples)} matches scored with both teams past warm-up\n")

    def corr(xs, ys):
        n = len(xs)
        mx, my = statistics.mean(xs), statistics.mean(ys)
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        dy = math.sqrt(sum((y - my) ** 2 for y in ys))
        return num / (dx * dy) if dx and dy else 0.0

    resid = [s[2] for s in samples]
    print(f"mean residual {statistics.mean(resid):+.4f} "
          f"(near zero = the model is unbiased overall)\n")

    for idx, label in ((0, "rest difference (days, a - b)"), (1, "best_of")):
        xs = [s[idx] for s in samples]
        r = corr(xs, resid)
        # Standard error of a correlation under the null.
        se = 1.0 / math.sqrt(len(xs) - 3) if len(xs) > 3 else 1.0
        z = 0.5 * math.log((1 + r) / (1 - r)) / se if abs(r) < 1 else 0.0
        verdict = "SIGNAL" if abs(z) > 2.0 else "no signal"
        print(f"{label:32s} corr {r:+.4f}   z {z:+.2f}   {verdict}")

    # Bucketed view -- a correlation can hide a non-monotonic effect.
    print("\nresidual by rest-difference bucket:")
    buckets: dict[str, list[float]] = collections.defaultdict(list)
    for rd, _bo, res in samples:
        if rd <= -3: key = "a much less rested (<=-3d)"
        elif rd < 0: key = "a slightly less (-2..-1)"
        elif rd == 0: key = "equal rest"
        elif rd <= 2: key = "a slightly more (+1..+2)"
        else: key = "a much more rested (>=+3d)"
        buckets[key].append(res)
    for key in ("a much less rested (<=-3d)", "a slightly less (-2..-1)", "equal rest",
                "a slightly more (+1..+2)", "a much more rested (>=+3d)"):
        vals = buckets.get(key, [])
        if vals:
            print(f"   {key:28s} n={len(vals):5d}  mean residual {statistics.mean(vals):+.4f}")

    print("\nresidual by best_of:")
    by_bo: dict[float, list[float]] = collections.defaultdict(list)
    for _rd, bo, res in samples:
        by_bo[bo].append(res)
    for bo in sorted(by_bo):
        vals = by_bo[bo]
        print(f"   Bo{int(bo)}  n={len(vals):5d}  mean residual {statistics.mean(vals):+.4f}")


if __name__ == "__main__":
    main()

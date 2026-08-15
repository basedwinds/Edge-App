"""Does starting-pitcher quality move MLB game totals, and by how many RUNS?

THE GAP (#199, residual from #194). `game_lines_mlb.prob_over` takes exactly
(line, home_team, temp_f, out_wind_mph). That is a league-average run
distribution adjusted for PARK and WEATHER -- no starting pitcher, no bullpen, no
team offence, and it does not even take the away team. So when the market prices
a low total because two aces are starting, the model reads that line as below
league average and likes the OVER. Measured live: over claimed 0.605, delivered
0.519.

The MONEYLINE path already uses pitchers (elo_service_mlb.get_elo_diff takes both
probable-pitcher ids). Totals simply never consumed data the app already has.

NO LOOKAHEAD, BY CONSTRUCTION. Quality is the pitcher's PRIOR-SEASON line, which
is fully known before the season being predicted even starts -- there is no
as-of-date bookkeeping to get subtly wrong, and no way for a game's own result to
leak into its own predictor. It is a weaker signal than current-season-to-date
form would be, and that is the deliberate trade: a clean measurement of a smaller
effect beats a contaminated measurement of a larger one.

METRIC: K-BB% = (SO - BB) / battersFaced. Chosen over ERA/FIP per
check_mlb_pitcher_metric (see project_mlb_pitcher_metric): it is the most
predictive of the three. That work also found the honest effect size is r=0.089 --
the 0.305 figure quoted early on was the REDUNDANCY correlation with elo_diff,
not signal. So a small slope here is the expected result, not a disappointment.

DESCRIPTIVE BEFORE PRESCRIPTIVE. Step 1 fits the slope in RUNS on 2023-2025 and
reports it with a bootstrap CI. If that interval spans zero there is nothing to
ship and step 2 is skipped -- a term that cannot be shown to move runs must not be
allowed to move prices.

Step 2 validates on 2026, never used to fit, and only at the lines that carry
volume. The negative binomial from #194 is retained; a pitcher term shifts the
MEAN, dispersion is unchanged.

Run: backend/.venv/Scripts/python.exe scripts/fit_mlb_total_pitcher_term.py
"""
from __future__ import annotations

import json
import math
import random
import statistics
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.game_lines_mlb import (  # noqa: E402
    PARK_FACTOR, TOTAL_NB_DISPERSION, _nb_sf, _norm_cdf, TOTAL_STD,
)

TRAIN = (2023, 2024, 2025)
TEST = 2026
MIN_BF = 300          # prior-season batters faced before a pitcher's rate is usable
LINES = [6.5, 7.5, 8.5, 9.5, 10.5, 11.5]
SEED = 20260815


def _get(url: str):
    return json.load(urllib.request.urlopen(url, timeout=180))


def pitcher_kbb(season: int) -> dict[int, float]:
    """{player_id: K-BB%} for a whole season, one request."""
    d = _get("https://statsapi.mlb.com/api/v1/stats?stats=season&group=pitching"
             f"&season={season}&sportIds=1&gameType=R&playerPool=All&limit=2000")
    out = {}
    for s in (d.get("stats") or [{}])[0].get("splits") or []:
        st = s.get("stat") or {}
        pid = (s.get("player") or {}).get("id")
        bf = st.get("battersFaced")
        so, bb = st.get("strikeOuts"), st.get("baseOnBalls")
        if not pid or not bf or bf < MIN_BF or so is None or bb is None:
            continue
        out[pid] = (so - bb) / bf
    return out


def games(season: int):
    """Finals with both probable pitchers and a score, one request."""
    d = _get(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&season={season}"
             f"&startDate={season}-03-01&endDate={season}-11-15&gameType=R"
             f"&hydrate=probablePitcher")
    out = []
    for day in d.get("dates", []):
        for g in day.get("games", []):
            if (g.get("status") or {}).get("detailedState") != "Final":
                continue
            t = g.get("teams") or {}
            h, a = t.get("home") or {}, t.get("away") or {}
            hs, as_ = h.get("score"), a.get("score")
            if hs is None or as_ is None:
                continue
            hp = (h.get("probablePitcher") or {}).get("id")
            ap = (a.get("probablePitcher") or {}).get("id")
            abbr = ((h.get("team") or {}).get("abbreviation")
                    or (h.get("team") or {}).get("teamName"))
            out.append({"total": hs + as_, "hp": hp, "ap": ap, "home": abbr})
    return out


def build(season: int, kbb_prior: dict[int, float]):
    """(combined_kbb, actual_total, home_abbr) for games where BOTH starters have
    a usable prior season. Games missing one are dropped, not defaulted -- a
    default would quietly pull the fit toward league average."""
    rows = []
    for g in games(season):
        a, b = kbb_prior.get(g["hp"]), kbb_prior.get(g["ap"])
        if a is None or b is None:
            continue
        rows.append(((a + b) / 2.0, g["total"], g["home"]))
    return rows


def ols(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sxy / sxx
    return slope, my - slope * mx, mx


def boot_slope(xs, ys, n=3000):
    rnd = random.Random(SEED)
    k = len(xs)
    out = []
    for _ in range(n):
        idx = [rnd.randrange(k) for _ in range(k)]
        out.append(ols([xs[i] for i in idx], [ys[i] for i in idx])[0])
    out.sort()
    return out[int(0.025 * n)], out[int(0.975 * n)]


def main() -> None:
    print("Pulling prior-season pitcher rates (no lookahead: season Y uses Y-1)...")
    prior = {y: pitcher_kbb(y - 1) for y in TRAIN + (TEST,)}
    for y, m in prior.items():
        print(f"  {y} uses {y-1}: {len(m)} pitchers with >= {MIN_BF} BF")

    train_rows = []
    for y in TRAIN:
        r = build(y, prior[y])
        train_rows += r
        print(f"  train {y}: {len(r)} games with both starters rated")
    test_rows = build(TEST, prior[TEST])
    print(f"  test  {TEST}: {len(test_rows)} games (never used to fit)")

    xs = [r[0] for r in train_rows]
    ys = [float(r[1]) for r in train_rows]
    slope, intercept, mean_x = ols(xs, ys)
    lo, hi = boot_slope(xs, ys)
    sd_x = statistics.pstdev(xs)

    print(f"\n{'='*76}\nSTEP 1 -- DESCRIPTIVE: do starters move RUNS?\n{'='*76}")
    print(f"  combined K-BB%  mean {mean_x:.4f}  sd {sd_x:.4f}")
    print(f"  slope: {slope:+.3f} runs per 1.00 of combined K-BB%")
    print(f"  95% CI [{lo:+.3f}, {hi:+.3f}]  (bootstrap, n={len(xs)})")
    per_sd = slope * sd_x
    print(f"  => a 1-sd better pitching matchup is worth {per_sd:+.3f} runs")
    if lo <= 0.0 <= hi:
        print("\n  CI SPANS ZERO -- no measurable effect. Nothing to ship; stopping here")
        print("  rather than letting a term that cannot be shown to move runs move prices.")
        return
    if slope > 0:
        print("\n  SLOPE IS POSITIVE -- better pitchers would mean MORE runs, which is")
        print("  backwards. That is a data or metric bug, not a finding. Stopping.")
        return
    print("  Sign is NEGATIVE as expected: better pitching -> fewer runs.")

    # ---------------- STEP 2: does it improve held-out P(over)? ----------------
    print(f"\n{'='*76}\nSTEP 2 -- HELD-OUT {TEST}: does it price better?\n{'='*76}")
    base_mu = sum(float(r[1]) for r in train_rows) / len(train_rows)
    print(f"  baseline mean (train) {base_mu:.4f}")
    print("  Both arms use the SAME negative binomial and the SAME baseline mean;")
    print("  the only difference is whether the pitcher term shifts it.\n")

    n = len(test_rows)
    print(f"{'line':>7}{'actual':>10}{'no-pitcher':>12}{'err':>9}{'+pitcher':>11}{'err':>9}  winner")
    print("-" * 70)
    e_flat = e_pit = 0.0
    wins = {"flat": 0, "pitcher": 0}
    for line in LINES:
        act = sum(1 for r in test_rows if r[1] > line) / n
        # arm A: park only (what ships today)
        pa = sum(_nb_sf(line, base_mu + PARK_FACTOR.get(r[2], 0.0), TOTAL_NB_DISPERSION)
                 for r in test_rows) / n
        # arm B: park + pitcher shift
        pb = sum(_nb_sf(line,
                        base_mu + PARK_FACTOR.get(r[2], 0.0) + slope * (r[0] - mean_x),
                        TOTAL_NB_DISPERSION)
                 for r in test_rows) / n
        ea, eb = abs(pa - act), abs(pb - act)
        e_flat += ea
        e_pit += eb
        w = "pitcher" if eb < ea else "flat"
        wins[w] += 1
        print(f"{line:>7.1f}{act:>10.3f}{pa:>12.3f}{pa-act:>+9.3f}{pb:>11.3f}{pb-act:>+9.3f}  {w}")
    print("-" * 70)
    print(f"{'mean |err|':>7}{'':>10}{e_flat/len(LINES):>12.4f}{'':>9}{e_pit/len(LINES):>11.4f}")
    print(f"lines won: pitcher {wins['pitcher']} / flat {wins['flat']}")

    # The aggregate above hides whether it helps the GAMES it should. Split the
    # test season by matchup quality: the term must help where it actually fires.
    print(f"\nBY MATCHUP QUALITY (test season) -- the term must help where it FIRES")
    srt = sorted(test_rows, key=lambda r: r[0])
    third = len(srt) // 3
    for lbl, grp in (("worst pitching", srt[:third]),
                     ("middle", srt[third:2 * third]),
                     ("best pitching", srt[2 * third:])):
        act_mu = sum(float(r[1]) for r in grp) / len(grp)
        pred_flat = base_mu + sum(PARK_FACTOR.get(r[2], 0.0) for r in grp) / len(grp)
        pred_pit = pred_flat + slope * (sum(r[0] for r in grp) / len(grp) - mean_x)
        print(f"  {lbl:16} n={len(grp):5}  actual mean {act_mu:.3f}   "
              f"flat {pred_flat:.3f} ({pred_flat-act_mu:+.3f})   "
              f"+pitcher {pred_pit:.3f} ({pred_pit-act_mu:+.3f})")

    print()
    if wins["pitcher"] == len(LINES) and e_pit < e_flat:
        print(f"  SHIP: better at EVERY volume line out of sample.")
        print(f"  PITCHER_KBB_SLOPE = {slope:.4f}   LEAGUE_AVG_COMBINED_KBB = {mean_x:.4f}")
    elif e_pit < e_flat:
        print(f"  MIXED: better on average but loses {wins['flat']} line(s). Check which.")
    else:
        print(f"  DO NOT SHIP: no held-out improvement in PRICING, even if the runs")
        print(f"  slope is real. A real effect too small to move prices is not a fix.")


if __name__ == "__main__":
    main()


def cv_shrink() -> None:
    """Pick a shrink on the fitted slope using WITHIN-TRAIN cross-validation.

    The full-train slope OVERSHOOTS on 2026: actual spread across pitching
    terciles is 0.325 runs, the term predicts 0.642. Directionally right, about
    twice too strong -- the test-season slope is roughly half the train slope,
    just inside the train CI, so noise rather than contradiction.

    The fix is shrinkage, but choosing the factor by looking at 2026 would be
    selecting on the season reserved to judge the choice. So: fit on 2023-2024,
    choose the shrink that best predicts 2025's tercile means, and only then
    spend the test season once. 2026 is not touched here.
    """
    prior = {y: pitcher_kbb(y - 1) for y in (2023, 2024, 2025)}
    tr = build(2023, prior[2023]) + build(2024, prior[2024])
    va = build(2025, prior[2025])
    xs = [r[0] for r in tr]; ys = [float(r[1]) for r in tr]
    slope, _, mean_x = ols(xs, ys)
    base_mu = sum(ys) / len(ys)
    print(f"\n{'='*76}\nWITHIN-TRAIN CV: fit 2023-24 (n={len(tr)}), validate 2025 (n={len(va)})")
    print(f"{'='*76}")
    print(f"  2023-24 slope {slope:+.3f}   (full-train was -8.566)")
    srt = sorted(va, key=lambda r: r[0])
    third = len(srt) // 3
    groups = [srt[:third], srt[third:2*third], srt[2*third:]]
    print(f"\n{'shrink':>8}{'eff slope':>12}{'mean |tercile err| on 2025':>30}")
    best, best_e = None, None
    for sh in [round(0.1*i, 1) for i in range(0, 13)]:
        tot = 0.0
        for g in groups:
            act = sum(float(r[1]) for r in g) / len(g)
            pred = (base_mu + sum(PARK_FACTOR.get(r[2], 0.0) for r in g)/len(g)
                    + slope * sh * (sum(r[0] for r in g)/len(g) - mean_x))
            tot += abs(pred - act)
        e = tot / len(groups)
        mark = ""
        if best_e is None or e < best_e:
            best, best_e = sh, e
        print(f"{sh:>8.1f}{slope*sh:>12.3f}{e:>30.4f}{mark}")
    print(f"\n  CV picks shrink = {best:.1f}  ->  effective slope "
          f"{-8.566*best:+.3f} runs per unit K-BB%")
    print(f"  (applied to the FULL-train slope -8.566, since more data is better")
    print(f"   for the slope itself; CV chose only how much of it to believe.)")

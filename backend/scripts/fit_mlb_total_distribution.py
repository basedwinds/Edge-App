"""MLB game totals are priced off a NORMAL. Run totals are right-skewed counts.

THE LIVE DEFECT (calibration_report, 2026-08-15). `mlb/total` is one of five
flagged cells, and the two sides of the market disagree in opposite directions --
which is what proves it is real rather than a one-sided logging artifact:

    side=over    n=982  claimed 0.605  actual 0.519  gap +0.086
    side=under   n=201  claimed 0.438  actual 0.562  gap -0.124

A single directional fact: the model over-predicts scoring. By line, the miss
concentrates exactly where the volume is (7.5-10.5), all four CIs excluding the
claim.

IT IS NOT THE STALE CONSTANT. LEAGUE_AVG_TOTAL=9.0486 vs a real 2026 mean of
8.958 is 0.09 runs high, worth under 1pp of probability at TOTAL_STD=4.489. The
live miss is 13-15pp.

IT IS THE DISTRIBUTION SHAPE. `prob_over` is `1 - norm_cdf(line, mu, TOTAL_STD)`
-- a symmetric Normal for an overdispersed, right-skewed count. Real totals:

    2023  n=2433  mean 9.235  median 9.0  std 4.577  skew +0.636
    2024  n=2428  mean 8.782  median 8.0  std 4.312  skew +0.731
    2025  n=2429  mean 8.897  median 8.0  std 4.593  skew +0.823
    2026  n=1839  mean 8.958  median 8.0  std 4.533  skew +0.739

Median sits a full run below the mean every season. A right-skewed variable has
P(over ~mean) BELOW 0.5; a symmetric Normal says exactly 0.5. Handed the CORRECT
mean, the Normal still overstates P(over) by +0.060 at 7.5, +0.054 at 8.5 and 9.5
-- pure shape error, independent of any constant.

WHY NEGATIVE BINOMIAL AND NOT POISSON. Variance is ~20.6 against a mean of ~9.0,
so the counts are heavily OVERDISPERSED; Poisson forces variance == mean and
would be far too tight (std ~3.0 vs a real ~4.5). NB has exactly the extra
dispersion parameter that gap calls for, and produces right skew for free rather
than by assumption.

PROTOCOL. Fit on 2023-2025 (7,290 real games, pulled from the free MLB statsapi),
validate on 2026 (1,839) which is never used to fit. Report the Normal and the NB
against the SAME held-out season. NB ships only if it is better across the lines
that actually carry volume -- being better on average while worse at 8.5 would
not be an improvement to the thing being priced.

Run: backend/.venv/Scripts/python.exe scripts/fit_mlb_total_distribution.py
"""
from __future__ import annotations

import json
import math
import statistics
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.game_lines_mlb import _norm_cdf, TOTAL_STD  # noqa: E402

TRAIN_SEASONS = (2023, 2024, 2025)
TEST_SEASON = 2026
# Lines that actually carry volume, from the settled observation log.
LINES = [6.5, 7.5, 8.5, 9.5, 10.5, 11.5]


def season_totals(year: int) -> list[int]:
    u = (f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&season={year}"
         f"&startDate={year}-03-01&endDate={year}-11-15&gameType=R"
         f"&fields=dates,games,teams,home,away,score,status,detailedState")
    d = json.load(urllib.request.urlopen(u, timeout=120))
    out = []
    for day in d.get("dates", []):
        for g in day.get("games", []):
            if (g.get("status") or {}).get("detailedState") != "Final":
                continue
            t = g.get("teams") or {}
            h = (t.get("home") or {}).get("score")
            a = (t.get("away") or {}).get("score")
            if h is None or a is None:
                continue
            out.append(h + a)
    return out


def nb_params(mean: float, var: float) -> tuple[float, float]:
    """(r, p) for a negative binomial with this mean and variance.
    var = mean + mean^2 / r  ->  r = mean^2 / (var - mean). p = mean/(mean+r)."""
    if var <= mean:
        raise ValueError("not overdispersed -- NB is the wrong family here")
    r = mean * mean / (var - mean)
    p = mean / (mean + r)
    return r, p


def nb_sf(line: float, mean: float, r: float) -> float:
    """P(X > line) for integer X. Lines are half-integers so there is no push
    and no continuity correction to argue about: P(X > 8.5) == P(X >= 9)."""
    p = mean / (mean + r)
    k_max = int(math.floor(line))
    # log pmf: lgamma(k+r) - lgamma(r) - lgamma(k+1) + r*log(1-p) + k*log(p)
    cdf = 0.0
    log1mp, logp = math.log(1.0 - p), math.log(p)
    base = -math.lgamma(r) + r * log1mp
    for k in range(0, k_max + 1):
        cdf += math.exp(base + math.lgamma(k + r) - math.lgamma(k + 1) + k * logp)
    return max(0.0, 1.0 - cdf)


def main() -> None:
    train: list[int] = []
    for y in TRAIN_SEASONS:
        v = season_totals(y)
        train += v
        print(f"train {y}: n={len(v)}")
    test = season_totals(TEST_SEASON)
    print(f"test  {TEST_SEASON}: n={len(test)}  (never used to fit)")

    mu_tr = sum(train) / len(train)
    var_tr = statistics.pvariance(train)
    r, p = nb_params(mu_tr, var_tr)
    print(f"\nTRAIN  mean {mu_tr:.4f}  var {var_tr:.4f}  std {math.sqrt(var_tr):.4f}")
    print(f"NB fit r={r:.4f}  p={p:.4f}   (implied std {math.sqrt(mu_tr + mu_tr**2/r):.4f})")

    mu_te = sum(test) / len(test)
    print(f"TEST   mean {mu_te:.4f}")

    # Both models are given the TEST season's own mean, so this isolates SHAPE.
    # Giving either one a stale mean would be measuring the constant instead.
    print(f"\nHELD-OUT {TEST_SEASON}: predicted vs actual P(over), both at the true mean")
    print(f"{'line':>7}{'actual':>10}{'Normal':>10}{'err':>9}{'NegBin':>10}{'err':>9}   winner")
    print("-" * 66)
    n = len(test)
    wins = {"Normal": 0, "NegBin": 0}
    tot_norm = tot_nb = 0.0
    for line in LINES:
        act = sum(1 for x in test if x > line) / n
        nrm = 1.0 - _norm_cdf(line, mu_te, TOTAL_STD)
        nb = nb_sf(line, mu_te, r)
        e_n, e_b = abs(nrm - act), abs(nb - act)
        tot_norm += e_n
        tot_nb += e_b
        w = "NegBin" if e_b < e_n else "Normal"
        wins[w] += 1
        print(f"{line:>7.1f}{act:>10.3f}{nrm:>10.3f}{nrm-act:>+9.3f}"
              f"{nb:>10.3f}{nb-act:>+9.3f}   {w}")
    print("-" * 66)
    print(f"{'mean |err|':>7}{'':>10}{tot_norm/len(LINES):>10.4f}{'':>9}{tot_nb/len(LINES):>10.4f}")
    print(f"lines won: NegBin {wins['NegBin']} / Normal {wins['Normal']}")

    better_everywhere = wins["NegBin"] == len(LINES)
    print()
    if better_everywhere:
        print(f"  SHIP: NegBin(r={r:.4f}) is closer at EVERY volume-carrying line on a")
        print(f"  season it was not fitted to. Mean absolute error "
              f"{tot_norm/len(LINES):.4f} -> {tot_nb/len(LINES):.4f}.")
    elif tot_nb < tot_norm:
        print(f"  MIXED: NegBin wins on average ({tot_nb/len(LINES):.4f} vs "
              f"{tot_norm/len(LINES):.4f}) but loses at {wins['Normal']} line(s).")
        print(f"  Check WHICH lines lose and how much volume they carry before shipping.")
    else:
        print(f"  DO NOT SHIP: NegBin is not better out of sample.")

    print(f"\nSuggested constants:")
    print(f"  TOTAL_NB_DISPERSION = {r:.4f}   # fitted 2023-2025, n={len(train)}")


if __name__ == "__main__":
    main()

"""Fit CFB's own margin model. NFL's constants must NOT be borrowed.

WHY IT HAS TO BE MEASURED. game_lines.MARGIN_SLOPE/MARGIN_STD are NFL numbers
(0.04146 / 13.52). College football is a different sport statistically -- far
wider talent spread, so blowouts are routine -- and using NFL's spread width
would price every CFB game as far more certain than it is.

METHOD. Replay data/cfb_game_cache.json chronologically with the same Elo update
the app's own CFB service uses, so the ratings a game is predicted from are
PRE-GAME (no leakage from its own result), then regress actual margin on the
pre-game Elo difference. Slope is the points-per-Elo-point conversion; the
residual standard deviation is the spread width.

Reports out-of-sample by season -- fit on prior seasons, score the held-out one
-- because a slope fitted and scored on the same games flatters itself.
"""
import json
import math
import statistics
from collections import defaultdict

CACHE = "data/cfb_game_cache.json"
K = 20.0            # matches elo_service_cfb's update size
HOME_ADV = 55.0     # CFB home edge in Elo points, fitted below and reported
BASE = 1500.0
REVERT = 0.25       # season-to-season regression toward the mean


def load():
    d = json.load(open(CACHE))
    rows = [g for g in d.values()
            if g.get("home_score") is not None and g.get("away_score") is not None]
    rows.sort(key=lambda g: (g["season"], g["date"]))
    return rows


def replay(rows, home_adv=HOME_ADV):
    """Yield (elo_diff_pre_game, actual_home_margin, season) per game."""
    r = defaultdict(lambda: BASE)
    season = None
    out = []
    for g in rows:
        if g["season"] != season:
            season = g["season"]
            for t in list(r):  # carry ratings across seasons, partially reverted
                r[t] = BASE + (r[t] - BASE) * (1 - REVERT)
        h, a = g["home_abbr"], g["away_abbr"]
        neutral = bool(g.get("neutral"))
        diff = (r[h] + (0.0 if neutral else home_adv)) - r[a]
        margin = g["home_score"] - g["away_score"]
        out.append((diff, margin, season, g["home_score"] + g["away_score"]))
        exp = 1.0 / (1.0 + 10 ** (-diff / 400.0))
        actual = 1.0 if margin > 0 else (0.5 if margin == 0 else 0.0)
        r[h] += K * (actual - exp)
        r[a] -= K * (actual - exp)
    return out


def fit(pairs):
    """Least-squares slope through the origin, plus residual sd."""
    num = sum(d * m for d, m, *_ in pairs)
    den = sum(d * d for d, _m, *_ in pairs)
    slope = num / den if den else 0.0
    resid = [m - slope * d for d, m, *_ in pairs]
    return slope, statistics.pstdev(resid)


rows = load()
print(f"CFB games with final scores: {len(rows)}  seasons "
      f"{min(g['season'] for g in rows)}-{max(g['season'] for g in rows)}")

# Home advantage: pick the value that best centres the residuals.
print("\nhome-field advantage sweep (Elo points):")
best = None
for hfa in (0, 25, 40, 55, 65, 80, 100):
    pairs = replay(rows, home_adv=hfa)
    slope, sd = fit(pairs)
    bias = statistics.mean(m - slope * d for d, m, *_ in pairs)
    print(f"  hfa={hfa:4}  slope={slope:.5f}  resid sd={sd:5.2f}  mean resid={bias:+6.3f}")
    if best is None or abs(bias) < abs(best[1]):
        best = (hfa, bias, slope, sd)
print(f"  -> least-biased hfa={best[0]}")

pairs = replay(rows, home_adv=best[0])
slope, sd = fit(pairs)
print(f"\nFITTED ON ALL SEASONS:  MARGIN_SLOPE={slope:.5f}  MARGIN_STD={sd:.2f}")
print(f"  NFL for comparison:     MARGIN_SLOPE=0.04146    MARGIN_STD=13.52")

print("\nOUT-OF-SAMPLE by season (fit on the others, score the held-out one):")
seasons = sorted({s for _d, _m, s, _t in pairs})
for hold in seasons:
    tr = [p for p in pairs if p[2] != hold]
    te = [p for p in pairs if p[2] == hold]
    s_tr, _sd_tr = fit(tr)
    resid = [m - s_tr * d for d, m, *_ in te]
    print(f"  {hold}: n={len(te):4}  slope(train)={s_tr:.5f}  "
          f"held-out sd={statistics.pstdev(resid):5.2f}  mean resid={statistics.mean(resid):+6.3f}")

print("\nTOTALS (for a future totals model, not fitted here):")
totals = [t for _d, _m, _s, t in pairs]
print(f"  league mean total={statistics.mean(totals):.2f}  sd={statistics.pstdev(totals):.2f}")
print(f"  NFL for comparison: mean=44.16  naive sd=14.14")
print("  A totals MODEL needs per-team offence/defence strength, which CFB does")
print("  not have -- the league mean alone cannot price a specific matchup.")

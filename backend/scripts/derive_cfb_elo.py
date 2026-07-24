"""Derives the College Football (FBS) baseline Elo (core-5 expansion) from
data/cfb_game_cache.json. Same structure as the CBB/NFL builds (season
regression, measured home-field advantage, neutral-site bowls), grid-searching
K on walk-forward Brier. Teams keyed by ESPN team id.

Market-odds backtest DEFERRED: CFB starts ~late Aug and Kalshi only retains ~2
months of settled markets, so KXNCAAFGAME closing prices aren't available until
the season opens. This validates model quality now.
"""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.calibration import brier_score  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CACHE = DATA_DIR / "cfb_game_cache.json"
BASE = 1500.0
SEASON_REGRESSION = 1.0 / 3.0
WARMUP = 900  # ~1.2 seasons of FBS


def load():
    rows = list(json.loads(CACHE.read_text(encoding="utf-8")).values())
    rows.sort(key=lambda g: (g["date"], g["id"]))
    return rows


def win_prob(hr, ar, adv):
    return 1.0 / (1.0 + 10 ** (-((hr + adv) - ar) / 400.0))


def measure_home_adv(rows):
    nn = [g for g in rows if not g["neutral"]]
    hw = sum(1 for g in nn if g["home_score"] > g["away_score"]) / len(nn)
    return hw, 400.0 * math.log10(hw / (1 - hw))


def run(rows, k, adv):
    r = {}
    cur = None
    preds, outs = [], []
    for i, g in enumerate(rows):
        if g["season"] != cur:
            cur = g["season"]
            for t in r:
                r[t] = BASE + (1 - SEASON_REGRESSION) * (r[t] - BASE)
        h, a = g["home"], g["away"]
        hr, ar = r.get(h, BASE), r.get(a, BASE)
        hadv = 0.0 if g["neutral"] else adv
        p = win_prob(hr, ar, hadv)
        actual = 1.0 if g["home_score"] > g["away_score"] else 0.0
        if i >= WARMUP:
            preds.append(p); outs.append(actual)
        delta = k * (actual - p)
        r[h] = hr + delta
        r[a] = ar - delta
    return preds, outs


def main():
    rows = load()
    hw, adv = measure_home_adv(rows)
    print(f"{len(rows)} CFB games (2021-2025). Home win rate {hw:.3f} -> home-field adv {adv:.1f} Elo pts")
    print(f"\n{'K':>5} {'Brier':>10} {'accuracy':>9}")
    best = None
    for k in (16, 20, 24, 28, 32, 40, 48, 56):
        p, o = run(rows, k, adv)
        b = brier_score(p, o)
        acc = sum(1 for pp, oo in zip(p, o) if (pp >= 0.5) == (oo >= 0.5)) / len(o)
        print(f"{k:>5} {b:>10.5f} {acc:>9.4f}")
        if best is None or b < best[1]:
            best = (k, b, acc)
    naive = brier_score([0.5] * len(run(rows, best[0], adv)[1]), run(rows, best[0], adv)[1])
    print(f"\nbest K={best[0]}: Brier {best[1]:.5f}, accuracy {best[2]:.4f}")
    print(f"naive 0.5 baseline Brier: {naive:.5f}")
    print("(market-odds edge backtest deferred to season; KXNCAAFGAME markets purged off-season)")


if __name__ == "__main__":
    main()

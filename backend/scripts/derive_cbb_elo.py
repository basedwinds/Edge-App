"""Derives the men's College Basketball baseline Elo (core-5 expansion) from
data/cbb_game_cache.json. Mirrors the WNBA/NBA structure (season regression,
measured home-court advantage, neutral-site handling for tournaments), grid-
searching K on walk-forward Brier. Teams are keyed by ESPN team id (abbrevs
collide across ~360 D1 schools); the ~700 non-D1 guarantee-game opponents just
sit near BASE, which is correct.

Market-odds backtest is DEFERRED: CBB is off-season and Kalshi only retains ~2
months of settled markets, so KXNCAAMBGAME closing prices are purged until the
season relists (~Nov). This script validates model quality now; the edge read
comes when markets return.
"""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.calibration import brier_score  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CACHE = DATA_DIR / "cbb_game_cache.json"
BASE = 1500.0
SEASON_REGRESSION = 1.0 / 3.0
WARMUP = 4000  # ~most of season 1 (huge team pool needs a long burn-in)


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
    print(f"{len(rows)} CBB games (2022-2026). Home win rate {hw:.3f} -> home-court adv {adv:.1f} Elo pts")
    print(f"\n{'K':>5} {'Brier':>10} {'accuracy':>9}")
    best = None
    for k in (12, 16, 20, 24, 28, 32, 40, 48):
        p, o = run(rows, k, adv)
        b = brier_score(p, o)
        acc = sum(1 for pp, oo in zip(p, o) if (pp >= 0.5) == (oo >= 0.5)) / len(o)
        print(f"{k:>5} {b:>10.5f} {acc:>9.4f}")
        if best is None or b < best[1]:
            best = (k, b, acc)
    naive = brier_score([0.5] * len(run(rows, best[0], adv)[1]), run(rows, best[0], adv)[1])
    print(f"\nbest K={best[0]}: Brier {best[1]:.5f}, accuracy {best[2]:.4f}")
    print(f"naive 0.5 baseline Brier: {naive:.5f}")
    print("(market-odds edge backtest deferred to season; KXNCAAMBGAME markets purged off-season)")


if __name__ == "__main__":
    main()

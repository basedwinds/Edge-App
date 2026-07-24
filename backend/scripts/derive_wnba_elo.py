"""Derives the WNBA baseline team Elo (task #40) -- mirrors elo_nba.py's
structure (season regression, home-court advantage, neutral-site handling),
grid-searched against real WNBA game history (data/wnba_game_cache.json).
Home-court advantage is measured from the data, then K is grid-searched on
walk-forward Brier. Reports accuracy vs the naive 0.5 baseline.
"""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.calibration import brier_score  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CACHE = DATA_DIR / "wnba_game_cache.json"
BASE = 1500.0
SEASON_REGRESSION = 1.0 / 3.0  # same as NBA/NFL, a sane default (can re-tune)
WARMUP = 220  # ~1 season


def load():
    rows = list(json.loads(CACHE.read_text(encoding="utf-8")).values())
    rows.sort(key=lambda g: (g["date"], g["id"]))
    # compute rest days per team
    last = {}
    for g in rows:
        d = g["date"]
        for side in ("home", "away"):
            t = g[side]
            g[f"{side}_rest"] = (_days(d, last[t]) if t in last else None)
        last[g["home"]] = d
        last[g["away"]] = d
    return rows


def _days(a, b):
    import datetime as dt
    return (dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days


def win_prob(hr, ar, adv):
    return 1.0 / (1.0 + 10 ** (-((hr + adv) - ar) / 400.0))


def measure_home_adv(rows):
    nonneutral = [g for g in rows if not g["neutral"]]
    hw = sum(1 for g in nonneutral if g["home_score"] > g["away_score"]) / len(nonneutral)
    adv = 400.0 * math.log10(hw / (1 - hw))
    return hw, adv


def run(rows, k, adv):
    r = {}
    cur_season = None
    preds, outs = [], []
    for i, g in enumerate(rows):
        if g["season"] != cur_season:
            cur_season = g["season"]
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
    print(f"{len(rows)} WNBA games (2021-2026). Home win rate {hw:.3f} -> home-court adv {adv:.1f} Elo pts")
    print(f"\n{'K':>5} {'Brier':>10} {'accuracy':>9}")
    best = None
    for k in (8, 12, 16, 20, 24, 28, 32, 40):
        p, o = run(rows, k, adv)
        b = brier_score(p, o)
        acc = sum(1 for pp, oo in zip(p, o) if (pp >= 0.5) == (oo >= 0.5)) / len(o)
        print(f"{k:>5} {b:>10.5f} {acc:>9.4f}")
        if best is None or b < best[1]:
            best = (k, b, acc)
    naive = brier_score([0.5] * len(run(rows, best[0], adv)[1]), run(rows, best[0], adv)[1])
    print(f"\nbest K={best[0]}: Brier {best[1]:.5f}, accuracy {best[2]:.4f}")
    print(f"naive 0.5 baseline Brier: {naive:.5f}")


if __name__ == "__main__":
    main()

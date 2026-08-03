"""Walk-forward calibration audit for the CFB Elo, split by rating gap.

WHY. Live CFB moneyline prices went visible for the first time on 2026-08-03
(the client had been reading Kalshi price keys that don't exist, so every
snapshot was empty). The first 30 real rows showed the model at LSU 5.8% to win
at Ole Miss where the market said 47%, and 25 of 30 rows more extreme than the
market -- while the MEAN edge was ~0.000, i.e. unbiased overall.

That pattern -- unbiased on average, far too extreme per game -- is what you get
when a model is fine on mismatches and overconfident on close games. The shipped
temperature (T=0.83) SHARPENS, because it was fit on all games at once, and CFB
schedules are full of blowouts that genuinely deserve extreme probabilities.

This script tests that directly: replay Elo strictly walk-forward (every game
rated on the state BEFORE it, so nothing leaks), then measure calibration inside
rating-gap bands.

Run: python -m scripts.cfb_calibration_audit
"""
import json
import pathlib
import statistics
from collections import defaultdict

from app.models import calibration_temp
from app.models.baseline import elo_cfb

CACHE = pathlib.Path(__file__).resolve().parents[2] / "data" / "cfb_game_cache.json"


def load_games():
    raw = json.loads(CACHE.read_text(encoding="utf-8"))
    games = [g for g in raw.values()
             if g.get("home_score") is not None and g.get("away_score") is not None
             and g.get("home_abbr") and g.get("away_abbr")]
    games.sort(key=lambda g: (g.get("date") or "", g.get("id") or ""))
    return games


def walk_forward(games):
    """(elo_diff_before, home_won, season) per game, replaying ratings as we go.

    Ratings update AFTER each game is recorded, so the diff used for a game
    never contains that game's own result.
    """
    state = elo_cfb.EloState()
    out = []
    for g in games:
        h, a = g["home_abbr"], g["away_abbr"]
        neutral = bool(g.get("neutral"))
        hfa = elo_cfb.effective_home_field_adv(neutral)
        hr = state.ratings.get(h, elo_cfb.BASE_RATING)
        ar = state.ratings.get(a, elo_cfb.BASE_RATING)
        diff = hr - ar + hfa
        out.append((diff, 1 if g["home_score"] > g["away_score"] else 0, g.get("season")))
        elo_cfb.update_ratings(state, h, a, g["home_score"], g["away_score"], home_field_adv=hfa)
    return out


def _temper(p, t):
    """Same transform as calibration_temp.apply, without its sport lookup."""
    if t is None or t == 1.0:
        return p
    import math
    p = min(max(p, 1e-6), 1 - 1e-6)
    logit = math.log(p / (1 - p))
    return 1.0 / (1.0 + math.exp(-logit / t))


def _p(diff, temp=None):
    return _temper(1.0 / (1.0 + 10 ** (-diff / 400.0)), temp)


def brier(rows, fn):
    return statistics.mean((fn(d) - y) ** 2 for d, y, _ in rows)


def logloss(rows, fn):
    import math
    tot = 0.0
    for d, y, _ in rows:
        p = min(max(fn(d), 1e-9), 1 - 1e-9)
        tot += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return tot / len(rows)


def report_bands(rows, fn, label):
    """Predicted vs actual inside |elo diff| bands -- where the damage shows."""
    bands = [(0, 100), (100, 200), (200, 300), (300, 500), (500, 10_000)]
    print(f"\n  {label}")
    print(f"    {'|elo gap|':>12}{'n':>7}{'pred':>9}{'actual':>9}{'err':>9}")
    for lo, hi in bands:
        sub = [(d, y) for d, y, _ in rows if lo <= abs(d) < hi]
        if len(sub) < 30:
            continue
        # Score from the FAVOURITE's side. Averaging raw home probabilities over
        # a band mixes home favourites (p~0.9) with away favourites (p~0.1) and
        # collapses toward 0.5, which hides the very error being looked for.
        pred = statistics.mean(max(fn(d), 1 - fn(d)) for d, _ in sub)
        act = statistics.mean((y if fn(d) >= 0.5 else 1 - y) for d, y in sub)
        print(f"    {f'{lo}-{hi}':>12}{len(sub):7}{pred:9.3f}{act:9.3f}{pred-act:+9.3f}")


def main():
    games = load_games()
    rows = walk_forward(games)
    # Skip the burn-in season: every team starts at 1500, so those diffs are
    # meaningless and would flatter any calibration fit.
    seasons = sorted({s for _, _, s in rows if s is not None})
    warm = rows[len(rows) // 6:] if len(seasons) < 2 else [r for r in rows if r[2] != seasons[0]]
    print(f"games={len(rows)}  seasons={seasons}  scored after burn-in={len(warm)}")

    shipped_t = calibration_temp.TEMPERATURE.get("cfb")
    print(f"\nshipped temperature for cfb: {shipped_t}")
    print(f"  raw    brier={brier(warm, lambda d: _p(d)):.5f}  logloss={logloss(warm, lambda d: _p(d)):.5f}")
    print(f"  T={shipped_t}  brier={brier(warm, lambda d: _p(d, shipped_t)):.5f}  "
          f"logloss={logloss(warm, lambda d: _p(d, shipped_t)):.5f}")

    report_bands(warm, lambda d: _p(d), "RAW (no temperature)")
    report_bands(warm, lambda d: _p(d, shipped_t), f"SHIPPED (T={shipped_t})")

    # Grid-search a temperature honestly: fit on the earlier seasons, score on
    # the held-out later ones, so the reported number isn't fit on its own test.
    if len(seasons) >= 3:
        cut = seasons[len(seasons) * 2 // 3]
        train = [r for r in warm if r[2] < cut]
        test = [r for r in warm if r[2] >= cut]
    else:
        cut = None
        train = warm[: int(len(warm) * 0.67)]
        test = warm[int(len(warm) * 0.67):]
    print(f"\ntrain={len(train)}  test={len(test)}  (split at season {cut})")

    grid = [round(x, 2) for x in [0.6 + 0.02 * i for i in range(56)]]
    best = min(grid, key=lambda t: logloss(train, lambda d: _p(d, t)))
    print(f"  best T on TRAIN = {best}")
    for name, t in (("raw", None), ("shipped", shipped_t), ("fitted", best)):
        print(f"    {name:8} T={str(t):5} test brier={brier(test, lambda d: _p(d, t)):.5f}  "
              f"test logloss={logloss(test, lambda d: _p(d, t)):.5f}")

    report_bands(test, lambda d: _p(d, best), f"FITTED T={best} (held-out seasons only)")
    report_bands(test, lambda d: _p(d, shipped_t), f"SHIPPED T={shipped_t} (held-out seasons only)")

    # And the real question: does ONE temperature work everywhere, or does the
    # right value differ between close games and mismatches?
    print("\n  best T fitted WITHIN each gap band (train only):")
    for lo, hi in [(0, 100), (100, 200), (200, 300), (300, 500), (500, 10_000)]:
        sub = [r for r in train if lo <= abs(r[0]) < hi]
        if len(sub) < 100:
            continue
        bt = min(grid, key=lambda t: logloss(sub, lambda d: _p(d, t)))
        print(f"    gap {lo}-{hi}: n={len(sub)}  best T={bt}")


if __name__ == "__main__":
    main()

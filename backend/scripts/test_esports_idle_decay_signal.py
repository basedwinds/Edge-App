"""Should esports Elo ratings DECAY toward the mean while a team sits idle?
And if a decay term helps, is it actually about IDLENESS -- or just shrinkage?

THE GAP THIS TESTS. Every traditional-sport Elo in this app regresses ratings
toward the mean between seasons -- NFL/NBA/WNBA/soccer 1/3, MLB 0.25
(grid-searched), CFB 0.0 (deliberately, held-out validated). **No esports title
has any time-based regression at all.** The justification in
elo_valorant.py/elo_lol.py is inherited word-for-word from elo_mma.py/
elo_tennis.py -- "no discrete season structure to regress between" -- which was
never measured for esports, and is questionable given esports rosters turn over
MORE than NFL rosters, not less.

WHY CONTINUOUS IDLE-DECAY RATHER THAN A SEASON RESET. Esports has no clean
season boundary in this data (regional splits, rolling international events,
open qualifiers). An offseason IS just a long idle gap, so decaying by ELAPSED
IDLE TIME subsumes a season reset and also covers the offseason, a roster
sabbatical and a team that quietly stops attending events -- without needing a
season label the crawl doesn't carry.

    TIME  mode:  rating <- BASE + (rating - BASE) * RETENTION ** (idle_days / 30)
    MATCH mode:  rating <- BASE + (rating - BASE) * RETENTION       (per appearance)

**THE TWO MODES ARE THE WHOLE POINT.** A gain in TIME mode is ambiguous on its
own: with a median idle gap of 1-3 days, `RETENTION ** (idle/30)` is a small
shrink applied at nearly every match, which is indistinguishable from plain
regularisation -- pulling over-dispersed ratings back toward 1500. MATCH mode is
the same shrinkage with the clock removed. So:

    TIME helps, MATCH helps the same     -> it is SHRINKAGE. Nothing to do with
                                            recency. Ship it as regularisation
                                            (or as a lower K) -- not as decay.
    TIME helps, MATCH does not           -> genuinely about elapsed IDLE TIME.
    neither helps                        -> the null stands.

Same descriptive+interventional pairing that settled the LoL patch question
([[project_lol_patch_rejected]]).

NOT THE SAME AS THE REJECTED STALENESS GATE.
check_esports_rating_staleness.py tested a BINARY gate -- refuse to price a team
idle > N days -- and rejected it (LoL n=24 looked damning, contradicted by
Valorant n=142; see [[project_esports_rating_staleness_rejected]]). This is a
different intervention: continuous, graded, applies to every team including
mildly-idle ones, and shrinks a rating rather than suppressing a bet. A gate
failing says nothing about whether decay helps.

K IS SWEPT JOINTLY, NOT HELD FIXED. Decay shrinks ratings, so the optimal K can
move -- pinning K at the shipped value would test decay with a handicap and
could reject a real effect. Each title's shipped K is always in its grid.

EACH TITLE USES ITS OWN derive_*_elo_constants.py LOADER, IMPORTED. Not a
reimplementation: a first pass here hand-rolled a best_of inference and a
`shipped_k` guess, which silently fed Valorant 23,297 rows instead of its real
19,644 and scored K=20 instead of its real K=36 -- a confident, wrong answer.
The documented Brier is now asserted for EVERY title before any grid is read;
the first pass only asserted it where a number happened to be handy, which is
exactly where it failed to catch anything. See the staleness memo for the
earlier incarnation of this same mistake (`winner` is the literal string
"team_a"/"team_b", never a team name).

=============================== VERDICT (2026-08-13) ==========================

NOT SHIPPED. All three baselines reproduced exactly (cs2 0.23368, valorant
0.22506, lol 0.20727) before any grid was read.

    title      baseline    best TIME                  best MATCH
    cs2         0.23368    0.23362 (K=32,r=0.99)      0.23368 (r=1.00, null)
    valorant    0.22506    0.22457 (K=36,r=0.98)      0.22506 (r=1.00, null)
    lol         0.20727    0.20576 (K=28,r=0.96)      0.20711 (K=28,r=0.99)

**MATCH mode never helps anywhere** -- it is monotonically worse in every column
of every title. So the shrinkage confound is REFUTED: what TIME mode picks up is
genuinely about elapsed calendar time. The control was worth running, and it
cleared the interpretation rather than killing it.

**CS2 and Valorant: the null wins.** CS2 gains 0.00006 (noise). Valorant gains
0.00049 with a credible interior basin, but that is 0.2% relative -- an order of
magnitude below the CS2 player-blend's own 0.00819, which is what a shippable
esports change has looked like in this app.

**LoL is the only real candidate and it is still not shippable.** -0.00151 at
K=28/r=0.96 (-0.00145 at the shipped K=24), a smooth basin in BOTH dimensions,
the shape this codebase treats as credible. Two reasons it stays on the shelf:

  1. IT CANNOT CLEAR THE MARKET GATE. lol_market_odds_backtest_cache.json holds
     132 events and exactly **12** with a usable pre-match price. A 0.0015 Brier
     difference is invisible at n=12. Every esports change that shipped here was
     confirmed against real closing prices first (see elo_cs2.py's K_PLAYER
     note), and this app's documented history is that self-Brier gains DID NOT
     transfer -- h2h, rest and roster all improved self-Brier while WIDENING the
     market gap.
  2. THE EFFECT SIZE RUNS BACKWARDS TO THE IDLENESS IT SUPPOSEDLY MODELS. LoL
     has the LEAST idle time of the three (>=90d 1.9%, >=180d 0.5%) and the
     LARGEST gain; CS2 and Valorant have ~2x the idle share and gain nothing. A
     staleness mechanism predicts the opposite ordering.

THE MECHANISM THAT DOES FIT (2) is not staleness but SYNCHRONISED BREAKS: LoL
runs regional splits where an entire league goes idle together, so decay there
acts as a league-wide between-split regression -- i.e. exactly the
SEASON_REGRESSION every traditional sport in this app already ships. CS2 and
Valorant run rolling calendars with no shared boundary, so the same term only
jitters individual teams. That predicts the observed CS2/Valorant/LoL ordering.

REVISIT WHEN the LoL market sample is large enough to resolve 0.0015 (order 1000
priced events, vs 12 today), and test it AS a split-boundary regression keyed on
the split, not as continuous idle decay -- the continuous form is a proxy for the
boundary, and the boundary is the thing with the mechanism behind it.

Run: backend/.venv/Scripts/python.exe scripts/test_esports_idle_decay_signal.py
"""
import datetime
import math
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS.parent))
sys.path.insert(0, str(SCRIPTS))

import derive_cs2_elo_constants as d_cs2  # noqa: E402
import derive_lol_elo_constants as d_lol  # noqa: E402
import derive_valorant_elo_constants as d_val  # noqa: E402

from app.models.calibration import brier_score  # noqa: E402
from app.models.baseline.elo_cs2 import BASE_RATING, map_win_prob, series_score_distribution  # noqa: E402
from app.models.baseline.elo_cs2 import K as K_CS2  # noqa: E402
from app.models.baseline.elo_lol import K as K_LOL  # noqa: E402
from app.models.baseline.elo_valorant import K as K_VAL  # noqa: E402

# Fraction of a team's rating EDGE kept per 30 idle days (TIME) or per
# appearance (MATCH). 1.0 == today's behaviour, the null.
RETENTION_GRID = [1.00, 0.99, 0.98, 0.96, 0.94, 0.90, 0.85, 0.80]

TITLES = {
    "cs2": {
        "loader": d_cs2.load_matches,
        "shipped_k": K_CS2,
        "k_grid": [24, 28, 32, 40, 48],
        "warmup": d_cs2.WARMUP,
        "per_map": False,          # CS2 updates per SERIES -- per-map measured and rejected
        "documented_brier": 0.23368,
    },
    "valorant": {
        "loader": d_val.load_matches,
        "shipped_k": K_VAL,
        "k_grid": [24, 28, 32, 36, 40, 48],
        "warmup": d_val.WARMUP,
        "per_map": True,
        "documented_brier": 0.22506,
    },
    "lol": {
        "loader": d_lol.load_matches,
        "shipped_k": K_LOL,
        "k_grid": [16, 20, 24, 28, 32, 40],
        "warmup": d_lol.WARMUP,
        "per_map": True,
        "documented_brier": 0.20727,
    },
}


def match_day(m: dict) -> datetime.date:
    return datetime.date.fromisoformat((m.get("estimated_start_time") or m["match_date"])[:10])


def prob_series_win_a(a_r: float, b_r: float, best_of: int) -> float:
    dist = series_score_distribution(map_win_prob(a_r, b_r), best_of)
    return sum(p for (a, b), p in dist.items() if a > b)


def run(matches, k: float, retention: float, per_map: bool, mode: str):
    """Walk forward. retention < 1.0 pulls a rating toward BASE_RATING --
    by elapsed idle days (mode='time') or once per appearance (mode='match')."""
    ratings: dict[str, float] = {}
    last_seen: dict[str, datetime.date] = {}
    preds: list[float] = []
    outcomes: list[float] = []
    decaying = retention < 1.0

    def shrink(team: str, today: datetime.date) -> float:
        r = ratings.get(team, BASE_RATING)
        if not decaying:
            return r
        if mode == "match":
            return BASE_RATING + (r - BASE_RATING) * retention
        prev = last_seen.get(team)
        if prev is None:
            return r
        idle = (today - prev).days
        if idle <= 0:
            return r
        return BASE_RATING + (r - BASE_RATING) * (retention ** (idle / 30.0))

    def apply_one(team_a, team_b, actual_a):
        a_r, b_r = ratings[team_a], ratings[team_b]
        delta = k * (actual_a - map_win_prob(a_r, b_r))
        ratings[team_a] = a_r + delta
        ratings[team_b] = b_r - delta

    for m in matches:
        team_a, team_b, best_of, winner = m["team_a"], m["team_b"], m["best_of"], m["winner"]
        today = match_day(m)

        # Persist the shrunk value so it compounds and feeds the update.
        ratings[team_a] = shrink(team_a, today)
        ratings[team_b] = shrink(team_b, today)

        preds.append(prob_series_win_a(ratings[team_a], ratings[team_b], best_of))
        outcomes.append(1.0 if winner == "team_a" else 0.0)

        actual_a = 1.0 if winner == "team_a" else 0.0
        wa, wb = m.get("maps_won_a"), m.get("maps_won_b")
        if per_map and wa is not None and wb is not None and (wa + wb) > 0:
            for _ in range(wa):
                apply_one(team_a, team_b, 1.0)
            for _ in range(wb):
                apply_one(team_a, team_b, 0.0)
        else:
            apply_one(team_a, team_b, actual_a)

        last_seen[team_a] = last_seen[team_b] = today

    return preds, outcomes


def idle_profile(matches) -> str:
    """How much idle time is even in this data? A decay term cannot be about
    staleness if teams never sit still -- read this before any Brier."""
    last: dict[str, datetime.date] = {}
    gaps: list[int] = []
    for m in matches:
        today = match_day(m)
        for t in (m["team_a"], m["team_b"]):
            if t in last:
                gaps.append((today - last[t]).days)
            last[t] = today
    gaps.sort()
    n = len(gaps)
    share = lambda d: 100.0 * sum(1 for g in gaps if g >= d) / n  # noqa: E731
    return (f"median gap {gaps[n // 2]}d | >=30d {share(30):.1f}% | "
            f">=90d {share(90):.1f}% | >=180d {share(180):.1f}%")


def sweep(matches, cfg, mode: str, warm: int):
    print(f"\n  --- {mode.upper()} mode ---")
    print(f"  {'retention':>10}  " + "  ".join(f"K={k:<3.0f}" for k in cfg["k_grid"]))
    best = None
    for ret in RETENTION_GRID:
        cells = []
        for k in cfg["k_grid"]:
            preds, out = run(matches, k, ret, cfg["per_map"], mode)
            b = brier_score(preds[warm:], out[warm:])
            cells.append(b)
            if best is None or b < best[0]:
                best = (b, k, ret)
        mark = "   (null)" if ret == 1.00 else ""
        print(f"  {ret:>10.2f}  " + "  ".join(f"{c:.5f}" for c in cells) + mark)
    return best


def main() -> None:
    summary = []
    for title, cfg in TITLES.items():
        matches = cfg["loader"]()
        warm = cfg["warmup"]
        print(f"\n{'=' * 78}\n{title.upper()}  --  {len(matches)} matches, warmup {warm}, "
              f"shipped K={cfg['shipped_k']:.0f}")
        print(f"idle profile: {idle_profile(matches)}")

        base_preds, base_out = run(matches, cfg["shipped_k"], 1.00, cfg["per_map"], "time")
        base = brier_score(base_preds[warm:], base_out[warm:])
        doc = cfg["documented_brier"]
        if abs(base - doc) >= 5e-4:
            print(f"  BASELINE MISMATCH: got {base:.5f}, documented {doc:.5f}. "
                  f"REFUSING to read the grid.")
            continue
        print(f"baseline (shipped K, no decay): Brier {base:.5f}  <-- reproduces documented {doc:.5f}")

        b_time = sweep(matches, cfg, "time", warm)
        b_match = sweep(matches, cfg, "match", warm)
        summary.append((title, base, b_time, b_match))

    print(f"\n{'=' * 78}\nVERDICT\n{'=' * 78}")
    print(f"{'title':10} {'baseline':>9} {'best TIME':>26} {'best MATCH':>26}")
    for title, base, bt, bm in summary:
        print(f"{title:10} {base:>9.5f} "
              f"{f'{bt[0]:.5f} (K={bt[1]:.0f},r={bt[2]:.2f}) {bt[0]-base:+.5f}':>26} "
              f"{f'{bm[0]:.5f} (K={bm[1]:.0f},r={bm[2]:.2f}) {bm[0]-base:+.5f}':>26}")
    print()
    for title, base, bt, bm in summary:
        gain_t, gain_m = base - bt[0], base - bm[0]
        if gain_t < 5e-4:
            verdict = "NULL STANDS -- no decay worth shipping"
        elif gain_m >= gain_t * 0.7:
            verdict = ("SHRINKAGE, NOT STALENESS -- time-blind shrink captures "
                       f"{100 * gain_m / gain_t:.0f}% of the gain")
        else:
            hl = 30.0 * math.log(0.5) / math.log(bt[2])
            verdict = f"REAL IDLE-TIME EFFECT -- half-life ~{hl:.0f} idle days"
        print(f"  {title:10} {verdict}")


if __name__ == "__main__":
    main()

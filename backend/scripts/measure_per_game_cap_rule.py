"""Does the per-game cap's HIGHEST-EDGE selection rule actually cost money?

THE MECHANISM (#198). frontend markets.ts `capToOneRowPerGame` keeps one row per
real-world game and picks it by the largest `edge`. One mis-rated match does not
produce one bad row -- it produces a CORRELATED FAMILY (series_winner, handicap,
map_winner, total all inherit the same wrong rating) -- so the rule selects the
largest distortion in that family by construction. Observed live on cs2 Spirit vs
BIG: `implausible_certainty` blocked the series_winner row at 10.2x but the
handicap row scored 2.9x, never fired, and carried the bigger edge (+28.5pp vs
+13.6pp), so the cap surfaced it.

THAT IS A MECHANISM, NOT A MEASUREMENT. One anecdote motivates this; it does not
settle it. The cap has been in place a long time and every sport's Recommended
list depends on it, so the bar for changing it is a number.

WHAT IS COMPARED. Over settled `model_observations`, restricted to games that
actually had a CHOICE (2+ eligible rows), the realised ROI of:

    HIGHEST EDGE   -- what the cap does today
    LOWEST EDGE    -- the opposite extreme
    RANDOM         -- seeded, so it is reproducible
    MONEYLINE-FIRST-- prefer the simplest market type, fall back to highest edge

All rules pick from the SAME games, so this is a like-for-like comparison of
selection rules and not a comparison of different bet populations.

ROI CONVENTION: 1 unit at `market_prob`, returns 1/market_prob on a win. This is
the midpoint, so every arm is equally optimistic about execution -- the
comparison between arms is unaffected.

ELIGIBILITY is an approximation and is stated as one: `model_observations` stores
no bid/ask and no stake flag, so "would have been recommended" is reconstructed
as edge >= MIN_EDGE_TO_BET with real volume. That is looser than production (no
ask guard, no spread cap), which means the arms include rows the app would have
refused. It is applied IDENTICALLY to every arm, so it cannot favour one.

A CONTROL ARM IS INCLUDED: rows where model and market agree must land near zero
in every rule, or the harness is broken and nothing below can be read.

Run: backend/.venv/Scripts/python.exe scripts/measure_per_game_cap_rule.py
"""
from __future__ import annotations

import random
import sys
from collections import defaultdict
from math import sqrt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.database import SessionLocal  # noqa: E402
from app.models.staking import MIN_EDGE_TO_BET  # noqa: E402
from sqlalchemy import text  # noqa: E402

GAME_ID_COLS = [
    "nfl_game_id", "nba_game_id", "wnba_game_id", "cfb_game_id", "mlb_game_id",
    "mma_fight_id", "tennis_match_id", "soccer_match_id", "valorant_match_id",
    "cs2_match_id", "lol_match_id", "cod_match_id", "race_event_id",
]
# Mirrors the frontend's per-title prefixing, which exists because raw ids
# collide across titles.
MONEYLINE_LIKE = {"moneyline", "moneyline_3way", "series_winner", "match_winner",
                  "race_winner", "fight_winner"}
SEED = 20260815


def game_key(r) -> str | None:
    for c in GAME_ID_COLS:
        v = r._mapping.get(c)
        if v:
            return f"{c}:{v}"
    return None


def roi(rows) -> tuple[float, int]:
    """rows = [(market_prob, won)]. 1 unit staked at market_prob."""
    if not rows:
        return (0.0, 0)
    stake = float(len(rows))
    ret = sum((1.0 / mp) for mp, w in rows if w and mp > 0)
    return (ret / stake - 1.0, len(rows))


def boot_ci(rows, n=2000):
    """Bootstrap CI on ROI -- the payout distribution is heavy-tailed (a 5c
    winner returns 20x), so a normal approximation on the mean is wrong here."""
    if len(rows) < 20:
        return (float("nan"), float("nan"))
    rnd = random.Random(SEED)
    outs = []
    k = len(rows)
    for _ in range(n):
        samp = [rows[rnd.randrange(k)] for _ in range(k)]
        outs.append(roi(samp)[0])
    outs.sort()
    return (outs[int(0.025 * n)], outs[int(0.975 * n)])


def main() -> None:
    s = SessionLocal()
    rows = s.execute(text("""
        SELECT * FROM model_observations
        WHERE status IN ('won','lost') AND model_prob IS NOT NULL
          AND market_prob IS NOT NULL AND market_prob > 0.01 AND market_prob < 0.99
          AND edge IS NOT NULL
    """)).fetchall()
    s.close()

    by_game = defaultdict(list)
    for r in rows:
        k = game_key(r)
        if k:
            by_game[k].append(r)

    def eligible(r):
        return (r.edge or 0) >= MIN_EDGE_TO_BET and (r.volume or 0) > 0

    # Games that actually had a CHOICE. A game with one eligible row is decided
    # the same way by every rule and would only dilute the comparison.
    contested = {}
    for k, v in by_game.items():
        el = [r for r in v if eligible(r)]
        if len(el) >= 2:
            contested[k] = el

    print(f"settled observations              {len(rows)}")
    print(f"distinct games                    {len(by_game)}")
    print(f"games with 2+ eligible rows       {len(contested)}   <- the only ones a rule can differ on")
    if len(contested) < 30:
        print("\nTOO FEW CONTESTED GAMES to measure a selection rule. The cap rarely has a")
        print("real choice, so changing it cannot matter much either way.")
        return
    sizes = sorted(len(v) for v in contested.values())
    print(f"eligible rows per contested game  median {sizes[len(sizes)//2]}, max {max(sizes)}")

    rnd = random.Random(SEED)

    def pick(rule, cands):
        if rule == "highest":
            return max(cands, key=lambda r: r.edge)
        if rule == "lowest":
            return min(cands, key=lambda r: r.edge)
        if rule == "random":
            return cands[rnd.randrange(len(cands))]
        if rule == "moneyline_first":
            ml = [c for c in cands if (c.market_type or "") in MONEYLINE_LIKE]
            return max(ml or cands, key=lambda r: r.edge)
        raise ValueError(rule)

    RULES = ["highest", "lowest", "random", "moneyline_first"]
    print(f"\nREALISED ROI BY SELECTION RULE  (same {len(contested)} games, 1 unit each)")
    print(f"{'rule':18}{'n':>6}{'ROI':>9}{'95% CI':>22}{'win%':>8}{'avg edge':>10}{'avg price':>11}")
    print("-" * 84)
    results = {}
    for rule in RULES:
        picked = [pick(rule, v) for v in contested.values()]
        pairs = [(p.market_prob, p.status == "won") for p in picked]
        r, n = roi(pairs)
        lo, hi = boot_ci(pairs)
        wr = sum(1 for _, w in pairs if w) / n
        ae = sum(p.edge for p in picked) / n
        ap = sum(p.market_prob for p in picked) / n
        results[rule] = (r, picked)
        mark = "   <- the cap today" if rule == "highest" else ""
        print(f"{rule:18}{n:>6}{r:>+8.1%}  [{lo:>+6.1%},{hi:>+6.1%}]{wr:>8.1%}{ae:>10.3f}{ap:>11.3f}{mark}")

    # ---- CONTROL ARM ----
    ctrl = {}
    for k, v in by_game.items():
        el = [r for r in v if abs(r.edge or 0) < 0.02 and (r.volume or 0) > 0]
        if len(el) >= 2:
            ctrl[k] = el
    if len(ctrl) >= 30:
        picked = [max(v, key=lambda r: r.edge) for v in ctrl.values()]
        pairs = [(p.market_prob, p.status == "won") for p in picked]
        r, n = roi(pairs)
        lo, hi = boot_ci(pairs)
        print(f"\nCONTROL (|edge| < 2pp, same rule)  n={n}  ROI {r:+.1%} [{lo:+.1%},{hi:+.1%}]")
        print("  Must be near zero. If it is not, the grading or the price field is broken")
        print("  and every number above is meaningless.")

    # ---- Is the effect concentrated in DERIVED markets? ----
    print(f"\nWHAT THE CAP ACTUALLY PICKS (highest-edge rule)")
    picked = results["highest"][1]
    by_mt = defaultdict(list)
    for p in picked:
        fam = "moneyline-like" if (p.market_type or "") in MONEYLINE_LIKE else "derived"
        by_mt[fam].append((p.market_prob, p.status == "won"))
    for fam, v in sorted(by_mt.items(), key=lambda kv: -len(kv[1])):
        r, n = roi(v)
        lo, hi = boot_ci(v)
        ci = f"[{lo:+.1%},{hi:+.1%}]" if n >= 20 else "(n too small)"
        print(f"  {fam:16} n={n:5}  ROI {r:+7.1%}  {ci}")

    # And what the ALTERNATIVE would have picked instead, same games.
    print(f"\nSHARE OF CONTESTED GAMES where highest-edge picks a DERIVED market: "
          f"{len(by_mt.get('derived', []))}/{len(picked)} "
          f"({100*len(by_mt.get('derived', []))//max(len(picked),1)}%)")

    # ---- PAIRED comparison, which is the right test here ----
    #
    # Every rule picks from the SAME games, so comparing four independent
    # intervals throws away the pairing and is badly underpowered: each arm's own
    # CI is ~30pp wide because ROI on 5c longshots is heavy-tailed, while the
    # differences between rules are ~3pp. Resampling GAMES and taking the
    # difference within each resample cancels the shared variance -- when two
    # rules pick the same row for a game (which is common; the median contested
    # game has only 2 eligible rows) that game contributes exactly 0 to the
    # difference instead of contributing noise to both arms.
    print(f"\nPAIRED DIFFERENCE vs the current rule (resampling GAMES, {len(contested)} of them)")
    print("  This is the test that can actually decide it -- the arms share games,")
    print("  so their individual CIs overlap far more than the difference does.")
    print(f"{'rule':18}{'d(ROI)':>10}{'95% CI on the difference':>28}{'':>4}verdict")
    print("-" * 84)
    keys = list(contested.keys())
    picks = {rule: {k: pick(rule, contested[k]) for k in keys} for rule in RULES}
    rnd2 = random.Random(SEED)
    for rule in RULES:
        if rule == "highest":
            continue
        diffs = []
        for _ in range(4000):
            samp = [keys[rnd2.randrange(len(keys))] for _ in range(len(keys))]
            a = roi([(picks[rule][k].market_prob, picks[rule][k].status == "won") for k in samp])[0]
            b = roi([(picks["highest"][k].market_prob, picks["highest"][k].status == "won") for k in samp])[0]
            diffs.append(a - b)
        diffs.sort()
        lo, hi = diffs[100], diffs[-100]
        point = sum(diffs) / len(diffs)
        differs = (lo > 0) or (hi < 0)
        n_diff = sum(1 for k in keys if picks[rule][k].id != picks["highest"][k].id)
        print(f"{rule:18}{point:>+9.1%}  [{lo:>+6.1%},{hi:>+6.1%}]    "
              f"{'DIFFERENT' if differs else 'not distinguishable'}  ({n_diff} games differ)")

    print(f"\n{'='*84}")
    print("  READ THE PAIRED TABLE, NOT THE UNPAIRED ONE.")
    print()
    print("  The unpaired ROI table above is reported for context and is MISLEADING")
    print("  on its own -- measured 2026-08-15, `random` shows +10.2% vs highest-edge's")
    print("  +6.7%, which reads as a 3.5pp advantage, while the PAIRED difference on")
    print("  the same games is -5.1%. The sign flips. Two independently-noisy arms")
    print("  drawn from a heavy-tailed payout distribution will differ by several")
    print("  points from sampling alone; only the within-game difference cancels it.")
    print()
    verdicts = [k for k in RULES if k != "highest"]
    print(f"  If no rule's paired CI excludes 0, the cap's rule is NOT demonstrably")
    print(f"  costing money and must be left alone. The mechanism (a correlated family")
    print(f"  of rows, highest edge selecting the largest distortion) is real and")
    print(f"  visible; 'real mechanism' and 'measurable loss' are different claims and")
    print(f"  only the second justifies changing a rule every sport depends on.")


if __name__ == "__main__":
    main()

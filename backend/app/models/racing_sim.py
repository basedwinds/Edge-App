"""Monte-Carlo finishing-order simulator for racing markets (F1 / IndyCar /
NASCAR). Takes a field of drivers with a single strength rating (Elo-points,
already blending driver skill + constructor + starting grid -- see
racing_engine_v2.py's validated formula) and simulates the full finishing order
many times, which yields EVERY racing market type at once:

  * P(win)          -> race-winner markets (KXINDYCARRACE, KXNASCARCUPSERIES...)
  * P(top-N)        -> KXF1TOP5, KXNASCARTOP3/5/10, podium
  * P(A ahead of B) -> head-to-head driver matchups (KXNASCARH2H)

Method: Plackett-Luce. To draw one finishing order, repeatedly pick the next
finisher from the remaining field with probability proportional to its strength
weight v_i = 10**(rating_i/400) -- the rank-order generalization of the same
Bradley-Terry win prob the walk-forward backtest validated (so single-draw P(1st)
matches the closed-form win prob; the sim just extends it to full order for the
top-N / H2H markets that need it). model_validated stays False -- like every
model in this app it's proven or killed by forward CLV (paper_logger.py), which
matters doubly for racing since it can't be historically backtested (thin market
retention).

Deliberately does NOT model DNF/caution/fuel explicitly -- those are the
irreducible chaos that make NASCAR winner-hit ~18% even with a good model; the
strength spread already reflects them empirically via the walk-forward fit.
"""
import random

DEFAULT_TRIALS = 20000


def _weights(ratings: dict[str, float]) -> dict[str, float]:
    return {d: 10 ** (r / 400.0) for d, r in ratings.items()}


def simulate(ratings: dict[str, float], trials: int = DEFAULT_TRIALS,
             top_ns: tuple[int, ...] = (1, 3, 5, 10, 20), seed: int | None = None,
             attrition: float = 0.0) -> dict[str, dict]:
    """Returns {driver: {"win": p, "top3": p, "top5": p, "top10": p, "top20": p}}.
    Needs >=2 drivers with a rating; returns {} otherwise. Deterministic given
    `seed` (default: seeded from the field so displayed prices don't jitter
    between cache refreshes for an unchanged field).

    20 was ADDED 2026-08-07. KXNASCARTOP20 has been wired for ingestion in
    kalshi_racing_client for some time, but this default stopped at 10, so
    `sim[driver].get("top20")` was always None and all 36 of the Iowa Corn 350's
    top-20 markets sat unpriced while top-3/5/10 on the SAME race priced 34 of
    36. Ingested-but-unpriceable is the quiet failure mode: nothing errors, the
    rows just never produce a bet.

    A top-N line at or beyond the rated field size is DEGENERATE and is not
    emitted. With 18 rated drivers every one of them finishes top-20 in every
    trial, so the model would say 100% against a market pricing ~85% and book a
    15pp edge on all of them -- an artifact of the field being short, not a real
    disagreement. Returning no number leaves those rows unpriced instead, which
    is the same posture the field-coverage gate takes in racing_markets.py."""
    field = [d for d in ratings]
    if len(field) < 2:
        return {}
    if seed is None:
        seed = hash(tuple(sorted(field))) & 0xFFFFFFFF
    rng = random.Random(seed)
    w = _weights(ratings)

    counts = {d: {n: 0 for n in top_ns} for d in field}
    for _ in range(trials):
        # ATTRITION (added 2026-08-07). Each driver independently suffers a
        # race-ruining event with probability `attrition` and is placed BEHIND
        # everyone who finished, which is what a DNF actually does to a result.
        #
        # This module used to argue explicitly that DNFs needed no term because
        # "the strength spread already reflects them empirically via the
        # walk-forward fit". That is true for WINNING and false for finishing
        # position, and the difference is measurable: the spread was fitted on
        # P(1st) only, so nothing ever constrained the deep tail. Measured
        # walk-forward over 142 NASCAR races with real grids
        # (scripts/check_racing_topn_calibration.py), the un-attrited model said
        # 92% for top-20 where the truth was 69%, and 3% for top-10 where the
        # truth was 15% -- error growing monotonically with N, because a top-20
        # market in a 40-car field is mostly a bet on whether the car survives,
        # and survival was the one thing not simulated. Directly in the data:
        # 24.3% of NASCAR drivers starting top-5 finish in the back 40% of the
        # field (F1 11.0%).
        #
        # Default 0.0 keeps the previous behaviour exactly, so callers that have
        # not been given a fitted rate are unchanged.
        retired: list[str] = []
        if attrition > 0.0:
            retired = [d for d in field if rng.random() < attrition]
        if retired and len(retired) < len(field):
            retired_set = set(retired)
            remaining = [d for d in field if d not in retired_set]
            rem_w = {d: w[d] for d in remaining}
            # Order among the retired is not modelled -- one DNF is not
            # meaningfully "ahead" of another for any market priced here.
            rng.shuffle(retired)
            tail = retired
        else:
            remaining = list(field)
            rem_w = dict(w)
            tail = []
        pos = 0
        while remaining:
            pos += 1
            total = sum(rem_w[d] for d in remaining)
            x = rng.random() * total
            acc = 0.0
            pick = remaining[-1]
            for d in remaining:
                acc += rem_w[d]
                if x <= acc:
                    pick = d
                    break
            for n in top_ns:
                if pos <= n:
                    counts[pick][n] += 1
            remaining.remove(pick)
            del rem_w[pick]
            if pos >= max(top_ns):
                break  # nothing below the deepest top-N line is ever asked for

        # If enough of the field retired that the SURVIVORS don't fill the
        # deepest line, the remaining places are taken by retired cars -- in a
        # 40-car race where 25 drop out, positions 26-40 are DNFs, and a top-20
        # market still has to put someone in P16-20. Skipping this would leave
        # those slots uncounted and quietly deflate every top-N probability.
        for d in tail:
            if pos >= max(top_ns):
                break
            pos += 1
            for n in top_ns:
                if pos <= n:
                    counts[d][n] += 1

    out = {}
    for d in field:
        row = {}
        for n in top_ns:
            if n > 1 and n >= len(field):
                continue  # degenerate: everyone makes it, see docstring
            key = "win" if n == 1 else f"top{n}"
            row[key] = round(counts[d][n] / trials, 4)
        out[d] = row
    return out


def h2h_prob(rating_a: float, rating_b: float) -> float:
    """P(driver A finishes ahead of B), pairwise -- the closed-form Bradley-Terry
    value (no sim needed for a clean 2-way), for head-to-head matchup markets."""
    va, vb = 10 ** (rating_a / 400.0), 10 ** (rating_b / 400.0)
    return round(va / (va + vb), 4)

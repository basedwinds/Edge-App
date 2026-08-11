"""Score every model against THE MARKET on real settled outcomes.

WHY. model_observations records, for each priced market before its event, what
the model said and what the market said. Nothing read it -- the log had a writer
and no consumer, so the data accrued and never answered a question. This is the
consumer: per sport and market type, did the model's probabilities beat the
market's on the outcomes that have since settled?

THE BASELINE IS THE MARKET, deliberately, not a coin flip and not the model's own
backtest. A model can look excellent against its historical harness and still be
worse than the price you have to pay, and only the second comparison can justify
staking. Brier score (lower is better) on the same rows for both, so nothing but
the probability differs.

WHAT IT CANNOT TELL YOU. Beating the market on Brier is a claim about ACCURACY,
not about profit -- fees, the spread actually crossed and stake sizing all sit
between the two, and this app has already measured that CLV does not predict
profit. Treat a losing sport here as a reason to look, not a verdict.

READ THE `n` COLUMN FIRST. A few hundred rows over a couple of days is a hint;
the CI column says how wide the uncertainty really is, and most rows will span
zero for a long time yet.

    python scripts/score_model_observations.py
    python scripts/score_model_observations.py --by-market-type --min-n 50
"""
from __future__ import annotations

import argparse
import collections
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import SessionLocal  # noqa: E402
from app.db.models import ModelObservation  # noqa: E402

WON, LOST = "won", "lost"


def brier(prob: float, outcome: int) -> float:
    return (prob - outcome) ** 2


def summarise(rows) -> dict | None:
    """rows: (model_prob, market_prob, outcome). Paired, so the ONLY difference
    between the two scores is whose probability was used."""
    pairs = [(m, k, o) for m, k, o in rows if m is not None and k is not None]
    if not pairs:
        return None
    model = [brier(m, o) for m, k, o in pairs]
    market = [brier(k, o) for m, k, o in pairs]
    diffs = [b - a for a, b in zip(model, market)]  # >0 means the MODEL is better
    n = len(pairs)
    mean_diff = statistics.mean(diffs)
    # Paired CI: the two scores share the same events, so the pairing removes
    # most of the event-to-event noise an unpaired comparison would carry.
    if n > 1:
        se = statistics.stdev(diffs) / math.sqrt(n)
    else:
        se = float("inf")
    return {
        "n": n,
        "model_brier": statistics.mean(model),
        "market_brier": statistics.mean(market),
        "edge_vs_market": mean_diff,
        "ci_low": mean_diff - 1.96 * se,
        "ci_high": mean_diff + 1.96 * se,
        "calibration": _worst_bucket(pairs),
    }


# CALIBRATION MUST BE BUCKETED, and this is not a refinement -- the obvious
# version is worthless here. Every two-sided market is logged as BOTH sides, so
# the probabilities sum to 1 and the mean model probability is 0.500 for every
# sport by construction, as is the mean outcome. Comparing those two means
# therefore always reads "perfectly calibrated" no matter what the model does.
# The first version of this script printed exactly that and it fooled me into
# reading 0.500 as evidence the model was inert.
#
# Bucketing survives the symmetry: within the rows where the model said ~0.8,
# the outcome rate has to be ~0.8 or the model is miscalibrated, and pairing a
# confident side with its mirrored underdog side cannot hide it.
_BUCKETS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0))


def _worst_bucket(pairs) -> str:
    """The probability band where predicted and actual diverge most."""
    worst = None
    for low, high in _BUCKETS:
        rows = [(m, o) for m, k, o in pairs if low <= m < high or (high == 1.0 and m == 1.0)]
        if len(rows) < 20:  # too few to say anything
            continue
        predicted = statistics.mean(m for m, o in rows)
        actual = statistics.mean(o for m, o in rows)
        gap = abs(predicted - actual)
        if worst is None or gap > worst[0]:
            worst = (gap, low, high, predicted, actual, len(rows))
    if worst is None:
        return "(no band with n>=20)"
    _, low, high, predicted, actual, n = worst
    return f"{low:.1f}-{high:.1f}: said {predicted:.2f}, got {actual:.2f} (n={n})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-n", type=int, default=30)
    ap.add_argument("--by-market-type", action="store_true")
    args = ap.parse_args()

    session = SessionLocal()
    try:
        settled = [o for o in session.query(ModelObservation).all()
                   if o.status in (WON, LOST)]
    finally:
        session.close()

    if not settled:
        print("no settled observations yet -- nothing to score")
        return 0

    groups: dict[tuple, list] = collections.defaultdict(list)
    for o in settled:
        key = (o.sport, o.market_type) if args.by_market_type else (o.sport,)
        groups[key].append((o.model_prob, o.market_prob, 1 if o.status == WON else 0))

    scored = []
    skipped = 0
    for key, rows in groups.items():
        s = summarise(rows)
        if s is None:
            continue
        if s["n"] < args.min_n:
            skipped += 1
            continue
        s["key"] = " / ".join(str(k) for k in key)
        scored.append(s)
    scored.sort(key=lambda r: r["edge_vs_market"])

    print(f"settled observations: {len(settled)}   groups scored: {len(scored)}"
          f"   (skipped {skipped} under n={args.min_n})")
    print()
    print(f"{'group':22s} {'n':>5s} {'model':>7s} {'market':>7s} {'diff':>8s} "
          f"{'95% CI':>18s}  worst-calibrated band")
    for r in scored:
        verdict = ""
        if r["ci_low"] > 0:
            verdict = "  MODEL BEATS MARKET"
        elif r["ci_high"] < 0:
            verdict = "  <-- MARKET BEATS MODEL, look here"
        print(f"{r['key']:22s} {r['n']:5d} {r['model_brier']:7.4f} {r['market_brier']:7.4f} "
              f"{r['edge_vs_market']:+8.4f} [{r['ci_low']:+.4f},{r['ci_high']:+.4f}]"
              f"  {r['calibration']}{verdict}")
    print()
    print("diff = market Brier minus model Brier; POSITIVE means the model is more")
    print("accurate than the price. A CI spanning zero means not yet distinguishable.")
    print("the band shown is where predicted and actual diverge MOST. A persistent gap")
    print("there is a recalibration candidate; a sport whose CI sits clearly below zero")
    print("is a rebuild candidate. Means are useless here -- both sides of every market")
    print("are logged, so mean probability is 0.500 by construction. See _worst_bucket.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

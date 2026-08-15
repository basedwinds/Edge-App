"""Weekly calibration report: what the model CLAIMED vs what HAPPENED.

THE ENGINE ALREADY EXISTS AND NOBODY READS IT. `model_observations` logs every
priced market pre-event and settles it -- 42,213 rows, ~12,000 already graded,
each carrying sport, market_type, model_prob and market_prob. That is a standing
calibration dataset accruing unread. This reads it.

NEVER TRUST THE MEAN GAP. Two things make it worthless on its own:

1. IT IS STRUCTURALLY ZERO on symmetric markets. Both sides of a two-way market
   are logged, so the claimed mean is forced to 0.500 and exactly one side wins.
   tennis moneyline reads claimed 0.500 / actual 0.500 over 1,673 rows and that
   number says NOTHING about the model. Same for every series_winner and for
   3-way soccer at 0.333.

2. ERRORS CANCEL. Measured 2026-08-14 on racing: restrictor-plate races showed a
   mean gap of +-0.000 -- apparently flawless -- while carrying TEN TIMES the
   decile calibration error, because the model under-predicted at the bottom
   (claimed 0.056, delivered 0.140) and over-predicted at the top (claimed 0.544,
   delivered 0.376) in equal measure. A 17pp overstatement sat hidden behind a
   perfect-looking average for months.

So the deciles are the report and the mean is a footnote. A cell is FLAGGED on
its worst decile miss, never on its mean.

THE CONTROL ARM IS NOT OPTIONAL. Rows where model and market agree (|edge| < 2pp)
must come out near zero. If they do not, the grading or the price field is broken
and every other number here is meaningless. That control is what made the
NO-side backtest believable; it is built in rather than bolted on per
investigation.

THE TOP EDGE DECILE IS REPORTED SEPARATELY, because only the highest-edge bets
get placed. A model can be well calibrated on average and badly overconfident
exactly where it gets staked -- which is the shape of the known ~2.4x global
overstatement on settled bets.

Run: backend/.venv/Scripts/python.exe scripts/calibration_report.py
     ... --min-n 60      raise the cell threshold
"""
import sys
from collections import defaultdict
from math import sqrt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import SessionLocal  # noqa: E402
from sqlalchemy import text  # noqa: E402

MIN_CELL = 40           # rows before a sport/market_type cell is reported
MIN_BUCKET = 25         # rows before a decile is scored
CONTROL_EDGE = 0.02     # |model - market| below this is "they agree"
FLAG_MISS = 0.10        # a decile missing by more than this is flagged


def wilson(k: int, n: int) -> "tuple[float, float]":
    """95% CI for a proportion. Normal approx breaks at the extremes, which is
    exactly where the interesting deciles live."""
    if not n:
        return (0.0, 1.0)
    p = k / n
    z = 1.96
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def deciles(rows):
    """[(lo, n, claimed, actual, ci_lo, ci_hi)] for buckets with enough rows."""
    b = defaultdict(list)
    for p, y in rows:
        b[min(int(p * 10), 9)].append((p, y))
    out = []
    for k in sorted(b):
        v = b[k]
        if len(v) < MIN_BUCKET:
            continue
        claimed = sum(p for p, _ in v) / len(v)
        wins = sum(1 for _, y in v if y)
        lo, hi = wilson(wins, len(v))
        out.append((k / 10, len(v), claimed, wins / len(v), lo, hi))
    return out


def worst_miss(dec):
    """Largest |claimed - actual| whose CI EXCLUDES the claim. A decile that
    merely looks off but whose interval covers the claim is noise."""
    worst = None
    for lo, n, claimed, actual, cl, ch in dec:
        if cl <= claimed <= ch:
            continue                      # claim is inside the CI -> not evidence
        miss = abs(claimed - actual)
        if worst is None or miss > worst[0]:
            worst = (miss, lo, n, claimed, actual)
    return worst


def main() -> int:
    min_cell = MIN_CELL
    if "--min-n" in sys.argv:
        min_cell = int(sys.argv[sys.argv.index("--min-n") + 1])

    s = SessionLocal()
    rows = s.execute(text("""
        SELECT sport, market_type, model_prob, market_prob, edge, status
        FROM model_observations
        WHERE status IN ('won','lost') AND model_prob IS NOT NULL
    """)).fetchall()
    s.close()

    print(f"CALIBRATION REPORT   {len(rows)} settled observations")
    print("=" * 84)

    # ---------------- CONTROL ARM ----------------
    ctrl = [(r[2], r[5] == "won") for r in rows
            if r[4] is not None and abs(r[4]) < CONTROL_EDGE]
    print(f"\nCONTROL ARM -- rows where model and market AGREE (|edge| < {CONTROL_EDGE:.0%})")
    print("  These MUST land near zero. If they do not, grading or prices are broken")
    print("  and nothing below can be trusted.")
    cd = deciles(ctrl)
    bad_ctrl = worst_miss(cd)
    for lo, n, c, a, cl, ch in cd:
        mark = "  <-- claim outside CI" if not (cl <= c <= ch) else ""
        print(f"    p={lo:.1f}-{lo+0.1:.1f}  n={n:5d}  claimed {c:.3f}  actual {a:.3f} "
              f"[{cl:.3f},{ch:.3f}]{mark}")
    if bad_ctrl and bad_ctrl[0] > FLAG_MISS:
        print(f"  *** CONTROL FAILED: {bad_ctrl[0]:.3f} miss at p={bad_ctrl[1]:.1f}. "
              f"STOP -- fix the harness before reading anything else. ***")
    else:
        print("  control OK")

    # ---------------- PER CELL ----------------
    cells = defaultdict(list)
    for sp, mt, mp, _mk, _e, st in rows:
        cells[(sp, mt)].append((mp, st == "won"))

    print(f"\nPER SPORT x MARKET TYPE  (cells with n >= {min_cell})")
    print("  'mean gap' is a FOOTNOTE -- it is structurally 0 on symmetric markets")
    print("  and hides cancelling errors. The WORST DECILE MISS is the verdict.")
    print("-" * 84)
    print(f"{'sport':9}{'market_type':24}{'n':>6}{'mean gap':>10}   {'worst decile miss'}")
    print("-" * 84)
    flagged = []
    for (sp, mt), v in sorted(cells.items(), key=lambda kv: -len(kv[1])):
        if len(v) < min_cell:
            continue
        claimed = sum(p for p, _ in v) / len(v)
        actual = sum(1 for _, y in v if y) / len(v)
        w = worst_miss(deciles(v))
        if w is None:
            desc = "none significant"
        else:
            desc = f"{w[0]:+.3f} at p={w[1]:.1f} (n={w[2]})"
            if w[0] > FLAG_MISS:
                flagged.append((sp, mt, w, len(v)))
        print(f"{sp:9}{str(mt)[:24]:24}{len(v):>6}{claimed-actual:>+10.3f}   {desc}")

    # ---------------- TOP EDGE DECILE ----------------
    print(f"\nTOP EDGE DECILE -- the only rows that actually get staked")
    ranked = sorted([r for r in rows if r[4] is not None], key=lambda r: -abs(r[4]))
    top = ranked[:max(50, len(ranked) // 10)]
    tv = [(r[2], r[5] == "won") for r in top]
    if tv:
        c = sum(p for p, _ in tv) / len(tv)
        a = sum(1 for _, y in tv if y) / len(tv)
        lo, hi = wilson(sum(1 for _, y in tv if y), len(tv))
        print(f"  n={len(tv)}  claimed {c:.3f}  actual {a:.3f} [{lo:.3f},{hi:.3f}]  "
              f"overstatement {c/a if a else float('inf'):.2f}x")
        print("  NOT COMPARABLE TO the ~2.4x figure in project_staked_bets_have_real_edge.")
        print("  That measures EDGE overstatement on 501 rows that passed every staking")
        print("  gate; this measures PROBABILITY calibration on all priced rows in the top")
        print("  |edge| decile. Different quantity, different population -- a number near")
        print("  1.0 here does NOT retire that finding.")

    print("\n" + "=" * 84)
    if flagged:
        print(f"  {len(flagged)} cell(s) with a decile missing by more than {FLAG_MISS:.0%}:")
        for sp, mt, w, n in sorted(flagged, key=lambda f: -f[2][0]):
            print(f"    {sp}/{mt}: claimed {w[3]:.3f} delivered {w[4]:.3f} "
                  f"at p={w[1]:.1f} (n={w[2]} of {n})")
    else:
        print("  no cell has a decile missing by more than the flag threshold")
    return 1 if (bad_ctrl and bad_ctrl[0] > FLAG_MISS) else 0


if __name__ == "__main__":
    raise SystemExit(main())

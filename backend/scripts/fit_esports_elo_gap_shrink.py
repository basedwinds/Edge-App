"""Fit a per-title Elo-gap shrink for LoL and Valorant, and test it out of sample.

THE MEASUREMENT (measure_esports_elo_gap_calibration.py, 2026-08-15), harness
verified identical to production on every replayed match at lambda=1:

  LOL      4841 gated          VALORANT  9603 gated
     0-49  +0.0217  -             0-49  +0.0046  -
    50-99  +0.0074  -            50-99  -0.0283  YES
  100-149  -0.0087  -          100-149  -0.0273  YES
  150-199  -0.0319  -          150-199  -0.0676  YES
  200-299  -0.0361  YES        200-299  -0.0575  YES
     300+  -0.0525  YES           300+  -0.0862  YES

VALORANT looks like CS2: significant from 50 Elo up. LOL does NOT -- its defect is
confined to the 200+ tail (16.9% of its gated predictions) and everything below
is inside its confidence interval, with 0-49 mildly UNDER-confident.

THAT ASYMMETRY IS THE WHOLE RISK. A single global lambda helps a title whose error
is broad and can HURT one whose error is confined, by dragging a correctly
calibrated majority toward 0.5 to repair a minority. That is precisely how the
tennis global temperature failed (#192: Brier improved, ECE got worse). The
proportional shape of a gap shrink limits the damage -- a 30-point gap loses 6
points at lambda=0.8 -- but "limits" is not "eliminates", so each title has to
pass the bar on its own numbers rather than inherit CS2's verdict.

THE BAR (calibration_temp.py): ship only if BOTH ECE and Brier improve OUT OF
SAMPLE. Train on the earlier 70% by date, choose lambda on TRAIN Brier alone,
then look at the held-out 30% exactly once.

Run: backend/.venv/Scripts/python.exe scripts/fit_esports_elo_gap_shrink.py
"""
from __future__ import annotations

import sys
from math import log
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.measure_esports_elo_gap_calibration import run, BUCKETS, label  # noqa: E402

LAMBDAS = [round(0.60 + 0.02 * i, 2) for i in range(21)]   # 0.60 .. 1.00


def brier(preds, outs):
    return sum((p - o) ** 2 for p, o in zip(preds, outs)) / len(preds)


def logloss(preds, outs):
    eps = 1e-12
    return -sum(o * log(max(p, eps)) + (1 - o) * log(max(1 - p, eps))
                for p, o in zip(preds, outs)) / len(preds)


def ece(preds, outs, bins=10):
    buckets = [[] for _ in range(bins)]
    for p, o in zip(preds, outs):
        buckets[min(int(p * bins), bins - 1)].append((p, o))
    n = len(preds)
    tot = 0.0
    for b in buckets:
        if not b:
            continue
        c = sum(p for p, _ in b) / len(b)
        a = sum(o for _, o in b) / len(b)
        tot += (len(b) / n) * abs(c - a)
    return tot


def split(rows):
    dated = sorted([r for r in rows if r[3]], key=lambda r: r[3])
    cut = int(len(dated) * 0.70)
    return dated[:cut], dated[cut:]


def score(rows):
    preds = [p for _, p, _, _ in rows]
    outs = [o for _, _, o, _ in rows]
    return brier(preds, outs), ece(preds, outs), logloss(preds, outs)


def fit(title: str) -> None:
    print(f"\n{'='*78}\n{title.upper()}\n{'='*78}")
    base, _, _, _ = run(title, 1.0)
    tr0, te0 = split(base)
    b0, e0, l0 = score(tr0)
    B0, E0, L0 = score(te0)
    print(f"train {len(tr0)}  {tr0[0][3][:10]} -> {tr0[-1][3][:10]}")
    print(f"test  {len(te0)}  {te0[0][3][:10]} -> {te0[-1][3][:10]}")
    print(f"baseline lam=1.00  TRAIN brier {b0:.5f} ece {e0:.5f}  "
          f"TEST brier {B0:.5f} ece {E0:.5f} logloss {L0:.5f}")

    best, best_b, cache = None, None, {}
    print(f"\n{'lam':>6s} {'TRAIN brier':>12s} {'TRAIN ece':>10s}")
    for lam in LAMBDAS:
        rows, _, _, _ = run(title, lam, verify=False)
        cache[lam] = rows
        a, _ = split(rows)
        b, e, _ = score(a)
        print(f"{lam:6.2f} {b:12.5f} {e:10.5f}")
        if best_b is None or b < best_b:
            best, best_b = lam, b
    print(f"\nchosen on TRAIN brier alone: lam = {best:.2f}")

    _, te1 = split(cache[best])
    B1, E1, L1 = score(te1)
    def cmp(new, old):
        # lam=1.00 is a NO-OP, not a regression. Reporting identical numbers as
        # "WORSE" would read as evidence against the idea when it is really the
        # fit declining to act -- a distinction worth keeping straight, since the
        # two call for different follow-ups.
        if abs(new - old) < 1e-12:
            return "unchanged"
        return "BETTER" if new < old else "WORSE"

    print(f"\nOUT OF SAMPLE (never used to choose lam):")
    print(f"  brier    {B0:.5f} -> {B1:.5f}   {cmp(B1, B0)}")
    print(f"  ece      {E0:.5f} -> {E1:.5f}   {cmp(E1, E0)}")
    print(f"  logloss  {L0:.5f} -> {L1:.5f}   {cmp(L1, L0)}")
    if best >= 1.0:
        print(f"  VERDICT: NO SHRINK -- the fit chose lam=1.00 on its own train data.")
        print(f"           Not a rejection of the idea, a rejection of it FOR THIS TITLE.")
    elif (B1 < B0) and (E1 < E0):
        print(f"  VERDICT: SHIP lam={best:.2f} -- both ECE and Brier improve out of sample")
    else:
        print(f"  VERDICT: DO NOT SHIP -- the module rule needs BOTH")

    print(f"\nTEST-set calibration by gap, before | after:")
    print(f"{'gap':>9s} {'n':>6s} {'claimed':>9s} {'miss':>8s} | {'claimed':>9s} {'miss':>8s}")
    for lo, hi in BUCKETS:
        pre = [(p, o) for g, p, o, _ in te0 if lo <= g <= hi]
        post = [(p, o) for g, p, o, _ in te1 if lo <= g <= hi]
        if len(pre) < 25:
            continue
        c0 = sum(p for p, _ in pre) / len(pre); a0 = sum(o for _, o in pre) / len(pre)
        c1 = sum(p for p, _ in post) / len(post); a1 = sum(o for _, o in post) / len(post)
        print(f"{label((lo,hi)):>9s} {len(pre):6d} {c0:9.4f} {a0-c0:+8.4f} | {c1:9.4f} {a1-c1:+8.4f}")


def main() -> None:
    for t in ("lol", "valorant"):
        fit(t)


if __name__ == "__main__":
    main()

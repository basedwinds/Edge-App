"""Measures probability calibration (are the model's 70% predictions winning
~70%?) for the cache-based Elo models, BEFORE building any calibration layer --
same measure-first discipline as everything else in this project. Reports a
reliability table (predicted vs actual per decile) and the Expected Calibration
Error (ECE, sample-weighted mean |confidence - accuracy| across bins). A small
ECE means the model is already well-calibrated and a calibration layer would be
a near-no-op; a large ECE (or a monotone bias) is what justifies fitting one.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.derive_wnba_elo as W  # noqa: E402
import scripts.derive_cbb_elo as C  # noqa: E402
import scripts.derive_cfb_elo as F  # noqa: E402


def reliability(preds, outs, nbins=10):
    bins = [[] for _ in range(nbins)]
    for p, o in zip(preds, outs):
        idx = min(nbins - 1, int(p * nbins))
        bins[idx].append((p, o))
    rows, ece, n = [], 0.0, len(preds)
    for i, b in enumerate(bins):
        if not b:
            continue
        conf = sum(p for p, _ in b) / len(b)
        acc = sum(o for _, o in b) / len(b)
        rows.append((f"{i/nbins:.1f}-{(i+1)/nbins:.1f}", len(b), conf, acc, acc - conf))
        ece += len(b) / n * abs(acc - conf)
    return rows, ece


def report(name, preds, outs):
    rows, ece = reliability(preds, outs)
    print(f"\n=== {name}  (n={len(outs)}, base rate {sum(outs)/len(outs):.3f}) ===")
    print(f"{'bin':>10} {'n':>6} {'pred':>7} {'actual':>7} {'gap':>7}")
    for label, cnt, conf, acc, gap in rows:
        print(f"{label:>10} {cnt:>6} {conf:>7.3f} {acc:>7.3f} {gap:>+7.3f}")
    print(f"ECE = {ece:.4f}   ({'well-calibrated' if ece < 0.02 else 'meaningful miscalibration'})")
    return ece


def main():
    # WNBA
    wr = W.load(); _, wadv = W.measure_home_adv(wr)
    wp, wo = W.run(wr, 32, wadv)
    report("WNBA (K=32)", wp, wo)
    # CBB
    cr = C.load(); _, cadv = C.measure_home_adv(cr)
    cp, co = C.run(cr, 48, cadv)
    report("CBB (K=48)", cp, co)
    # CFB
    fr = F.load(); _, fadv = F.measure_home_adv(fr)
    fp, fo = F.run(fr, 56, fadv)
    report("CFB (K=56)", fp, fo)


if __name__ == "__main__":
    main()

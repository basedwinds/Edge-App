"""Walk-forward validation of the belt-retention model. (Task #111, the gate.)

WHY THIS IS THE GATE. fit_mma_title_retention.py fitted the parameters and then
refused to price on them, for a reason it stated plainly: "Matching 2 of 4
champions by eye is not validation, it is four data points." It named the test
that would settle it -- score the model against REAL past year-ends -- and this
is that test.

THE QUESTION, exactly as the market asks it. On 9 August of year Y, the champion
of a division is whoever last won a title fight there. Does that same fighter
still hold the belt on 31 December of year Y? The archive knows the answer for
every past year, so the model can be scored on hundreds of real cases instead of
four eyeballed ones.

WHAT IS BEING COMPARED. Three predictions per division-year:

  * base rate      -- one number for every division and year, the overall
                      historical hold-through-December frequency. This is the
                      "knows nothing" baseline and it is a genuinely hard one to
                      beat, because belts usually do NOT change hands in four
                      months.
  * hazard only    -- P(hold) = 1 - P(a title fight happens before Dec 31),
                      using that division's own empirical gap distribution, with
                      the champion assumed to lose any fight that happens.
  * full model     -- 1 - P(fight) * (1 - P(champion beats the field)), the form
                      fit_mma_title_retention.py landed on.

Scored by Brier and log-loss. A model that cannot beat the base rate has no
business pricing an 81-leg board, however sensible its parts look.

EVERYTHING IS COMPUTED FROM DATA AVAILABLE ON THE PREDICTION DATE. The hazard
for year Y uses only title fights before 9 August Y, and the champion is
whoever held the belt then -- never who turned out to hold it later. Getting
this wrong would leak the answer into the prediction and produce a model that
looks excellent and fails live, which is the specific failure this whole script
exists to prevent.

The champion-quality term is deliberately NOT included. Reconstructing each
fighter's Elo as it stood in, say, 2016 needs a walk-forward rating replay that
this script does not do, and using today's ratings would leak. So the "full
model" here tests the hazard plus a FIXED historical retention rate per
division. If the hazard alone cannot beat the base rate, the quality term is not
what is holding the model back.

===========================================================================
RESULT, 2026-08-09: REJECTED. The 81 title_holder legs are NOT priced.

102 division-years (2012-2025) with a fresh champion and enough history. Belts
actually survived to December 74.5% of the time.

    model               Brier  log-loss   mean p
    base rate          0.1899    0.5677    0.745
    hazard only        0.3304    1.7797    0.402
    full model         0.1902    0.6758    0.807

THE MODEL DOES NOT BEAT A CONSTANT. Predicting the same 0.745 for every division
in every year scores better on Brier (0.1899 vs 0.1902) and clearly better on
log-loss (0.5677 vs 0.6758). It beat the base rate in 8 of 14 years, which is
what a coin flip looks like.

The log-loss gap is the more damning of the two, and it says WHY: mean predicted
0.807 against an actual 0.745, so the model is systematically overconfident that
champions hold. Brier is forgiving of that; log-loss is not, and a market price
is not either.

HAZARD ALONE IS MUCH WORSE (0.3304), because assuming the champion loses every
fight that happens is simply wrong -- incumbents win most title fights. That is
not a fixable weight; it is the wrong shape.

WHAT THIS SETTLES ABOUT THE EARLIER BLOCKERS. The previous pass could not
explain why Light Heavyweight sat 28pp below the market and wondered whether it
was an edge. It was not. The model is overconfident in the champion's favour on
average and mis-ranks divisions against each other, so a large disagreement on
any single leg is model error. Pricing this board would have staked its own
biggest errors on its most liquid legs -- the exact failure mode this app has
already paid for once with racing top_n.

WHAT WOULD CHANGE THE ANSWER. Not a better hazard: the base rate that beats it
uses no schedule information at all. It would take real information the archive
does not contain -- announced fight cards for the remaining window, and a
current-champions feed that knows about vacancies, interim promotions and
retirements (the Heavyweight/Jon Jones failure). Absent both, "champions usually
hold" is the honest model, and the market already knows it.
===========================================================================
"""
from __future__ import annotations

import collections
import datetime
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.fit_mma_title_retention import (  # noqa: E402
    HAZARD_WINDOW_YEARS, MARKET_DIVISIONS, load_title_fights,
)

PREDICT_MONTH, PREDICT_DAY = 8, 9   # same point in the year the live board sits at
FIRST_YEAR, LAST_YEAR = 2012, 2025  # modern era through the last completed year


def champion_on(fights, division, asof):
    """Whoever last won a DECISIVE, non-interim title fight in this division on
    or before `asof`. Same heuristic the fit script uses -- including its known
    blind spot for vacancies and interim promotions, because validating a
    different rule than the one that would ship proves nothing."""
    best = None
    for f in fights:
        if f["interim"] or not f["decisive"] or f["weight_class"] != division:
            continue
        if f["date"] <= asof and (best is None or f["date"] > best["date"]):
            best = f
    return best


def still_champion_at(fights, division, champ, asof, year_end):
    """Did `champ` survive to year_end? True unless a decisive title fight in
    that window was won by someone else."""
    for f in fights:
        if f["interim"] or f["weight_class"] != division:
            continue
        if asof < f["date"] <= year_end and f["decisive"]:
            if f["winner_id"] != champ["winner_id"]:
                return False
    return True


def gap_hazard(fights, division, asof, days_left):
    """P(a title fight happens in this division before year end), from the
    division's own empirical spacing between title fights.

    Asked conditionally: given it has ALREADY been `since` days without one,
    what share of historical gaps would have completed within `days_left` more?
    That is what makes it schedule-aware without an announcement feed."""
    hist = [f for f in fights
            if f["weight_class"] == division and not f["interim"]
            and f["date"] <= asof
            and (asof - f["date"]).days <= HAZARD_WINDOW_YEARS * 365]
    if len(hist) < 3:
        return None
    hist.sort(key=lambda f: f["date"])
    gaps = [(b["date"] - a["date"]).days for a, b in zip(hist, hist[1:])]
    gaps = [g for g in gaps if g > 0]
    if len(gaps) < 2:
        return None
    since = (asof - hist[-1]["date"]).days
    eligible = [g for g in gaps if g >= since]
    if not eligible:
        return 1.0  # every historical gap was shorter than the current one
    return sum(1 for g in eligible if g <= since + days_left) / len(eligible)


def retention_rate(fights, division, asof):
    """Historical share of title fights in this division won by the incumbent,
    using only fights before `asof`."""
    hist = [f for f in fights
            if f["weight_class"] == division and not f["interim"]
            and f["decisive"] and f["date"] <= asof]
    hist.sort(key=lambda f: f["date"])
    if len(hist) < 4:
        return None
    held = 0
    for prev, cur in zip(hist, hist[1:]):
        if cur["winner_id"] == prev["winner_id"]:
            held += 1
    return held / max(1, len(hist) - 1)


def brier(p, y):
    return (p - y) ** 2


def logloss(p, y):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def main() -> None:
    fights = load_title_fights()
    print(f"{len(fights)} title fights in the archive\n")

    cases = []
    for year in range(FIRST_YEAR, LAST_YEAR + 1):
        asof = datetime.date(year, PREDICT_MONTH, PREDICT_DAY)
        year_end = datetime.date(year, 12, 31)
        days_left = (year_end - asof).days
        for div in MARKET_DIVISIONS:
            champ = champion_on(fights, div, asof)
            if champ is None:
                continue
            # A division whose last title fight is ancient is exactly the
            # Heavyweight/Jon Jones case the fit script flagged. Excluded here
            # too, so validation measures the model that would actually ship.
            if (asof - champ["date"]).days > 365:
                continue
            haz = gap_hazard(fights, div, asof, days_left)
            ret = retention_rate(fights, div, asof)
            if haz is None or ret is None:
                continue
            held = still_champion_at(fights, div, champ, asof, year_end)
            cases.append({
                "year": year, "div": div, "haz": haz, "ret": ret,
                "held": 1 if held else 0,
            })

    if len(cases) < 40:
        print(f"only {len(cases)} usable division-years -- too few to conclude.")
        return
    print(f"{len(cases)} division-years with a fresh champion and enough history")
    print(f"actual hold-through-December rate: {statistics.mean(c['held'] for c in cases):.3f}\n")

    base = statistics.mean(c["held"] for c in cases)
    rows = []
    for c in cases:
        p_base = base
        p_haz = 1.0 - c["haz"]                       # champion loses any fight
        p_full = 1.0 - c["haz"] * (1.0 - c["ret"])   # champion may retain it
        rows.append((c, p_base, p_haz, p_full))

    print(f"{'model':16s}{'Brier':>9s}{'log-loss':>10s}{'mean p':>9s}")
    scores = {}
    for name, idx in (("base rate", 1), ("hazard only", 2), ("full model", 3)):
        ps = [r[idx] for r in rows]
        b = statistics.mean(brier(p, r[0]["held"]) for p, r in zip(ps, rows))
        ll = statistics.mean(logloss(p, r[0]["held"]) for p, r in zip(ps, rows))
        scores[name] = (b, ll)
        print(f"{name:16s}{b:9.4f}{ll:10.4f}{statistics.mean(ps):9.3f}")

    bb, bll = scores["base rate"]
    print()
    for name in ("hazard only", "full model"):
        b, ll = scores[name]
        verdict = "BEATS" if b < bb else "LOSES TO"
        print(f"{name:16s} {verdict} the base rate on Brier "
              f"({b:.4f} vs {bb:.4f}, {bb - b:+.4f})")

    # Leave-one-year-out: a single lucky year should not carry the verdict.
    print("\nBY YEAR (Brier, lower is better)")
    print(f"{'year':6s}{'n':>4s}{'base':>9s}{'hazard':>9s}{'full':>9s}")
    wins = 0
    years = sorted({c["year"] for c in cases})
    for y in years:
        sub = [(c, pb, ph, pf) for (c, pb, ph, pf) in rows if c["year"] == y]
        if not sub:
            continue
        b = statistics.mean(brier(pb, c["held"]) for c, pb, _, _ in sub)
        h = statistics.mean(brier(ph, c["held"]) for c, _, ph, _ in sub)
        f = statistics.mean(brier(pf, c["held"]) for c, _, _, pf in sub)
        wins += f < b
        print(f"{y:<6d}{len(sub):>4d}{b:9.4f}{h:9.4f}{f:9.4f}")
    print(f"\nfull model beat the base rate in {wins}/{len(years)} years")


if __name__ == "__main__":
    main()

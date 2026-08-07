"""Does the MMA Elo know anything about a fighter with only 1-2 prior fights?

BACKGROUND. get_fight_win_prob now returns None when EITHER fighter is absent
from the rating history entirely (0 prior fights) -- that case was a pure
fabrication: two UFC debutants priced at exactly 0.500, which against Kalshi's
0.30 showed a +20.5pp edge and drew a real suggested stake. Fighters with 1-2
prior fights are still priced, on the reasoning that their rating is noisy but
at least DERIVED FROM RESULTS. This script checks whether that reasoning holds.

WHAT THIS CAN AND CANNOT MEASURE. Tennis's own 0-match cutoff was validated
against the MARKET (Elo's Brier gap vs the book by prior-match bucket). That is
not available here: ufcstats carries no odds, and no free structured historical
MMA odds source exists (Tapology returns HTTP 402; see the MMA memory). So this
measures SKILL-VS-UNINFORMED, not edge-vs-market:

    Brier(model) vs Brier(0.5) = 0.25, and accuracy vs 50%, per bucket.

That is a weaker claim, and it is stated rather than dressed up. It still
decides the question at hand, because the failure mode being guarded against is
a bucket where the model has NO skill and its "edges" are therefore noise.

THE CONFOUND THAT HAD TO BE CONTROLLED. Thin-record fighters are concentrated in
the early UFC, when the sport itself was different (huge skill gaps, open-weight
tournaments). Bucketing over all time would confound "few prior fights" with
"1990s". Every table is therefore reported all-time AND for the modern era, and
the era mix per bucket is printed so the confound is visible rather than assumed
away.

ORIENTATION. ufcstats lists the WINNER first, so fighter_a wins ~64% of decided
fights. Accuracy is computed as "did the model pick the actual winner", which
depends only on the ratings and not on row order, and the no-information
baselines are 0.25 Brier / 50% accuracy. A naive "always pick fighter_a" would
score ~64% and is an ARTIFACT of the scrape order -- it is deliberately not used
as a baseline.

Run:  PYTHONPATH=. ./.venv/Scripts/python.exe scripts/check_mma_min_fights_threshold.py
"""
from __future__ import annotations

import collections
import datetime as dt
import random
import sys

from app.ingestion import ufc_data
from app.models import mma_features
from app.models.baseline.elo_mma import MmaEloState, age_adjustment_elo, update_ratings, win_prob

MODERN_FROM = "2010-01-01"
BUCKETS = ((0, 0), (1, 2), (3, 5), (6, 10), (11, 10_000))


def _bucket_label(n: int) -> str:
    for lo, hi in BUCKETS:
        if lo <= n <= hi:
            return f"{lo}" if lo == hi else (f"{lo}-{hi}" if hi < 10_000 else f"{lo}+")
    return "?"


def _age_at(dob, when: str) -> float | None:
    if not dob:
        return None
    try:
        d = dt.date.fromisoformat(when[:10])
    except (TypeError, ValueError):
        return None
    return (d - dob).days / 365.25


def _boot_ci(pairs: list[tuple[float, int]], stat, trials: int = 2000) -> tuple[float, float]:
    """Percentile bootstrap CI for a statistic over (prob, outcome) pairs."""
    if len(pairs) < 5:
        return (float("nan"), float("nan"))
    rng = random.Random(20260806)
    n = len(pairs)
    vals = []
    for _ in range(trials):
        sample = [pairs[rng.randrange(n)] for _ in range(n)]
        vals.append(stat(sample))
    vals.sort()
    return vals[int(0.025 * trials)], vals[int(0.975 * trials)]


def _brier(pairs) -> float:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def _acc(pairs) -> float:
    # A p of exactly 0.5 is a non-pick; score it as half credit rather than
    # letting a tie-break inflate or deflate the bucket.
    return sum(1.0 if (p > 0.5) == bool(y) else (0.5 if p == 0.5 else 0.0) for p, y in pairs) / len(pairs)


def walk_forward() -> list[dict]:
    """One row per DECIDED fight, in chronological order, carrying the
    pre-fight prediction and how much history each side had at the time."""
    fights = ufc_data.load_fights()
    bios = ufc_data.load_fighter_bios()
    dobs = {fid: mma_features.parse_dob(b.get("dob")) for fid, b in bios.items()}
    fights = sorted(fights, key=lambda f: (f.get("event_date") or "", f.get("id") or ""))

    state = MmaEloState()
    seen: collections.Counter = collections.Counter()
    rows: list[dict] = []
    for f in fights:
        a, b = f.get("fighter_a_id"), f.get("fighter_b_id")
        if not a or not b:
            continue
        if f.get("is_no_contest"):
            continue  # excluded from training AND scoring in production
        when = f.get("event_date") or ""
        p_a = win_prob(
            state.get(a) + age_adjustment_elo(_age_at(dobs.get(a), when)),
            state.get(b) + age_adjustment_elo(_age_at(dobs.get(b), when)),
        )
        # Record BEFORE the counters/ratings absorb this fight -- that is what
        # makes it walk-forward rather than a fit.
        if not f.get("is_draw") and f.get("winner_id") in (a, b):
            rows.append({
                "date": when,
                "p_a": p_a,
                "y_a": 1 if f["winner_id"] == a else 0,
                "min_prior": min(seen[a], seen[b]),
                "max_prior": max(seen[a], seen[b]),
            })
        update_ratings(state, a, b, f.get("winner_id"), bool(f.get("is_draw")))
        seen[a] += 1
        seen[b] += 1
    return rows


def report(rows: list[dict], title: str) -> None:
    print(f"\n{'=' * 96}\n{title}\n{'=' * 96}")
    print(f"{'min prior':>10} {'n':>6} {'Brier':>8} {'vs .25':>9} {'95% CI':>18} "
          f"{'acc':>7} {'95% CI':>16}  {'median yr':>9}")
    by = collections.defaultdict(list)
    years = collections.defaultdict(list)
    for r in rows:
        by[_bucket_label(r["min_prior"])].append((r["p_a"], r["y_a"]))
        years[_bucket_label(r["min_prior"])].append(r["date"][:4])
    order = [f"{lo}" if lo == hi else (f"{lo}-{hi}" if hi < 10_000 else f"{lo}+") for lo, hi in BUCKETS]
    for label in order:
        pairs = by.get(label) or []
        if not pairs:
            continue
        br, ac = _brier(pairs), _acc(pairs)
        blo, bhi = _boot_ci(pairs, _brier)
        alo, ahi = _boot_ci(pairs, _acc)
        yrs = sorted(years[label])
        med = yrs[len(yrs) // 2]
        flag = "" if bhi < 0.25 else "   <-- CI touches 0.25: no proven skill"
        print(f"{label:>10} {len(pairs):>6} {br:>8.4f} {0.25 - br:>+9.4f} "
              f"[{blo:.4f},{bhi:.4f}] {ac:>7.3f} [{alo:.3f},{ahi:.3f}]  {med:>9}{flag}")


def main() -> int:
    rows = walk_forward()
    print(f"walk-forward rows (decided, non-NC fights): {len(rows)}")
    report(rows, "ALL TIME -- baseline is 0.25 Brier / 0.500 accuracy (no information)")
    modern = [r for r in rows if r["date"] >= MODERN_FROM]
    report(modern, f"MODERN ONLY ({MODERN_FROM}+) -- controls for the early-UFC era confound")

    # THE DECISIVE SPLIT. "min prior = 0" lumps two very different fights
    # together, and the production guard currently blocks both:
    #   both unrated  -> 1500 v 1500 -> p is EXACTLY 0.500 by construction, so
    #                    Brier must come out at exactly 0.25. No skill is
    #                    possible; blocking it is not a judgement call.
    #   one unrated   -> the rated side still carries real information, and the
    #                    1500 stand-in acts as "an average fighter". Whether
    #                    that beats a coin flip is an empirical question.
    print(f"\n{'=' * 96}\nMODERN, the two halves of the 'min prior = 0' bucket\n{'=' * 96}")
    print(f"{'case':>28} {'n':>6} {'Brier':>8} {'vs .25':>9} {'95% CI':>18} {'acc':>7} {'95% CI':>16}")
    for name, pred in (
        ("both unrated (0 v 0)", lambda r: r["max_prior"] == 0),
        ("one unrated, other rated", lambda r: r["min_prior"] == 0 and r["max_prior"] >= 1),
        ("one unrated, other 10+", lambda r: r["min_prior"] == 0 and r["max_prior"] >= 10),
    ):
        pairs = [(r["p_a"], r["y_a"]) for r in modern if pred(r)]
        if not pairs:
            continue
        br, ac = _brier(pairs), _acc(pairs)
        blo, bhi = _boot_ci(pairs, _brier)
        alo, ahi = _boot_ci(pairs, _acc)
        print(f"{name:>28} {len(pairs):>6} {br:>8.4f} {0.25 - br:>+9.4f} "
              f"[{blo:.4f},{bhi:.4f}] {ac:>7.3f} [{alo:.3f},{ahi:.3f}]")

    # The production question is asymmetric: a debutant facing a 40-fight
    # veteran is the shape that actually occurs on a card, so check it directly.
    print(f"\n{'=' * 96}\nMODERN, thin side vs how experienced the OPPONENT is\n{'=' * 96}")
    print(f"{'min prior':>10} {'opp prior':>11} {'n':>6} {'Brier':>8} {'acc':>7}")
    for label in ("0", "1-2", "3-5"):
        for olo, ohi, oname in ((0, 5, "0-5"), (6, 20, "6-20"), (21, 10_000, "21+")):
            pairs = [(r["p_a"], r["y_a"]) for r in modern
                     if _bucket_label(r["min_prior"]) == label and olo <= r["max_prior"] <= ohi]
            if len(pairs) >= 30:
                print(f"{label:>10} {oname:>11} {len(pairs):>6} {_brier(pairs):>8.4f} {_acc(pairs):>7.3f}")
    _market_validation_feasibility()
    return 0


def _market_validation_feasibility() -> None:
    """How far off a REAL market-based validation is.

    The house rule is to validate against market odds, never a coin flip. That
    is impossible for MMA history (no free structured odds), so the only route
    is this app's own settled bets accumulating. Printed so the gap is a
    measured number rather than an excuse.
    """
    import collections

    from app.db.database import SessionLocal
    from app.db.models import PlacedBet

    session = SessionLocal()
    try:
        bets = session.query(PlacedBet).filter(PlacedBet.sport == "mma").all()
    finally:
        session.close()
    settled = [b for b in bets if b.status in ("won", "lost")]
    ml = [b for b in settled if b.market_type == "moneyline" and b.market_prob_at_placement is not None]
    days = sorted({str(b.settled_at)[:10] for b in settled if b.settled_at})
    print(f"\n{'=' * 96}\nMARKET-BASED VALIDATION: not yet possible\n{'=' * 96}")
    print(f"  settled MMA bets: {len(settled)}  ({dict(collections.Counter(b.status for b in bets))})")
    print(f"  settled moneyline WITH a recorded market price: {len(ml)}")
    print(f"  spanning {len(days)} distinct settle days: {days[:1]} .. {days[-1:]}")
    print("  -> far too few to bucket by prior-fight count. Re-run this once the")
    print("     tracker holds a few hundred settled MMA moneylines; until then the")
    print("     tables above are skill-vs-uninformed, NOT edge-vs-market.")


if __name__ == "__main__":
    sys.exit(main())

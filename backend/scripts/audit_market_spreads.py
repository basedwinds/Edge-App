"""Which market classes can the BOOK actually pay an edge on?

THE TEST. To enter a position you cross the spread, so a book of width S costs
about S/2 against its own midpoint -- and the midpoint is what every edge in
this app is measured from. If half the spread is as large as the edge threshold
that made the bet recommendable, the class is unprofitable BY CONSTRUCTION and
no amount of model quality changes that.

    half-spread >= MIN_EDGE_TO_RECOMMEND  ->  dead, do not build a model for it
    half-spread <<  threshold             ->  the book can pay; model quality decides

WHY THIS IS WORTH A SCRIPT. It is a two-minute measurement on data already held,
and it answers "should we cover X?" BEFORE anyone writes a model. It is what
retired player season-stat props: median spread 0.200 against a 10pp threshold,
so the entry cost ate the entire edge, while team futures at 0.080 leave 6pp of
a 10pp edge intact.

IT ALSO CORRECTED A WRONG ARGUMENT. Player props were first dismissed here as
"the sharpest market class in sports". That is true of sportsbook props and
FALSE of these venues -- they are thinly traded, i.e. NEGLECTED rather than
sharp, so a decent model might well beat the prices. The reason to skip them is
that the book cannot pay, which assumes nothing about who else is pricing.

A MISSING BOOK IS NOT A WIDE ONE. Several Polymarket ingesters never store
bid/ask, so quoted-coverage is reported per class: a class with few quotes is
UNMEASURED, not cheap. Do not read its spread column.

Run: backend/.venv/Scripts/python.exe scripts/audit_market_spreads.py
"""
import collections
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func

from app.db.database import SessionLocal  # noqa: E402
from app.db.models import Market, MarketSnapshot  # noqa: E402
from app.models.staking import MIN_EDGE_TO_BET  # noqa: E402

# The bar a bet must clear to reach the recommended list. MIN_EDGE_TO_BET is the
# floor; the board is run at a stricter 10pp, which is what to judge against.
RECOMMEND_THRESHOLD = 0.10
MIN_QUOTES = 20          # below this a class is unmeasured, not cheap
MIN_COVERAGE = 0.30      # ditto: too few of its markets carry a book at all


def main() -> None:
    s = SessionLocal()
    try:
        latest = (
            s.query(MarketSnapshot.market_id, func.max(MarketSnapshot.ts).label("ts"))
            .group_by(MarketSnapshot.market_id).subquery()
        )
        rows = (
            s.query(Market.sport, Market.market_type, MarketSnapshot.yes_bid, MarketSnapshot.yes_ask)
            .join(latest, latest.c.market_id == Market.id)
            .join(MarketSnapshot, (MarketSnapshot.market_id == Market.id)
                  & (MarketSnapshot.ts == latest.c.ts))
            .filter(Market.status == "active")
            .all()
        )
    finally:
        s.close()

    by_class: dict[tuple, list] = collections.defaultdict(list)
    total: dict[tuple, int] = collections.Counter()
    for sport, mtype, bid, ask in rows:
        key = (sport or "?", mtype or "?")
        total[key] += 1
        if bid is not None and ask is not None and ask >= bid:
            by_class[key].append(ask - bid)

    print(f"active markets with a latest snapshot: {len(rows)}")
    print(f"recommend threshold: {RECOMMEND_THRESHOLD:.2f}   "
          f"(MIN_EDGE_TO_BET floor is {MIN_EDGE_TO_BET:.2f})\n")

    verdicts = []
    for key, total_n in total.items():
        spreads = by_class.get(key, [])
        cov = len(spreads) / total_n if total_n else 0.0
        if len(spreads) < MIN_QUOTES or cov < MIN_COVERAGE:
            verdicts.append((None, key, total_n, len(spreads), cov, None, "UNMEASURED (too few quotes)"))
            continue
        med = statistics.median(spreads)
        half = med / 2
        if half >= RECOMMEND_THRESHOLD:
            v = "DEAD -- entry cost eats the whole edge"
        elif half >= RECOMMEND_THRESHOLD * 0.5:
            v = "MARGINAL -- over half the edge is spread"
        else:
            v = "viable"
        verdicts.append((half, key, total_n, len(spreads), cov, med, v))

    ranked = sorted([v for v in verdicts if v[0] is not None], key=lambda r: -r[0])
    unmeasured = [v for v in verdicts if v[0] is None]

    print(f"{'sport':10s} {'market_type':24s} {'n':>6s} {'quoted':>7s} {'med sp':>7s} {'half':>6s}  verdict")
    for half, (sport, mtype), n, q, cov, med, v in ranked:
        print(f"{sport:10s} {mtype[:24]:24s} {n:6d} {q:7d} {med:7.3f} {half:6.3f}  {v}")

    print(f"\n--- UNMEASURED: fewer than {MIN_QUOTES} quotes or under "
          f"{MIN_COVERAGE:.0%} coverage ({len(unmeasured)} classes) ---")
    for _h, (sport, mtype), n, q, cov, _m, _v in sorted(unmeasured, key=lambda r: -r[2])[:20]:
        print(f"{sport:10s} {mtype[:24]:24s} {n:6d} {q:7d} quoted ({cov:.0%})")

    dead = [r for r in ranked if r[0] >= RECOMMEND_THRESHOLD]
    marg = [r for r in ranked if RECOMMEND_THRESHOLD * 0.5 <= r[0] < RECOMMEND_THRESHOLD]
    print(f"\nSUMMARY: {len(dead)} dead, {len(marg)} marginal, "
          f"{len(ranked) - len(dead) - len(marg)} viable, {len(unmeasured)} unmeasured")
    if dead:
        print("  DEAD classes (no model can profit here):")
        for _h, (sport, mtype), *_ in dead:
            print(f"    {sport} / {mtype}")


if __name__ == "__main__":
    main()

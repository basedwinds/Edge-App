"""Re-grade the tennis bets that the stale-market_type bug routed to the wrong grader.

SCOPE IS DELIBERATELY NARROW. Only bets that
  * carry market_type 'game_spread' while their market is 'set_spread', AND
  * were settled by the PER-SPORT grader (settlement_note starts 'auto-settled:')

are touched. Explicitly NOT touched:
  * 212 deliberate bookkeeping voids ('voided: ...') -- those are about the
    WAGER being booked against a phantom 0.500 or a flat ladder, not about the
    outcome, and Polymarket resolving the market fine does not undo that.
  * 1 bet the user settled by hand ('manual: ...') -- their call, not ours.
  * 55 already settled from Polymarket's own resolution -- already right.

Pass --apply to write. Default is a dry run.
"""
import datetime
import sys
from collections import Counter

from app.db.database import SessionLocal
from app.db.models import Market, PlacedBet
from app.ingestion.poller_lock import db_write_lock
from app.ingestion.polymarket_resolution import condition_id
from app.ingestion.polymarket_settlement import fetch_closed_markets, grade, stored_side

APPLY = "--apply" in sys.argv

s = SessionLocal()
rows = (s.query(PlacedBet.id, PlacedBet.status, PlacedBet.paper, PlacedBet.stake_dollars,
                PlacedBet.team, PlacedBet.settlement_note, Market.source_ticker)
        .join(Market, PlacedBet.market_id == Market.id)
        .filter(PlacedBet.market_type == "game_spread", Market.market_type == "set_spread")
        .all())
targets = [r for r in rows if (r[5] or "").startswith("auto-settled:")]
print(f"mis-typed bets: {len(rows)}   settled by the per-sport grader: {len(targets)}")

gamma = fetch_closed_markets(sorted({c for c in (condition_id(r[6]) for r in targets) if c}))
print(f"conditions resolved on polymarket: {len(gamma)}\n")

changes = []
verdicts = Counter()
for bid, status, paper, stake, team, note, ticker in targets:
    g = gamma.get(condition_id(ticker) or "")
    if g is None:
        verdicts["market not resolved -- leave as is"] += 1
        continue
    v, reason = grade(stored_side(ticker), g)
    if v is None:
        verdicts[f"ungradeable: {reason[:34]}"] += 1
        continue
    if v == status:
        verdicts["already correct"] += 1
        continue
    verdicts[f"CHANGE {status} -> {v}"] += 1
    changes.append((bid, status, v, paper, stake, team, reason, g.get("slug")))

for k, n in verdicts.most_common():
    print(f"  {k:44} {n}")

real = [c for c in changes if not c[3]]
print(f"\nWOULD CHANGE: {len(changes)}  (paper {len(changes)-len(real)}, REAL {len(real)})")
if real:
    print("\n  REAL-MONEY bets that change:")
    for bid, old, new, _p, stake, team, reason, slug in real:
        print(f"    bet {bid}: {old} -> {new}  ${stake or 0:.2f} on {team}")
        print(f"      {slug}  |  {reason}")

if not APPLY:
    print("\n(dry run -- pass --apply to write)")
    raise SystemExit

now = datetime.datetime.utcnow()
with db_write_lock():
    w = SessionLocal()
    try:
        n = 0
        for bid, old, new, _p, _stake, _team, reason, _slug in changes:
            b = w.get(PlacedBet, bid)
            if b is None or b.status != old:
                continue
            b.status = new
            b.settled_at = now
            b.settlement_note = (
                f"re-graded from Polymarket resolution ({reason}); was {old!r} from the "
                f"games-differential grader, which the stale market_type routed it to"
            )
            n += 1
        w.commit()
        print(f"\nAPPLIED: {n} bets re-graded")
    finally:
        w.close()

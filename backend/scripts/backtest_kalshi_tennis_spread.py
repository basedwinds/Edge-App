"""BACK-TEST: does _grade_tennis_game_spread agree with Kalshi's own resolution?

The Polymarket set-handicap bug was found because Polymarket publishes its
resolution. Kalshi does too, so the same check is available for the 444 Kalshi
tennis game_spread markets that legitimately keep that type -- and nothing had
ever run it.

Tests the GRADER against every FINALIZED market linked to a tennis match with a
usable score, not just the 81 bets that happen to exist. Writes nothing.
"""
from collections import Counter, defaultdict

from app.clients.base import get_json
from app.db.database import SessionLocal
from app.db.models import Market, TennisMatch
from app.models.bet_settlement import _grade_tennis_game_spread

_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
_BATCH = 100


class Stub:
    """Minimal stand-in for PlacedBet -- the grader only reads these."""
    sport = "tennis"
    market_type = "game_spread"

    def __init__(self, team, line):
        self.team, self.line = team, line


s = SessionLocal()
rows = (s.query(Market.id, Market.source_ticker, Market.team, Market.line, Market.tennis_match_id)
        .filter(Market.sport == "tennis", Market.source == "kalshi",
                Market.market_type == "game_spread", Market.source_ticker.isnot(None))
        .all())
print(f"kalshi tennis game_spread markets: {len(rows)}")

tickers = sorted({t for _i, t, _tm, _l, _m in rows if t})
result = {}
for i in range(0, len(tickers), _BATCH):
    chunk = tickers[i:i + _BATCH]
    try:
        d = get_json(f"{_URL}?tickers={','.join(chunk)}&limit={_BATCH}")
    except Exception as e:
        print("chunk failed:", e)
        continue
    for m in d.get("markets", []):
        if m.get("status") in ("finalized", "settled") and m.get("result") in ("yes", "no"):
            result[m["ticker"]] = m["result"]
print(f"finalized with a yes/no result: {len(result)}\n")

verdict = Counter()
disagree = []
by_line = defaultdict(Counter)
for mid, ticker, team, line, tmid in rows:
    truth = result.get(ticker)
    if truth is None:
        verdict["not finalized"] += 1
        continue
    match = s.get(TennisMatch, tmid) if tmid else None
    if match is None:
        verdict["no linked tennis match"] += 1
        continue
    got = _grade_tennis_game_spread(Stub(team, line), match)
    if got is None:
        verdict["grader refused (score/side/parse)"] += 1
        continue
    if got == "push":
        verdict["grader said push"] += 1
        continue
    expect = "won" if truth == "yes" else "lost"
    if got == expect:
        verdict["AGREE"] += 1
        by_line[line]["agree"] += 1
    else:
        verdict["DISAGREE"] += 1
        by_line[line]["disagree"] += 1
        if len(disagree) < 20:
            disagree.append((ticker, team, f"line={line}", f"kalshi={expect}", f"ours={got}",
                             match.player_a_name, match.player_b_name, match.score))

for k, v in verdict.most_common():
    print(f"  {k:36} {v}")
n = verdict["AGREE"] + verdict["DISAGREE"]
if n:
    print(f"\nGRADED: {n}   agreement {100*verdict['AGREE']/n:.1f}%")
print("\nby line:")
for line in sorted(by_line):
    c = by_line[line]
    t = c["agree"] + c["disagree"]
    print(f"  line {line}: n={t:4} disagree={c['disagree']:3} ({100*c['disagree']/max(t,1):.1f}%)")
if disagree:
    print("\nDISAGREEMENTS:")
    for d in disagree:
        print("  ", d)

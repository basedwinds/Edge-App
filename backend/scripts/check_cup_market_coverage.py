"""How many of Kalshi's LIVE cup markets could this app actually price? (#102.)

Same discipline as check_uefa_coverage.py, applied before building rather than
after: a cup tie is only priceable if BOTH clubs are rated, and domestic cups
reach much further down the pyramid than any league this app models. The DFB
Pokal's first round pairs Bundesliga sides with Regionalliga clubs -- Trier and
Grossaspach are in the live inventory right now and sit in the FOURTH tier, two
divisions below anything football-data publishes for Germany. Coppa Italia is
kinder (it starts at Serie A/B) but still admits Serie C entrants.

So the question this answers is not "can the bridge price a cross-tier tie" --
check_cup_tier_bridge.py already settled that -- but "how many of the 185 live
markets survive the coverage filter at all", which decides whether #102 is worth
shipping and how much of it is reachable.

Team names come from the market TITLE ("X vs Y Winner?"), resolved through the
app's own canonical_team_key so this measures what the pricing path would see.

===========================================================================
RESULT, 2026-08-08. 185 live markets, 62 distinct fixtures.

    series                   priceable  blocked    %
    KXCOPPAITALIAGAME               30       18   62%
    KXCOPPAITALIAADVANCE            20       12   62%
    KXDFBPOKALGAME                  12       33   27%
    KXDFBPOKALADVANCE                8       22   27%

    both clubs rated: 28 / 62 fixtures = 45%
      I1vI1  12   same tier, no bridge needed
      D2vD2   8   same tier, no bridge needed
      I1vI2   8   cross-tier, bridge + caution flag

TWO SEPARATE FINDINGS, and they point different ways.

1. 45% IS A FLOOR, NOT A CEILING -- A THIRD NAMING GAP. Most "blocked" clubs
are rated under a different spelling. Kalshi says Hellas Verona, football-data
says Verona; Kiel vs Holstein Kiel; Entella vs Virtus Entella; Stabia vs Juve
Stabia; Rostock vs Hansa Rostock; Munster vs Preussen Munster; Trier vs Ein
Trier; L.R. Vicenza vs Vicenza; Sudtirol Bolzano vs Sudtirol. This is a
KALSHI <-> football-data gap, distinct from the ESPN <-> football-data one that
build_soccer_espn_aliases.py already solved -- cup markets pull in Serie B and
2. Bundesliga clubs whose Kalshi spellings were never mapped, because those
tiers had never been priced against a market before.

AND IT MUST NOT BE FUZZY-MATCHED EITHER. The same probe that found those also
matched "1860 Munich" to BOTH "munich 1860" (correct) and "bayern munich"
(catastrophic), and "Union Brescia" to four clubs at once -- union berlin,
brescia, real union, philadelphia union. Union Brescia is in fact a Serie C
refoundation, so even the plausible-looking "brescia" is wrong. Third
independent demonstration today that string similarity cannot do this job.

The fix is the same fixture join, one step removed: Kalshi markets are FUTURE
so they carry no score to join on, but they do carry a date and a club pair,
and ESPN publishes the same cup fixtures with names this app can already
resolve. Anchor on the side that already resolves, require the date's ESPN
fixture list to contain exactly one match with that club, and the opponent is
determined without ever comparing two strings.

2. THE DFB POKAL IS STRUCTURALLY POOR AND WILL STAY THAT WAY. Its first round
pairs Bundesliga clubs with Regionalliga sides -- Grossaspach, Hemelingen,
Viktoria Cologne, Luneburg, St. Tonis are all third or fourth tier, two
divisions below anything football-data publishes for Germany. No naming fix
reaches them. Coverage should improve in later rounds as the minnows go out,
but early-round Pokal is not a market this app can serve. Coppa Italia is the
opposite case: it starts at Serie A/B, so 62% before any naming work.
===========================================================================
"""
from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.base import get_json  # noqa: E402
from app.ingestion.market_matcher_soccer import canonical_team_key  # noqa: E402
from app.models.baseline import elo_service_soccer  # noqa: E402

SERIES = {
    "KXCOPPAITALIAGAME": ("I1", "I2"),
    "KXCOPPAITALIAADVANCE": ("I1", "I2"),
    "KXCOPPAITALIATOTAL": ("I1", "I2"),
    "KXDFBPOKALGAME": ("D1", "D2"),
    "KXDFBPOKALADVANCE": ("D1", "D2"),
}
URL = "https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker={s}&status=open&limit=200"
PAIR = re.compile(r"^(.+?)\s+vs\.?\s+(.+?)(?:\s+Winner\?|:|$)")


def main() -> None:
    elo_service_soccer.refresh_ratings()
    states = elo_service_soccer._cache["states_by_league"]
    rated: dict[str, str] = {}
    for lg, st in states.items():
        for team in st.attack_log:
            rated.setdefault(team, lg)
    print(f"{len(rated)} rated teams across {len(states)} leagues\n")

    per_series = collections.Counter()
    fixtures: dict[str, tuple] = {}
    unrated: collections.Counter = collections.Counter()

    for series, (top, second) in SERIES.items():
        try:
            markets = get_json(URL.format(s=series)).get("markets", [])
        except Exception as exc:
            print(f"{series}: FAIL {exc}")
            continue
        for m in markets:
            title = m.get("title") or ""
            mt = PAIR.match(title)
            if not mt:
                # TOTAL markets say "Will over N goals be scored?" -- the pair
                # lives in the ticker's event segment instead.
                continue
            a, b = mt.group(1).strip(), mt.group(2).strip()
            ka, kb = canonical_team_key(a), canonical_team_key(b)
            la, lb = rated.get(ka), rated.get(kb)
            key = m.get("event_ticker") or f"{ka}|{kb}"
            fixtures[key] = (series, a, b, la, lb)
            per_series[(series, la is not None and lb is not None)] += 1
            for name, lg in ((a, la), (b, lb)):
                if lg is None:
                    unrated[name] += 1

    print(f"{'series':24s} {'priceable':>10s} {'blocked':>8s} {'%':>6s}")
    for series in SERIES:
        ok = per_series[(series, True)]
        no = per_series[(series, False)]
        tot = ok + no
        print(f"{series:24s} {ok:10d} {no:8d} {(ok/tot if tot else 0):6.0%}")

    print(f"\n--- distinct FIXTURES ({len(fixtures)}) ---")
    tiers = collections.Counter()
    for series, a, b, la, lb in fixtures.values():
        if la and lb:
            tiers[tuple(sorted((la, lb)))] += 1
    both = sum(tiers.values())
    print(f"both clubs rated: {both} / {len(fixtures)} = {both/len(fixtures):.0%}" if fixtures else "none")
    for pair, n in tiers.most_common():
        kind = "SAME TIER (no bridge needed)" if pair[0] == pair[1] else "cross-tier (bridge + caution)"
        print(f"   {pair[0]}v{pair[1]:4s} {n:3d}   {kind}")

    print("\n--- clubs blocking a fixture (below the modelled pyramid) ---")
    for name, n in unrated.most_common(20):
        print(f"   {n:3d}  {name}")


if __name__ == "__main__":
    main()

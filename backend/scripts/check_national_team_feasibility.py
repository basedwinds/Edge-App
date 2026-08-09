"""Can this app rate NATIONAL TEAMS well enough to price the live markets?

WHY THE QUESTION IS NOT "IS THERE SUPPLY". There is: 396 open Kalshi markets
across national-team series at 2026-08-09. But they split into two piles that
deserve completely different answers, and the totals hide that:

  * 336 are TOURNAMENT FUTURES expiring 2027-2031 -- the 2030 World Cup winner
    (82 markets, expiry 2031-01-01), 2030 qualifiers (76), CONCACAF Nations
    League (41), 2027 Women's World Cup (32), 2028 Euros (30 + 30 qualifiers),
    2027 Gold Cup (23), 2028 Copa America (22).
  * 60 are near-term MATCH markets, all from one competition: the ASEAN
    Championship (moneyline 12, total 24, spread 16, BTTS 4, advance 4), all
    expiring 2026-08-19.

Pricing a 2030 World Cup winner means simulating a tournament four years out,
played by squads that do not exist yet, under a format this app has no bracket
model for. That is not a modelling gap to close, it is a claim this app should
not make, so those 336 are out of scope here regardless of what the ratings can
do. This script is only about whether the 60 near-term match markets are
priceable.

WHAT IS BEING MEASURED. Whether a national-team rating pool built from FREE
data covers the teams actually listed, with enough matches each to be worth
trusting.

FRIENDLIES ARE EXCLUDED FROM TRAINING, and that is a deliberate consequence of
check_club_friendlies_signal.py's result rather than a guess. That script found
the model was WORSE than a knows-nothing baseline at predicting goals in club
friendlies (deviance gain -0.0152 against +0.2548 on competitive matches),
because rotating squads and no incentive to chase a result break the mapping
from rating to goals. International friendlies are the same event shape -- often
more so, since they exist largely to try players out. ESPN offers 460 completed
ones, which is by far the largest single source available and would have been
tempting to train on. Training ratings on matches the model demonstrably cannot
predict would corrupt the pool for the competitive fixtures that are actually
being traded.

So the pool is built only from competitive internationals: World Cup qualifiers
(AFC/UEFA/CONCACAF), the UEFA Nations League, and the AFF Championship itself.
"""
from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.base import get_json  # noqa: E402
from app.clients import kalshi_soccer_client as kalshi  # noqa: E402
from app.ingestion.market_matcher_soccer import canonical_team_key  # noqa: E402
from app.models.baseline.elo_soccer import SoccerRatingState, update_ratings  # noqa: E402

SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}"
              "/scoreboard?dates={a}-{b}&limit=500")
# COMPETITIVE internationals only -- see the module docstring on why
# fifa.friendly (460 completed matches, the biggest single source) is refused.
COMPETITIVE_SLUGS = [
    "fifa.worldq.afc", "fifa.worldq.uefa", "fifa.worldq.concacaf",
    "fifa.worldq.conmebol", "fifa.worldq.caf", "fifa.worldq.ofc",
    "uefa.nations", "aff.championship", "afc.asian.cup", "concacaf.gold",
]
WINDOWS = [("20220601", "20221231"), ("20230101", "20231231"),
           ("20240101", "20241231"), ("20250101", "20251231"),
           ("20260101", "20260831")]
MIN_MATCHES = 6  # below this a rating is noise, not information
PAIR = re.compile(r"^(?:Reg Time:\s*)?(.+?)\s+vs\.?\s+(.+?)(?:\s+Winner\?|:|$)")


def fetch_competitive():
    out, seen = [], set()
    per_slug = collections.Counter()
    for slug in COMPETITIVE_SLUGS:
        for a, b in WINDOWS:
            try:
                data = get_json(SCOREBOARD.format(slug=slug, a=a, b=b))
            except Exception:
                continue  # a slug that 400s simply contributes nothing
            for ev in data.get("events", []):
                if ev.get("id") in seen:
                    continue
                seen.add(ev.get("id"))
                try:
                    comp = ev["competitions"][0]
                    if not comp.get("status", {}).get("type", {}).get("completed"):
                        continue
                    home = away = None
                    for c in comp["competitors"]:
                        side = (c["team"]["displayName"], int(c["score"]))
                        if c["homeAway"] == "home":
                            home = side
                        else:
                            away = side
                    if not home or not away:
                        continue
                    date = str(ev.get("date"))[:10]
                except (KeyError, IndexError, ValueError, TypeError):
                    continue
                out.append((date, home[0], away[0], home[1], away[1]))
                per_slug[slug] += 1
    out.sort(key=lambda r: r[0])  # chronological: ratings must not see the future
    return out, per_slug


def main() -> None:
    matches, per_slug = fetch_competitive()
    print(f"{len(matches)} completed COMPETITIVE international matches "
          f"(friendlies deliberately excluded)")
    for slug, n in per_slug.most_common():
        print(f"    {slug:26s} {n}")
    if not matches:
        print("\nNO DATA -- stopping.")
        return

    state = SoccerRatingState()
    counts: collections.Counter = collections.Counter()
    for _d, hn, an, hg, ag in matches:
        h, a = canonical_team_key(hn), canonical_team_key(an)
        update_ratings(state, h, a, hg, ag)
        counts[h] += 1
        counts[a] += 1
    rated = {t for t, n in counts.items() if n >= MIN_MATCHES}
    print(f"\n{len(counts)} national teams seen, {len(rated)} with >= {MIN_MATCHES} matches")

    # ---- COVERAGE OF THE LIVE MARKETS -----------------------------------
    live_pairs, unresolved = [], collections.Counter()
    try:
        events = kalshi.get_open_events("KXASEANGAME")
    except Exception as exc:
        print(f"\ncould not fetch live ASEAN events: {exc}")
        events = []
    for ev in events:
        m = PAIR.match(ev.get("title") or "")
        if not m:
            continue
        hn, an = m.group(1).strip(), m.group(2).strip()
        h, a = canonical_team_key(hn), canonical_team_key(an)
        live_pairs.append((hn, an, h in rated, a in rated))
        for nm, k in ((hn, h), (an, a)):
            if k not in rated:
                unresolved[f"{nm} (n={counts.get(k, 0)})"] += 1

    both = sum(1 for _h, _a, x, y in live_pairs if x and y)
    print(f"\nLIVE ASEAN fixtures: {len(live_pairs)}, both teams rated: {both}"
          + (f" ({both/len(live_pairs):.0%})" if live_pairs else ""))
    for hn, an, x, y in live_pairs:
        print(f"    {hn:22s} vs {an:22s}  rated: {x} / {y}")
    if unresolved:
        print("\n  teams short of the threshold (match count in brackets):")
        for name, _n in unresolved.most_common():
            print(f"    {name}")

    top = sorted(((t, counts[t]) for t in rated), key=lambda x: -x[1])[:10]
    print(f"\nbest-covered teams: {[f'{t}:{n}' for t, n in top]}")


if __name__ == "__main__":
    main()

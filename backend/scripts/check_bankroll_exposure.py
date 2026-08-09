"""Does the Recommended list fit in the bankroll? (Bankroll rules revisit,
asked for after the sport count grew to twelve.)

THE QUESTION WORTH ASKING, which is not the one the Settings page answers.
Settings shows CONFIGURED allocation -- what share of the bankroll each sport
is nominally entitled to. That says nothing about whether the bets actually
being recommended, right now, could all be placed. This measures the second
thing by summing every sport's live suggested_stake_dollars and comparing it
to real remaining capacity.

Deduplicated by market id on purpose. A market can surface on both a sport's
markets route and its futures route, and double-counting allocations is a
mistake already made once in this codebase (the /settings total summed a
"total_allocation_pct" key as if it were a sport). The dupe count is printed
rather than silently dropped.

===========================================================================
RESULT, 2026-08-09. THE LIST IS ~2.5x OVERSUBSCRIBED.

  248 unique recommended bets, $2,262.50 total, against a $2,000 bankroll.
  That is 113% of the whole bankroll in suggested stakes.

  Real remaining capacity at the same moment: $900.
      TOTAL_EXPOSURE_CAP_PCT 0.60          -> $1,200 ceiling
      futures 20% $400 (nothing outstanding)
      game    40% $800 ($300 already outstanding) -> $500 left

  Worst offenders against their own nominal pool: tennis $560 vs a $128 pool
  (437%), CS2 $460 (359%), racing $170 vs $80 (212%), soccer $265 (207%).

WHY, AND IT IS NOT A BUG SO MUCH AS A DESIGN GAP. exposure.remaining_capacity
is computed from REAL PLACED bets only (paper == False, status == 'pending'),
and staking.size_stake_dollars compares each bet against it INDEPENDENTLY:

    stake = round(min(stake, remaining_capacity), 2)

Within a single response all 248 bets see the same $900 and each is only $10,
so not one of them is trimmed. The cap bites only as bets are actually placed.
Nothing anywhere bounds the AGGREGATE of what is recommended.

SECOND FINDING, arguably the bigger one. DEFAULT_STAKING_MODE is "flat" and
that is the live mode. Flat sizing is by unit tier and is documented as
"independent of the per-sport pool". So the twelve per-sport allocation
percentages -- the main bankroll control on the Settings page -- govern almost
nothing for game bets. Every stake observed here is a multiple of the $10
unit, which is the visible fingerprint of that.

What actually binds today:
    unit_dollars $10 x flat unit tiers
    TOTAL_EXPOSURE_CAP_PCT 0.60
    DEFAULT_FUTURES_PER_SPORT_CAP_FRACTION 0.25   -> $100
    DEFAULT_FUTURES_PER_TEAM_CAP_FRACTION  0.075  -> $30

WHAT NOT TO DO ABOUT IT. Do not rebalance the per-sport percentages by
measured ROI. The real tracker's per-sport confidence intervals all span zero,
so any reallocation would be fitting noise -- and in flat mode it would not
take effect anyway.
===========================================================================
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8756"

# (sport, game route, futures route). Racing has no futures route.
SPORTS = [
    ("nfl", "/markets", "/markets/futures"),
    ("nba", "/nba/markets", "/nba/futures"),
    ("wnba", "/wnba/markets", "/wnba/futures"),
    ("cfb", "/cfb/markets", "/cfb/futures"),
    ("mlb", "/mlb/markets", "/mlb/futures"),
    ("mma", "/mma/markets", "/mma/futures"),
    ("tennis", "/tennis/markets", "/tennis/futures"),
    ("soccer", "/soccer/markets", "/soccer/futures"),
    ("valorant", "/valorant/markets", "/valorant/futures"),
    ("cs2", "/cs2/markets", "/cs2/futures"),
    ("lol", "/lol/markets", "/lol/futures"),
    ("racing", "/racing/markets", None),
]

# Generous: these routes are slow on a cold cache, and a timeout here would
# read as "this sport recommends nothing", which is the wrong conclusion.
_client = httpx.Client(timeout=300.0)


class RouteFailed(Exception):
    """A route that did not answer. Raised rather than returning [] so a
    failure can never be silently counted as "this sport recommends nothing" --
    which would understate exposure, the exact direction that matters."""


def rows(path: str) -> list[dict]:
    try:
        r = _client.get(BASE + path)
    except httpx.HTTPError as exc:
        raise RouteFailed(f"{path}: {type(exc).__name__}") from exc
    if r.status_code != 200:
        raise RouteFailed(f"{path}: HTTP {r.status_code}")
    body = r.json()
    if isinstance(body, list):
        return body
    return body.get("markets") or body.get("futures") or []


def main() -> None:
    settings = _client.get(BASE + "/settings").json()
    bankroll = float(settings.get("bankroll_dollars") or 0.0)

    print(f"bankroll ${bankroll:,.0f}\n")
    print(f"{'sport':10s}{'pool $':>9s}{'bets':>7s}{'staked $':>11s}{'vs pool':>9s}{'dupes':>7s}")

    grand, total_bets, total_pool = 0.0, 0, 0.0
    failures: list[str] = []
    for sport, game_route, futures_route in SPORTS:
        seen: dict = {}
        dupes = 0
        for route in (game_route, futures_route):
            if not route:
                continue
            try:
                fetched = rows(route)
            except RouteFailed as exc:
                failures.append(str(exc))
                continue
            for row in fetched:
                stake = row.get("suggested_stake_dollars")
                if not stake:
                    continue  # only placeable bets count -- see the standing rule
                mid = row.get("id")
                if mid in seen:
                    dupes += 1
                    continue
                seen[mid] = float(stake)
        staked = sum(seen.values())
        pool = float(settings.get(f"{sport}_pool_dollars")
                     or settings.get(f"{sport}_weekly_pool_dollars") or 0.0)
        grand += staked
        total_bets += len(seen)
        total_pool += pool
        ratio = f"{staked / pool * 100:.0f}%" if pool else "-"
        print(f"{sport:10s}{pool:9.0f}{len(seen):7d}{staked:11.2f}{ratio:>9s}{dupes:7d}")

    print(f"{'TOTAL':10s}{total_pool:9.0f}{total_bets:7d}{grand:11.2f}")
    print()
    if failures:
        # Stated loudly: the total below is a FLOOR, not the figure.
        print(f"{len(failures)} route(s) did not answer -- totals are an UNDERCOUNT:")
        for f in failures:
            print(f"    {f}")
        print()
    if bankroll:
        print(f"recommended stakes are {grand / bankroll * 100:.1f}% of bankroll")

    # The number that actually matters: what could be placed right now.
    try:
        from app.db.database import SessionLocal
        from app.models import exposure

        with SessionLocal() as session:
            cap = exposure.capacity(session, bankroll,
                                    exposure.DEFAULT_FUTURES_EXPOSURE_CAP_PCT,
                                    exposure.DEFAULT_GAME_EXPOSURE_CAP_PCT)
            outstanding = exposure.outstanding_real_exposure(session)
        placeable = float(cap.get("futures", 0.0)) + float(cap.get("weekly", 0.0))
        print(f"total exposure ceiling  {exposure.TOTAL_EXPOSURE_CAP_PCT:.0%} = "
              f"${bankroll * exposure.TOTAL_EXPOSURE_CAP_PCT:,.0f}")
        print(f"outstanding real bets   {outstanding}")
        print(f"remaining capacity      ${placeable:,.2f}")
        if placeable > 0:
            print(f"\nthe list is {grand / placeable:.1f}x what can actually be held.")
    except Exception as exc:  # a live-server run without the app importable
        print(f"(capacity check skipped: {type(exc).__name__}: {exc})")


if __name__ == "__main__":
    main()

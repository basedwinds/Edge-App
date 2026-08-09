"""Does Rocket League actually have markets to bet? (User asked: "rocket league
should have been up at some point this weekend too".)

WHY THIS RUNS BEFORE ANY MODEL WORK. A rating model for a title nobody lists is
dead code with a maintenance cost. Every esports title in this app was added
because supply was confirmed FIRST. So the question is not "can we model Rocket
League" -- RLCS results are on Liquipedia exactly like CoD -- but "is there
anything to price, and is it thick enough to stake".

=========================================================================
TWO WRONG ANSWERS CAME BEFORE THE RIGHT ONE, AND BOTH FAILED THE SAME WAY:
a paginated sweep that silently returned a fraction of the catalog and was
read as an absence.

  1. Stepping the Gamma `offset` by 500 while a single call caps at 100
     events regardless of `limit` -- read 100 of every 500, skipped 80%.
  2. The obvious fix (offset += 100 over /events?closed=false) ALSO missed
     it: Gamma returns HTTP 422 past offset 2100, so that sweep silently
     truncates at 2,100 events and reports a clean zero.

Both said "no Rocket League". Rocket League was live on Polymarket at the
time, which is how the user knew before the scan did.

THE LESSON THIS FILE EXISTS TO ENCODE: a paginated absence is not evidence.
Either the endpoint has a query that answers the question directly, or a
zero has to be treated as unproven. Hence:

  * Polymarket -> /public-search?q=... , which QUERIES rather than enumerates
    and is not subject to the offset ceiling. Cross-checked against known
    titles so a zero can be told apart from a broken call.
  * Kalshi     -> /series (12,572 entries, includes DORMANT series that
    /events?status=open cannot show) and then per-series event+volume, which
    is what separates a real listing from a defined-but-unused ticker.
=========================================================================

RESULT, 2026-08-09: SUPPLY IS REAL BUT ONE-SIDED AND THIN.

  Polymarket  59 Rocket League events in the last 12 months, median volume
              $5,177, $539k total. Bursty: clustered on RLCS Majors (Paris
              Feb/Apr/May 2026) and the Esports World Cup. Exactly ONE open
              at scan time -- "Gentle Mates vs Five Fears (BO5), Esports
              World Cup", ending 2026-08-12, $60 volume.

  Kalshi      Five series EXIST -- KXROCKETLEAGUE (tournament winner),
              KXRLGAME, KXRLMAP, KXRLTOTALMAPS, KXROCKETLEAGUEGAME -- and
              they are STUBS. 24 markets across all of them, ever; $0 total
              volume; none active; every one closed around May 2026. A
              defined ticker with no trading is not supply, and this is why
              the series index alone would have given a false positive.

FOR SCALE, same method, same 12 months: Valorant's median event volume is
$170,596 and CS2's is in the millions. Rocket League is ~33x thinner than the
thinnest esports title already shipped, on one platform instead of two.

VERDICT: buildable, not urgent. The data source is solid (Liquipedia RLCS,
same shape as the CoD crawl) and the Elo template transfers, but ~59
events/year on a single platform at $5k median is a materially smaller prize
than the ~8-component build cost. Revisit when an RLCS Major populates the
board, and check Kalshi again then -- if those five series ever wake up with
real volume, the calculus changes.
"""
from __future__ import annotations

import collections
import datetime
import statistics
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_BASE = "https://gamma-api.polymarket.com"

# One year back from the scan, so "how often does this title list" is answered
# on a full competitive season rather than whatever happens to be open today.
LOOKBACK_DAYS = 365

# Each entry: search queries -> substrings a title MUST contain to count. The
# queries are broad (they find candidates), the must-list is narrow (it decides).
TITLES = {
    "Rocket League": (("rocket league", "rlcs"), ("rocket league", "rlcs")),
    # Comparators, so a thin number means something. These are the titles the
    # app already prices, i.e. the bar Rocket League has to be judged against.
    "CS2": (("counter-strike", "cs2", "blast", "iem"), ("counter-strike", "cs2")),
    "Valorant": (("valorant", "vct"), ("valorant",)),
}

ROCKET_SERIES = ("KXROCKETLEAGUE", "KXRLGAME", "KXRLMAP",
                 "KXRLTOTALMAPS", "KXROCKETLEAGUEGAME")

_client = httpx.Client(timeout=60.0, headers={"User-Agent": "nfl-edge-app/1.0"})


def gamma_search(queries: tuple[str, ...], must: tuple[str, ...]) -> list[dict]:
    """Union of /public-search results, filtered by the must-list.

    public-search is used INSTEAD of paginating /events because it is not
    subject to the offset-2100 ceiling that made the enumerating sweep report
    a false zero."""
    seen: dict = {}
    for q in queries:
        r = _client.get(f"{GAMMA_BASE}/public-search",
                        params={"q": q, "limit_per_type": 100})
        if r.status_code != 200:
            print(f"    public-search {q!r} -> HTTP {r.status_code}")
            continue
        for e in (r.json().get("events") or []):
            seen[e.get("id")] = e
    return [e for e in seen.values()
            if any(m in (e.get("title") or "").lower() for m in must)]


def kalshi_series_events(series: str) -> list[dict]:
    out, cursor = [], None
    while True:
        params = {"series_ticker": series, "limit": 200, "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        r = _client.get(f"{KALSHI_BASE}/events", params=params)
        if r.status_code != 200:
            break
        body = r.json()
        batch = body.get("events") or []
        out.extend(batch)
        cursor = body.get("cursor")
        if not batch or not cursor:
            break
    return out


def main() -> None:
    cutoff = datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)

    print("POLYMARKET (via public-search, not offset pagination)\n")
    print(f"{'title':15s}{'found':>7s}{'last12mo':>10s}{'open':>6s}"
          f"{'med vol':>11s}{'total vol':>13s}  latest end")
    for name, (queries, must) in TITLES.items():
        events = gamma_search(queries, must)
        rows = []
        for e in events:
            try:
                end = datetime.date.fromisoformat((e.get("endDate") or "")[:10])
            except ValueError:
                continue
            rows.append((end, float(e.get("volume") or 0), e.get("closed")))
        recent = [r for r in rows if r[0] >= cutoff]
        vols = [r[1] for r in recent if r[1] > 0]
        open_now = sum(1 for r in rows if r[2] is False)
        latest = max((r[0] for r in rows), default=None)
        med = statistics.median(vols) if vols else 0.0
        print(f"{name:15s}{len(events):7d}{len(recent):10d}{open_now:6d}"
              f"{med:11.0f}{sum(vols):13.0f}  {latest}")

    print("\n  (public-search caps at 100 per query, so these are LOWER BOUNDS --")
    print("   comparable across titles because every title is measured the same way.)")

    print("\nKALSHI (series index, which unlike /events?status=open shows dormant series)\n")
    print(f"{'series':22s}{'events':>7s}{'markets':>9s}{'volume':>10s}{'active':>8s}  window")
    total_vol = 0
    for series in ROCKET_SERIES:
        events = kalshi_series_events(series)
        markets = [m for e in events for m in (e.get("markets") or [])]
        vol = sum(int(m.get("volume") or 0) for m in markets)
        total_vol += vol
        active = sum(1 for m in markets if m.get("status") == "active")
        dates = sorted((m.get("close_time") or "")[:10] for m in markets if m.get("close_time"))
        window = f"{dates[0]}..{dates[-1]}" if dates else "never used"
        print(f"{series:22s}{len(events):7d}{len(markets):9d}{vol:10d}{active:8d}  {window}")

    print()
    if total_vol == 0:
        print("Kalshi Rocket League series are STUBS: defined tickers, zero volume ever.")
        print("A series that exists but has never traded is not supply.")


if __name__ == "__main__":
    main()

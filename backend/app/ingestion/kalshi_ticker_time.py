"""The real scheduled start, read out of the Kalshi ticker itself.

REAL BUG this fixes (user-reported twice on 2026-08-04). DRX vs OKSavingsBank
BRION was offered as an upcoming LoL bet at a recorded 14:30Z start while the
match had actually begun at 10:30Z. Every "has it started?" gate reasons from
`estimated_start_time`, which comes from Kalshi's `occurrence_datetime` -- a
value Kalshi sets once and never revises when a match moves.

The ticker carries the answer and nobody was reading it:

    KXLOLGAME-26AUG040630DRXBRO-BRO
                ^^^^^^^^^^^
                04 Aug 2026, 06:30 EASTERN  ->  10:30Z, the real start

The time is EASTERN, not UTC. That is why it must go through a real timezone
rather than a fixed offset: the same 06:30 ticker is 10:30Z in August and
11:30Z in January, and a hardcoded +4 would silently break every winter.

MEASURED against Flashscore's real start times before being trusted, rather
than assumed from the one case that prompted it:

    LoL (28 matches):  stored start within 15 min on 16/28
                       ticker time within 15 min on 25/28
    CS2 (3 matches):   stored 0/3, median error 180 min
                       ticker 3/3, median error 0 min

Ticker times are published for the three esports (LoL 100%, Valorant 100%,
CS2 98% of markets) and MLB (96%). NOT for tennis, NFL, NBA, soccer, racing or
MMA -- their tickers carry a date but no clock -- so this cannot help there and
those sports keep relying on their own gates.

Returns None on anything unexpected. A ticker that does not parse leaves the
caller with exactly the value it had before.
"""
from __future__ import annotations

import datetime
import re
from zoneinfo import ZoneInfo

# Kalshi publishes event times in US Eastern.
_EASTERN = ZoneInfo("America/New_York")

# KX<SERIES>-<YY><MON><DD><HHMM><teams>...  e.g. KXLOLGAME-26AUG040630DRXBRO-BRO
_TICKER = re.compile(r"^KX[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})")


def start_from_ticker(ticker: str | None) -> str | None:
    """The ticker's scheduled start as an ISO UTC string, or None.

    Only accepts a ticker carrying a real four-digit clock. Series whose tickers
    stop at the date (tennis, NFL, soccer, ...) return None rather than being
    assumed to start at midnight, which would be worse than the stale value it
    would replace.
    """
    if not ticker:
        return None
    m = _TICKER.match(ticker)
    if m is None:
        return None
    yy, mon, dd, hh, mi = m.groups()
    if int(hh) > 23 or int(mi) > 59:
        return None
    try:
        naive = datetime.datetime.strptime(f"{yy}{mon}{dd}{hh}{mi}", "%y%b%d%H%M")
    except ValueError:
        return None
    utc = naive.replace(tzinfo=_EASTERN).astimezone(datetime.timezone.utc)
    return utc.replace(tzinfo=None).isoformat() + "Z"

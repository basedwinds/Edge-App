"""Repair SoccerMatch rows whose match_date is a SCRAPE stamp rather than the
real kickoff day.

THE BUG THIS CLEANS UP AFTER. poller_soccer.py's Kalshi league path called
find_or_create_upcoming_match WITHOUT a date, even though it had the fixture's
estimated_start_time in hand on the very next line. With no date passed, the row
was stamped with "today" instead. Nothing downstream knows that stamp is a
scrape artifact: /soccer/markets treats `match_date < today` as proof the match
has already been played and drops the row, so a Kalshi-sourced fixture became
invisible the moment the date rolled over -- regardless of how far in the future
its actual kickoff was.

It rolled over earlier than a local clock suggests, which is why this presented
as an intermittent blackout rather than an obvious break: the stamp came from
local date.today() while the route compares against UTC. Measured live at 20:23
local on 2026-08-08 (local 08-08, UTC 08-09), every one of Brazil's 174,
Argentina's 88, Mexico's 49 and Japan's 36 markets was being dropped with ZERO
of them actually decided. E0/F1/I1/N1 were affected identically and only escaped
notice because they are between seasons.

Both causes are fixed at the source (the call site now passes the kickoff day,
and the remaining fallback stamps UTC). This script repairs the rows already
written, which the fix cannot reach on its own: find_or_create_upcoming_match
matches an existing fixture by name and returns it untouched, so a bad stamp
would otherwise persist for the life of the row.

WHAT IT WILL AND WILL NOT TOUCH. Only rows that are source="live" (the only ones
that ever get a scrape stamp -- a Polymarket-derived date is parsed from the
market's own question text and is real), that still have no result_ft (an
unplayed fixture, so correcting the date cannot rewrite history), and that carry
an estimated_start_time to correct it FROM. A row without a start time has no
better answer available and is left exactly as it is rather than guessed at.

Dry-run by default. Pass --apply to write.
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import SessionLocal  # noqa: E402
from app.db.models import SoccerMatch  # noqa: E402
from app.ingestion.market_catalog_soccer import _infer_season  # noqa: E402


def main() -> None:
    apply = "--apply" in sys.argv
    session = SessionLocal()
    try:
        rows = (
            session.query(SoccerMatch)
            .filter(SoccerMatch.source == "live",
                    SoccerMatch.result_ft.is_(None),
                    SoccerMatch.estimated_start_time.isnot(None))
            .all()
        )
        fixes, by_league = [], collections.Counter()
        for r in rows:
            start_day = str(r.estimated_start_time)[:10]
            if len(start_day) != 10 or start_day == r.match_date:
                continue
            fixes.append((r, start_day))
            by_league[r.league] += 1

        print(f"{len(rows)} unplayed live fixtures with a start time; "
              f"{len(fixes)} carry a match_date that disagrees with it\n")
        if by_league:
            print(f"{'league':16s}{'to fix':>7s}")
            for lg, n in by_league.most_common():
                print(f"  {str(lg):16s}{n:5d}")
        print("\nsample:")
        for r, start_day in fixes[:12]:
            print(f"  {str(r.league):14s} {r.match_date} -> {start_day}   "
                  f"{r.home_team} vs {r.away_team}")

        if not apply:
            print(f"\nDRY RUN -- nothing written. Re-run with --apply to fix {len(fixes)} rows.")
            return

        for r, start_day in fixes:
            r.match_date = start_day
            try:
                r.season = _infer_season(r.league, start_day)
            except (ValueError, TypeError):
                pass  # keep the existing season rather than crash the repair
        session.commit()
        print(f"\nAPPLIED -- {len(fixes)} fixtures re-dated to their real kickoff day.")
    finally:
        session.close()


if __name__ == "__main__":
    main()

"""Which sports trust a PLATFORM's start time with nothing checking it? (#124)

WHY THIS EXISTS. Two independent start-time bugs landed on the same day, both
user-reported, both causing already-started events to be recommended and staked:

  * RACING read ESPN's season CALENDAR endDate, a race-weekend window marker
    rather than the green flag. Every race was +3h.
  * SOCCER let the market poller overwrite ESPN's kickoff on the next cycle,
    so a correction that was computed correctly never survived. Brasileirao
    matches showed as 6pm fixtures while ~55 minutes into play.

A wrong start time is uniquely nasty here because start time is the LIVE-EVENT
GATE. Too late by a few hours and the app happily prices a match whose score is
already known -- to everyone except the model.

THE STRUCTURAL QUESTION, which is what this answers: for each sport, is the
stored start time written by a platform (Kalshi/Polymarket occurrence_datetime,
an estimate that is never revised), and if so does anything independent check
it? A sport with a platform writer and no checker is the next instance of this
bug, whether or not it happens to be wrong today.

THE EMPIRICAL CHECK runs where an independent feed is cheap: today's fixtures
are compared against ESPN's real event times and the deltas printed. A sport
that is structurally exposed but measures clean today is still exposed.
"""
from __future__ import annotations

import collections
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.base import get_json  # noqa: E402
from app.db.database import engine  # noqa: E402
from sqlalchemy import text  # noqa: E402

# Structural map, verified by reading each writer 2026-08-09.
#   writer   : what sets the stored start
#   checker  : what independently corrects it, or None
STRUCTURE = [
    ("NFL",      "nflverse schedule (gameday/gametime)", "n/a -- not platform-sourced",       "SAFE"),
    ("NBA",      "ESPN schedule (gameday/gametime)",     "n/a -- not platform-sourced",       "SAFE"),
    ("WNBA",     "ESPN schedule (gameday/gametime)",     "n/a -- not platform-sourced",       "SAFE"),
    ("MLB",      "MLB StatsAPI (gameday/gametime)",      "n/a -- not platform-sourced",       "SAFE"),
    ("CFB",      "ESPN schedule (gameday/gametime)",     "n/a -- not platform-sourced",       "SAFE"),
    ("Soccer",   "platform poller",                      "ESPN kickoffs + start_time_source", "GUARDED (fixed 2026-08-09)"),
    ("Tennis",   "platform poller",                      "tennisexplorer/flashscore + start_time_source", "GUARDED"),
    ("Racing",   "ESPN scoreboard EVENT time",           "n/a -- authoritative source",       "FIXED 2026-08-09"),
    ("Valorant", "vlr.gg scrape, via apply_start",       "vlr.gg IS the real schedule",       "SAFE"),
    ("CS2",      "Liquipedia scrape, via apply_start",   "Liquipedia IS the real schedule",   "SAFE"),
    ("LoL",      "Leaguepedia/gol.gg, via apply_start",  "scrape IS the real schedule",       "SAFE"),
    ("CoD",      "breakingpoint.gg, via apply_start",    "breakingpoint IS the real schedule", "SAFE"),
    # No independent SCHEDULE feed -- estimated_start_time is Kalshi's
    # occurrence_datetime, a pre-fight estimate it never revises. But the
    # router does not rely on it alone: mma_markets.py documents five gaps and
    # layers Kalshi/Polymarket status, staleness-behind-feed, and a structural
    # ladder-sanity check (two different round thresholds both pinned at an
    # extreme = the fight is already well underway). The start time is
    # untrustworthy; the CONSEQUENCE is defended.
    ("MMA",      "platform poller (estimated_start_time)", "no schedule feed; status + staleness + ladder-sanity", "MITIGATED"),
]

ESPN_SCOREBOARD = {
    "MLB": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "WNBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard",
    "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "CFB": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard",
    "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
}
TABLE = {"MLB": "mlb_games", "WNBA": "wnba_games", "NBA": "nba_games",
         "CFB": "cfb_games", "NFL": "nfl_games"}


def espn_starts(sport: str, day: datetime.date) -> dict[str, str]:
    """{normalized team name: ISO start} for one day."""
    try:
        data = get_json(f"{ESPN_SCOREBOARD[sport]}?dates={day:%Y%m%d}")
    except Exception as exc:  # noqa: BLE001
        print(f"   {sport}: ESPN fetch failed ({exc})")
        return {}
    out: dict[str, str] = {}
    for e in data.get("events") or []:
        comps = [c for c in (e.get("competitions") or []) if isinstance(c, dict)]
        when = (comps[0].get("date") if comps else None) or e.get("date")
        for c in comps:
            for t in c.get("competitors") or []:
                name = ((t.get("team") or {}).get("abbreviation")
                        or (t.get("team") or {}).get("displayName") or "")
                if name and when:
                    out[name.strip().lower()] = when
    return out


def main() -> None:
    today = datetime.date.today()
    print(f"START-TIME TRUST AUDIT, {today}\n")
    print(f"{'sport':10s}{'writer':40s}{'independent check':50s}verdict")
    print("-" * 130)
    for sport, writer, checker, verdict in STRUCTURE:
        print(f"{sport:10s}{writer:40s}{checker:50s}{verdict}")

    print("\n\nEMPIRICAL: stored start vs ESPN's real event time, today's fixtures\n")
    for sport in ("MLB", "WNBA", "NBA", "CFB", "NFL"):
        tbl = TABLE[sport]
        with engine.connect() as c:
            rows = c.execute(text(
                f"select home_team, away_team, gameday, gametime from {tbl} "
                f"where gameday = :d"), {"d": today.isoformat()}).fetchall()
        if not rows:
            print(f"{sport:6s} no fixtures today")
            continue
        starts = espn_starts(sport, today)
        deltas: list[int] = []
        missing = 0
        for home, away, gameday, gametime in rows:
            when = starts.get((home or "").strip().lower()) or starts.get((away or "").strip().lower())
            if not when or not gametime:
                missing += 1
                continue
            try:
                espn_dt = datetime.datetime.fromisoformat(when.replace("Z", "+00:00")).replace(tzinfo=None)
                hh, mm = str(gametime).split(":")[:2]
                day = datetime.date.fromisoformat(str(gameday))
            except (ValueError, TypeError):
                missing += 1
                continue
            # gameday/gametime do NOT share a convention across sports: CFB
            # stores both as UTC halves of one ESPN instant, while MLB's
            # gameday is officialDate (LOCAL) and gametime a UTC clock reading,
            # so a late West Coast game legitimately reads one day earlier.
            # Score against the nearest of the two candidate days -- the app
            # itself resolves this the same way (mlb_markets._game_kickoff_local
            # round-trips the offset), so a flat +/-1440 here would be an
            # artefact of this audit, not a finding.
            cands = [round((datetime.datetime.combine(day + datetime.timedelta(days=off),
                                                      datetime.time(int(hh), int(mm))) - espn_dt).total_seconds() / 60)
                     for off in (0, 1, -1)]
            deltas.append(min(cands, key=abs))
        if not deltas:
            print(f"{sport:6s} {len(rows)} fixture(s), none matchable to ESPN ({missing} unmatched)")
            continue
        hist = collections.Counter(deltas)
        big = [d for d in deltas if abs(d) > 60]
        print(f"{sport:6s} {len(rows)} fixture(s), {len(deltas)} compared, {missing} unmatched")
        print(f"       delta minutes (ours - ESPN): {dict(sorted(hist.items()))}")
        print(f"       |delta| > 60min: {len(big)}"
              + ("   <-- INVESTIGATE" if big else "   (clean)"))

    print("\nNOTE: gameday/gametime are stored in the LEAGUE's own local convention for")
    print("some sports, so a constant non-zero offset here is a timezone convention, not")
    print("a bug. What matters is a SPREAD of deltas, or a sport drifting from its own")
    print("usual offset -- that is what a stale platform timestamp looks like.")


if __name__ == "__main__":
    main()

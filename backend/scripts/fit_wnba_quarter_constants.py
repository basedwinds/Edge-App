"""Measure per-QUARTER WNBA scoring constants from ESPN linescores.

WHY. Kalshi runs twelve live WNBA quarter series (1Q-4Q winner, spread and
total) and several carry real volume -- 2Q total 53,749, 3Q total 24,241, 2Q
spread 22,563 when the flagged backlog was swept on 2026-08-11. The app already
prices HALVES off constants measured the same way (see game_lines_wnba's half
block), so this is the same measurement one level finer.

WHY NOT JUST SPLIT THE HALF CONSTANTS IN TWO. The half work already found that
the halves are NOT interchangeable -- home margin is +1.80 in the first and
-0.11 in the second, i.e. essentially the whole home-court edge lands before the
break. There is no reason to assume the four quarters divide evenly either, and
assuming it is exactly the error that block warns about. Each quarter gets its
own measured share, std and home edge.

SOURCE. ESPN's scoreboard endpoint, queried in date windows rather than one call
per game -- the same cheap path espn_client.get_first_half_scores uses, and far
cheaper than build_nba_halfline_sample.py's per-game summary calls. Games without
four quarters of linescores (in progress, postponed, stale) are skipped rather
than partially counted. Overtime is EXCLUDED from the quarter figures: a quarter
market resolves on that quarter alone, and OT belongs to none of them.

Run: backend/.venv/Scripts/python.exe scripts/fit_wnba_quarter_constants.py
"""
from __future__ import annotations

import datetime
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clients.espn_client import get_json  # noqa: E402

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
# The 2026 WNBA regular season. Windowed a week at a time because ESPN caps a
# scoreboard response -- the same truncation that silently cost the soccer
# pipeline every result after April.
START = datetime.date(2026, 5, 1)
END = datetime.date(2026, 8, 11)
WINDOW_DAYS = 7


def fetch_quarter_games() -> list[dict]:
    """[{home, away, q: [(h,a) x4], home_final, away_final}] for completed games."""
    out, seen = [], set()
    day = START
    while day <= END:
        stop = min(day + datetime.timedelta(days=WINDOW_DAYS - 1), END)
        url = f"{SCOREBOARD}?dates={day:%Y%m%d}-{stop:%Y%m%d}&limit=300"
        try:
            data = get_json(url)
        except Exception as exc:  # noqa: BLE001
            print(f"  window {day} .. {stop}: FETCH FAILED ({exc})")
            day = stop + datetime.timedelta(days=1)
            continue
        for event in (data or {}).get("events", []):
            comps = event.get("competitions") or []
            if not comps:
                continue
            comp = comps[0]
            if not ((comp.get("status") or {}).get("type") or {}).get("completed"):
                continue
            eid = event.get("id")
            if eid in seen:
                continue
            sides = {}
            for c in comp.get("competitors", []):
                lines = c.get("linescores") or []
                if len(lines) < 4:
                    sides = {}
                    break
                try:
                    qs = [int(float(lines[i].get("value") or 0)) for i in range(4)]
                    final = int(c.get("score"))
                except (TypeError, ValueError):
                    sides = {}
                    break
                sides[c.get("homeAway")] = ((c.get("team") or {}).get("abbreviation"), qs, final)
            if "home" in sides and "away" in sides:
                seen.add(eid)
                h, a = sides["home"], sides["away"]
                out.append({"home": h[0], "away": a[0], "hq": h[1], "aq": a[1],
                            "home_final": h[2], "away_final": a[2]})
        day = stop + datetime.timedelta(days=1)
    return out


def main() -> None:
    games = fetch_quarter_games()
    print(f"completed WNBA games with four quarters of linescores: {len(games)}")
    if len(games) < 50:
        print("SAMPLE TOO SMALL to fit per-quarter constants; not reporting numbers.")
        return

    reg_totals = [sum(g["hq"]) + sum(g["aq"]) for g in games]
    print(f"\nregulation total: mean {statistics.mean(reg_totals):.2f} "
          f"std {statistics.pstdev(reg_totals):.2f}")
    print(f"{'quarter':9s} {'margin mean':>12s} {'margin std':>11s} "
          f"{'total mean':>11s} {'total std':>10s} {'share':>7s}")
    shares = {}
    for q in range(4):
        margins = [g["hq"][q] - g["aq"][q] for g in games]
        totals = [g["hq"][q] + g["aq"][q] for g in games]
        share = statistics.mean(totals) / statistics.mean(reg_totals)
        shares[q + 1] = share
        print(f"Q{q+1:<8d} {statistics.mean(margins):+12.2f} {statistics.pstdev(margins):11.2f} "
              f"{statistics.mean(totals):11.2f} {statistics.pstdev(totals):10.2f} {share:7.4f}")
    # These shares are of REGULATION, and regulation is the sum of the four
    # quarters, so they sum to 1.0000 BY CONSTRUCTION -- that is arithmetic, not
    # evidence. Stated explicitly because the half constants' shares sum to
    # 0.9935 and the gap there IS meaningful (it is overtime); reading these the
    # same way would be reading a tautology as a finding.
    final_totals = [g["home_final"] + g["away_final"] for g in games]
    ot_share = 1.0 - statistics.mean(reg_totals) / statistics.mean(final_totals)
    print(f"\nshares sum to {sum(shares.values()):.4f} -- of REGULATION, which is the sum "
          f"of the four quarters, so this is arithmetic rather than a check.")
    print(f"overtime is {ot_share:.2%} of all points scored, and belongs to no quarter; "
          f"a quarter market must be modelled on regulation scoring only.")

    # HOME EDGE BY QUARTER is the number that decides whether a quarter market
    # can reuse the game model at all. The half work found the home edge is
    # almost entirely first-half; if that concentrates further into Q1, then Q2-Q4
    # must NOT carry it.
    full_margin = statistics.mean([g["home_final"] - g["away_final"] for g in games])
    print(f"\nfull-game home margin {full_margin:+.2f}; per-quarter share of it:")
    for q in range(4):
        m = statistics.mean([g["hq"][q] - g["aq"][q] for g in games])
        print(f"   Q{q+1}: {m:+.2f}  ({m / full_margin:+.1%} of the full-game edge)")


if __name__ == "__main__":
    main()

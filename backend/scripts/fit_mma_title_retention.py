"""Fit the inputs a UFC belt-retention model needs, and check them before
anything is simulated. (Task #111.)

WHAT THE MARKET ASKS. Kalshi's 81 title_holder legs ask "who holds the
{weight} belt on Dec 31?", one leg per candidate. Answering needs three things:

    1. WHO HOLDS EACH BELT NOW
    2. HOW OFTEN a division stages a title fight       <- the hazard rate
    3. HOW OFTEN the champion loses one                <- retention base rate

WHY THE HAZARD RATE IS LOAD-BEARING, NOT A REFINEMENT. Only THREE title fights
are announced before Dec 31 (Makhachev-Garry, Van-Pantoja, Dern-Robertson),
covering two of the eight divisions with markets. A model that chains only
announced fights would conclude, for the other six, that the champion faces
nobody and therefore holds the belt with probability 1.0 -- a confident,
obviously false answer on three quarters of the board. The unannounced fights
are most of what the market is pricing, so the hazard rate IS the model.

WHY THIS RUNS BEFORE ANY SIMULATION. An earlier pass called this whole model a
no-go after reading "4 title bouts" off the mma_fights table -- which holds only
133 current/upcoming rows, not the archive. data/ufc_fight_cache.json has 17,560
per-fighter rows covering ~8,780 fights, 480 of them title fights. The lesson is
to check the source, so this script prints its inputs for inspection rather than
feeding them straight into a Monte Carlo. A WRONG CHAMPION POISONS ITS ENTIRE
DIVISION, so that list gets eyeballed against reality before it is trusted.

===========================================================================
RESULT, 2026-08-08. Parameters fit cleanly from 480 title fights. The two
sanity checks below then FAILED, so nothing is priced off this yet.

    division              champion         haz/yr  retain   P(hold Dec31)  market
    Light Heavyweight     Carlos Ulberg      2.00     47%          54.6%    0.89
    Lightweight           Justin Gaethje     1.50     64%          76.4%    0.805
    Welterweight          Islam Makhachev    1.62     58%          70.6%    0.71
    Middleweight          Sean Strickland    1.75     54%          65.0%    0.63
    Heavyweight           Jon Jones          0.88     50%          78.6%     n/a
    Flyweight             Joshua Van         1.62     50%          63.9%       -

SECOND PASS, same day: both failures were addressed and the model IMPROVED but
is STILL NOT SAFE TO PRICE. Recorded here so the next attempt starts from the
evidence rather than repeating it.

FIX A (hazard) WORKED. Replacing the flat base rate with an EMPIRICAL GAP
distribution -- for each division, the historical spacing between consecutive
title fights, asked "would a gap this long have completed by Dec 31 given how
long it has already been?" -- makes the hazard schedule-aware without needing an
announcement feed. Lightweight drops to 18% (Gaethje fought 55 days ago, so
another fight before December is unlikely), which is exactly the behaviour the
flat rate could not produce.

FIX B (fighter quality) WORKED. Retention was a DIVISION average, so every
champion was equally likely to lose. Using elo_service_mma for the champion
against the average of that division's recent title fighters makes it specific:
Makhachev 0.724, Ulberg 0.579, Strickland 0.550, Gaethje 0.448.

COMBINED as P(hold) = 1 - P(fight) * (1 - P(champ beats field), the model now
tracks the market on half the board:

    division           model   market
    Welterweight       0.724   0.71    <- close
    Middleweight       0.653   0.63    <- close
    Lightweight        0.901   0.805
    Light Heavyweight  0.609   0.89    <- 28pp off, unexplained

STILL BLOCKING:
  * LIGHT HEAVYWEIGHT is 28pp below the market and nothing here explains it. LHW
    has the division's worst historical retention (47%) and a short gap since
    Ulberg's win, so the model sees a likely fight he is only 58% to win. The
    market disagrees strongly. Until that is understood it is a model error, not
    an edge -- and it would be the single largest recommendation on the board.
  * NO WALK-FORWARD VALIDATION. Matching 2 of 4 champions by eye is not
    validation, it is four data points. The archive supports scoring this against
    real past year-ends, and that is the gate.

FIX C (champion identity) DID NOT WORK. Including interim titles was tried and
returns Ciryl Gane for Heavyweight, not Tom Aspinall -- so neither the real-only
nor the interim-inclusive heuristic finds the actual champion. Vacations and
promotions are not recoverable from fight results. A staleness guard (refuse a
division whose last title fight is over ~12 months old) is the honest fallback,
which would exclude Heavyweight entirely rather than price it wrong.

FAILURE 1 -- A STALE CHAMPION. Four of the identified champions match the
market's favourite exactly (Ulberg, Gaethje, Makhachev, Strickland), which is
strong evidence the heuristic works in the normal case. HEAVYWEIGHT DOES NOT:
this says Jon Jones, whose last title win was 630 days ago, while the market's
favourite is Tom Aspinall at 0.57. That is a vacated belt promoted from interim,
and "most recent title-fight winner" cannot see vacancies, retirements or
interim elevations. A wrong champion poisons its entire division.

FAILURE 2 -- THE HAZARD RATE IGNORES THE SCHEDULE, and that is not a rounding
error. Light Heavyweight prices P(Ulberg holds) at 54.6% against a market of
0.89 -- a 34-point gap that is model error, not edge. The base rate says LHW
stages 2.0 title fights a year, so 4.8 months implies 0.79 expected fights; but
Ulberg has NO announced fight, so his real exposure to Dec 31 is far lower. The
market knows the calendar and the base rate does not. Pricing on this would
manufacture the largest fake edges on precisely the most liquid legs -- the
"staking your own biggest errors" failure this app has already paid for once.

WHAT WOULD FIX EACH:
  1. Champion identification needs a current-champions source (UFC rankings page
     is free) rather than inference, or at minimum a staleness guard that refuses
     a division whose last title fight is over ~12 months old.
  2. The hazard must be CONDITIONED ON THE SCHEDULE: announced fights are
     certain, and the unannounced hazard should apply only to the remaining
     window, with a shorter lead time than a full base rate implies (a title
     fight not yet announced in August is unlikely to happen and be lost before
     December). Validate walk-forward against past year-ends before pricing.

TWO REAL COMPLICATIONS, handled explicitly rather than silently:
  * INTERIM TITLES. "UFC Interim Heavyweight" fights are title bouts but their
    winner is not the champion. Counted separately, never treated as the belt.
  * THE TOURNAMENT ERA. The earliest rows are "UFC 1 Tournament" -- one-night
    tournaments from 1993, not title fights in any modern sense. A hazard rate
    fitted across that era would describe a sport that no longer exists, so the
    rate is fitted on a recent window only.
"""
from __future__ import annotations

import collections
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DATA = Path(__file__).resolve().parents[2] / "data" / "ufc_fight_cache.json"

# Divisions Kalshi actually lists title markets for (see kalshi_mma_client
# .TITLE_SERIES). Women's divisions and interim belts are reported but not
# modelled, because there is no market for them.
MARKET_DIVISIONS = [
    "UFC Bantamweight", "UFC Featherweight", "UFC Flyweight", "UFC Heavyweight",
    "UFC Light Heavyweight", "UFC Lightweight", "UFC Middleweight", "UFC Welterweight",
]
# Hazard fitted on recent history only -- see module docstring on the tournament era.
HAZARD_WINDOW_YEARS = 8


def parse_date(s):
    for fmt in ("%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(str(s).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def load_title_fights():
    """The cache is PER FIGHTER (two rows per fight), so rows are regrouped into
    fights keyed on (event_date, weight_class, fight_url)."""
    rows = json.loads(DATA.read_text(encoding="utf-8"))
    rows = list(rows.values()) if isinstance(rows, dict) else rows
    by_fight = collections.defaultdict(list)
    for r in rows:
        if not r.get("is_title_bout"):
            continue
        key = (r.get("fight_url") or "", r.get("event_date"), r.get("weight_class"))
        by_fight[key].append(r)

    fights = []
    for (_url, date_s, wc), sides in by_fight.items():
        d = parse_date(date_s)
        if d is None or len(sides) != 2:
            continue  # a half-scraped fight tells us nothing; skip rather than guess
        # ufcstats uses W/L/D/NC, not "win"/"loss". D and NC leave the belt where
        # it was, which is why `decisive` gates the champion chain below.
        winner = next((s for s in sides if str(s.get("result", "")).strip().upper() == "W"), None)
        loser = next((s for s in sides if s is not winner), None)
        fights.append({
            "date": d,
            "weight_class": wc or "",
            "interim": "Interim" in (wc or ""),
            "winner_id": (winner or {}).get("fighter_id"),
            "winner_name": (winner or {}).get("fighter_name"),
            "loser_name": (loser or {}).get("fighter_name"),
            "decisive": winner is not None,  # draws/NCs leave the belt where it was
        })
    fights.sort(key=lambda f: f["date"])
    return fights


def main() -> None:
    fights = load_title_fights()
    today = datetime.date.today()
    print(f"{len(fights)} title fights reconstructed, "
          f"{fights[0]['date']} -> {fights[-1]['date']}")
    interim = [f for f in fights if f["interim"]]
    print(f"  {len(interim)} interim-title fights (excluded from champion tracking)\n")

    # ---- 1. CURRENT CHAMPION ------------------------------------------------
    print("=== CURRENT CHAMPION per division (most recent decisive, non-interim) ===")
    print(f"{'division':26s} {'champion':24s} {'won on':>12s} {'days ago':>9s}")
    champions = {}
    for wc in MARKET_DIVISIONS:
        hist = [f for f in fights if f["weight_class"] == wc and not f["interim"] and f["decisive"]]
        if not hist:
            print(f"{wc:26s} {'-- none found --':24s}")
            continue
        last = hist[-1]
        champions[wc] = last
        print(f"{wc:26s} {str(last['winner_name'])[:24]:24s} "
              f"{str(last['date']):>12s} {(today - last['date']).days:>9d}")

    # ---- 2. HAZARD RATE -----------------------------------------------------
    cutoff = today - datetime.timedelta(days=365 * HAZARD_WINDOW_YEARS)
    print(f"\n=== TITLE-FIGHT HAZARD, last {HAZARD_WINDOW_YEARS} years (from {cutoff}) ===")
    print(f"{'division':26s} {'fights':>7s} {'per year':>9s} {'champ retained':>15s}")
    hazards = {}
    for wc in MARKET_DIVISIONS:
        recent = [f for f in fights if f["weight_class"] == wc and not f["interim"]
                  and f["date"] >= cutoff and f["decisive"]]
        if not recent:
            print(f"{wc:26s} {0:>7d}")
            continue
        per_year = len(recent) / HAZARD_WINDOW_YEARS
        # Did the belt stay put? The champion going in is the previous fight's winner.
        held = 0
        prev = None
        for f in recent:
            if prev is not None and f["winner_id"] == prev:
                held += 1
            prev = f["winner_id"]
        rate = held / max(1, len(recent) - 1)
        hazards[wc] = (per_year, rate)
        print(f"{wc:26s} {len(recent):>7d} {per_year:>9.2f} {rate:>14.0%}")

    # ---- 3. WHAT THIS IMPLIES FOR DEC 31 ------------------------------------
    dec31 = datetime.date(today.year, 12, 31)
    months = max(0.0, (dec31 - today).days / 30.44)
    print(f"\n=== IMPLIED EXPOSURE to Dec 31 ({months:.1f} months away) ===")
    print(f"{'division':26s} {'exp. fights':>12s} {'P(champ still holds)':>22s}")
    for wc in MARKET_DIVISIONS:
        if wc not in hazards:
            continue
        per_year, retain = hazards[wc]
        exp = per_year * months / 12.0
        # Each fight is an independent chance to lose the belt at the fitted rate.
        p_hold = retain ** exp
        print(f"{wc:26s} {exp:>12.2f} {p_hold:>21.1%}")

    print("\nSANITY CHECKS BEFORE ANY OF THIS IS USED:")
    print("  * champion list above must match reality -- a wrong champion poisons its division")
    print("  * P(champ still holds) should sit BELOW the market's price for that champion,")
    print("    since the market also prices the champion winning any fights that happen")
    print("  * a division with <5 recent title fights has a hazard estimate too thin to trust")


if __name__ == "__main__":
    main()

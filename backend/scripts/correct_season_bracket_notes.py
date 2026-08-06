"""Correct the season/bracket backlog notes -- they claimed more readiness than exists.

The 2026-08-06 triage labelled 10 season/bracket entries "READY -- this sport's
season sim already prices this kind of question. Wiring job, no new model."
Checking each against the actual code before building showed that is wrong for
every one of them, in four distinct ways. Recording what is really true, because
a backlog that overstates readiness is worse than one that says nothing -- it
sends you off to do a "quick wiring" that is a build.

WHAT WAS ACTUALLY VERIFIED (each by running the model, not by reading it):

  * racing_championship._compute("irl") -> 33 drivers, 6 races left, a real
    distribution. The MODEL is ready.
  * racing_championship._compute("nascar") -> 0 drivers, because
    fetch_driver_standings("nascar") itself returns 0. Blocked upstream of the
    model.
  * season_sim_wnba emits one_seed and playoff ONLY -- both regular-season
    finishes. There is no playoff bracket anywhere for WNBA, so championship /
    finals / semifinals have no model behind them at all.
  * season_sim (NFL) DOES simulate a full bracket, but run_simulation returns
    division_pct / playoff_pct / one_seed_pct / conf_champ_pct / sb_champ_pct.
    Neither "finishes as seed N" nor "hosts a playoff game" is among them.
  * NONE of the Polymarket championship/postseason events are ingested: 0 market
    rows for IndyCar champion, NASCAR champion, WNBA champion, WNBA postseason.
    Even where the model is ready, the market is not there to price.

Pass --apply to write. Default is a dry run.
"""
import sys

from app.db.database import SessionLocal
from app.db.models import CatalogEntry
from app.ingestion.poller_lock import db_write_lock

APPLY = "--apply" in sys.argv

_IRL = ("WAITING — the championship model IS ready for IndyCar (verified: 33 drivers, "
        "6 races left). Blocker is ingestion: this Polymarket event has 0 market rows.")
_NASCAR = ("WAITING — championship model returns 0 rated drivers for NASCAR because "
           "fetch_driver_standings('nascar') itself returns nothing. Needs a standings "
           "source before the model can run at all.")
_WNBA_BRACKET = ("WAITING — no WNBA playoff bracket model exists. The season sim stops at the "
                 "regular season (one_seed + playoff only), so a championship/finals/semis "
                 "price has nothing behind it. Needs a bracket sim, not wiring.")
_WNBA_POST = ("WAITING — the model IS ready (season sim's playoff probability; the Kalshi twin "
              "KXWNBAPLAYOFF is already live). Blocker is ingestion: 0 market rows for this "
              "Polymarket event.")
_NFL_SEED = ("WAITING — the NFL season sim does simulate the full bracket, but returns only "
             "division/playoff/one_seed/conf_champ/sb_champ. 'Finishes as seed N' and 'hosts a "
             "playoff game' are not emitted yet — a new sim output, not wiring.")
_WNBA_OT = ("WAITING — mis-filed by the auto-triage: this is a GAME prop (does the game go to "
            "overtime), not a season outcome. Needs an OT-rate model, which no sport has here.")

PLAN = {
    "ntt-indycar-series-2026-champion": _IRL,
    "nascar-cup-series-2026-champion": _NASCAR,
    "KXWNBA": _WNBA_BRACKET,
    "KXWNBAFINAL": _WNBA_BRACKET,
    "KXWNBASEMIFINAL": _WNBA_BRACKET,
    "wnba-2026-champion": _WNBA_BRACKET,
    "wnba-team-to-make-postseason": _WNBA_POST,
    "KXNFLSEED": _NFL_SEED,
    "KXNFLPLAYOFFHOST": _NFL_SEED,
    "KXWNBAOT": _WNBA_OT,
}

s = SessionLocal()
rows = s.query(CatalogEntry).filter(CatalogEntry.disposition == "flagged").all()
# LONGEST prefix wins. Kalshi series names nest -- KXWNBAOT starts with KXWNBA --
# so first-match order silently gave the Overtime prop the championship-bracket
# note. Same nesting trap that made "WNBA Championship" look already-built
# earlier today; sorting by length is the fix in both places.
_ORDERED = sorted(PLAN.items(), key=lambda kv: -len(kv[0]))
matched = []
for e in rows:
    for prefix, note in _ORDERED:
        if e.identifier.startswith(prefix):
            matched.append((e, note))
            break

print(f"flagged entries: {len(rows)}   matched by this correction: {len(matched)}")
still_ready = [e for e in rows if (e.note or "").startswith("READY")
               and e.id not in {m[0].id for m in matched}]
print(f"entries left claiming READY after this: {len(still_ready)}")
for e in still_ready[:12]:
    print(f"    {e.identifier[:34]:36} {e.title[:40]}")
print()
for e, note in matched[:10]:
    print(f"  {e.identifier[:34]:36} -> {note[:74]}...")

if not APPLY:
    print("\n(dry run -- pass --apply to write)")
    raise SystemExit

with db_write_lock():
    w = SessionLocal()
    try:
        for e, note in matched:
            w.get(CatalogEntry, e.id).note = note
        w.commit()
        print(f"\nAPPLIED: corrected {len(matched)} notes")
    finally:
        w.close()

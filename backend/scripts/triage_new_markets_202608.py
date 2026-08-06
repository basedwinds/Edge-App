"""Work the New Markets backlog to zero, with a written reason on every entry.

Run 2026-08-06 against the 29 undecided catalog entries. Every decision below
was checked against live data before being written down -- the point of the note
field is that a future scan re-surfacing the same series inherits the reasoning
instead of re-deriving it.

WHAT WAS MEASURED (so the notes are claims, not guesses):

  * football-data.co.uk 2025/26: B1 Belgium 311 rows and N1 Eredivisie 306 rows,
    both carrying all six columns the existing 5 leagues need. I1 Serie A 380
    and I2 Serie B 380 -- but I3 (Serie C) does not exist there at all.
  * ESPN soccer: jpn.1 returns real completed J-League fixtures (10 in June
    2026); ita.coppa_italia returns real fixtures too (16 upcoming).
  * Kalshi quotes on the new soccer series, sampled per market via the
    single-market endpoint (the LIST endpoint returns null for every quote
    field, which is why a first pass looked like "no market at all"):
        Belgium Total        median spread 0.06, ask depth to 699, volume 0
        Belgium BTTS         spread 0.06, depth 2,                volume 0
        Eredivisie 1H Total  spread 0.10, depth to 850,           volume 3
        Belgium Spread       spread 0.95, depth 25,               volume 0
        Eredivisie 1H Winner spread 0.94, depth 5,                volume 0
        J-League 1H Winner   spread 0.95, depth 1010,             volume 0
    So the totals family is genuinely quoted; the winner/spread family is
    quoted at ~95c, which is a placeholder rather than a market. Nothing in any
    of them has meaningfully traded.

THE CALL: flag the leagues rather than build them. The blocker is not modelling
-- Belgium and Eredivisie would drop into the existing football-data template --
it is that a league pipeline would currently serve three untraded series, and
the half that IS quoted tightly (totals) is priced by the Poisson goals model
rather than the validated Elo. Revisit on volume.

Pass --apply to write. Default is a dry run.
"""
import sys

from app.db.database import SessionLocal
from app.db.models import CatalogEntry
from app.ingestion.poller_lock import db_write_lock

APPLY = "--apply" in sys.argv

_SOCCER_VOL = ("Priceable in principle -- football-data.co.uk carries this league "
               "(Belgium B1 311 rows / Eredivisie N1 306 rows, 2025/26, all 6 columns "
               "the existing 5 leagues use), so it needs no new modelling, just the "
               "same ingestion template. NOT built 2026-08-06 because the Kalshi "
               "markets are effectively untraded: totals quote tightly (0.06-0.10 "
               "spread, depth to ~700) but volume is 0, and the winner/spread series "
               "quote at a ~0.95 spread, which is a placeholder not a market. "
               "UNBLOCKS WHEN: real volume appears on the winner/spread series. "
               "Re-check with scripts/triage_new_markets_202608.py's own numbers.")

_JLEAGUE = ("Priceable in principle but on a DIFFERENT path than Belgium/Eredivisie: "
            "football-data.co.uk has no J-League, so it would follow the MLS template "
            "(ESPN jpn.1, confirmed live -- 10 completed fixtures in June 2026). That "
            "is a second ingestion path with its own name-matching, and the season runs "
            "Feb-Dec rather than Aug-May. NOT built 2026-08-06: the 1st-half winner "
            "series quotes at a ~0.95 spread with 0 volume. UNBLOCKS WHEN: volume "
            "appears AND the Belgium/Eredivisie template is proven first.")

_COPPA = ("NOT PRICEABLE -- data blocked, not effort blocked. Soccer Elo here is "
          "PER-LEAGUE, and Coppa Italia mixes Serie A, B and C. football-data.co.uk "
          "has Serie A (I1, 380 rows) and Serie B (I2, 380) but NO Serie C, and the "
          "early rounds are full of Serie C sides (e.g. Catania, Vicenza), so one side "
          "of most ties cannot be rated at all. ESPN does list the fixtures, which is "
          "why they keep appearing -- the fixtures are not the missing piece. "
          "UNBLOCKS WHEN: a free Serie C results source exists AND cross-division "
          "ratings are built. Same reasoning applies to every cup/continental comp.")

_AWARD = ("NOT PRICEABLE -- resolves on a VOTE, not on play. Award finalists are "
          "decided by media/panel balloting, so there is nothing in box-score data to "
          "model; this is not a hard projection problem, it is a different kind of "
          "event. No plan to build.")

_STAT_LEADER = ("NOT PRICEABLE with what exists -- needs a per-player projection model "
                "(per-90 rates plus minutes/availability), which this app does not have "
                "for any sport. It is buildable in principle, unlike the award markets, "
                "but it is a whole new model class AND the standing decision after the "
                "player-stat mis-stake (~$3,790) is that player-stat futures stay "
                "TRACKING-ONLY. UNBLOCKS WHEN: a validated player projection model "
                "exists and that tracking-only policy is deliberately revisited.")

_ROSTER = ("NOT PRICEABLE, permanently. Resolves on transfer/roster NEWS, not on a "
           "sporting outcome -- there is no match, no result, and nothing an Elo or "
           "simulation could estimate. Dismissing rather than deferring: this will not "
           "become buildable.")

_MOTOGP = ("Genuinely deferrable, not dead. The racing engine already models F1, NASCAR "
           "and IndyCar, and MotoGP is the same shape (grid + finishing order), so the "
           "model exists. MISSING PIECE: a free MotoGP results/grid source -- the "
           "racing pipeline's existing sources do not cover it. UNBLOCKS WHEN: that "
           "source is found and confirmed to carry finishing positions.")

_LEC_QUALIFY = ("Genuinely deferrable. The esports tournament sim (Elo-seeded Monte "
                "Carlo) already prices qualify/advance questions like this one, but it "
                "ships PRICED-NOT-STAKED behind an 'approx' badge because it has never "
                "been validated against real bracket outcomes. UNBLOCKS WHEN: that sim "
                "is validated well enough to stake.")

# (identifier, disposition, note)
PLAN = [
    ("KXBELGIANPLBTTS", "flagged", _SOCCER_VOL),
    ("KXBELGIANPLSPREAD", "flagged", _SOCCER_VOL),
    ("KXBELGIANPLTOTAL", "flagged", _SOCCER_VOL),
    ("KXEREDIVISIE1H", "flagged", _SOCCER_VOL),
    ("KXEREDIVISIE1HBTTS", "flagged", _SOCCER_VOL),
    ("KXEREDIVISIE1HSPREAD", "flagged", _SOCCER_VOL),
    ("KXEREDIVISIE1HTOTAL", "flagged", _SOCCER_VOL),
    ("KXJLEAGUE1H", "flagged", _JLEAGUE),
    ("KXJLEAGUE1HBTTS", "flagged", _JLEAGUE),
    ("KXJLEAGUE1HSPREAD", "flagged", _JLEAGUE),
    ("KXJLEAGUE1HTOTAL", "flagged", _JLEAGUE),
    ("KXMOTOGPRACE", "flagged", _MOTOGP),
    ("lec-2026-summer-split-team-to-qualify-for-playoffs-20260805200222397", "flagged", _LEC_QUALIFY),

    ("KXCOPPAITALIA1H", "not_relevant", _COPPA),
    ("KXCOPPAITALIA1HBTTS", "not_relevant", _COPPA),
    ("KXCOPPAITALIA1HSPREAD", "not_relevant", _COPPA),
    ("KXCOPPAITALIA1HTOTAL", "not_relevant", _COPPA),
    ("KXCOPPAITALIAFTTS", "not_relevant", _COPPA),
    ("KXCOPPAITALIASCORE", "not_relevant", _COPPA),
    ("KXCOPPAITALIATEAMTOTAL", "not_relevant", _COPPA),

    ("KXMLBAWARDFIN", "not_relevant", _AWARD),
    ("KXNFLAWARDFIN", "not_relevant", _AWARD),
    ("KXNFLFFLEADERTOP", "not_relevant", _STAT_LEADER),
    ("KXLIGAMXLEADER", "not_relevant", _STAT_LEADER),
    ("KXMLSLEADER", "not_relevant", _STAT_LEADER),
    ("liga-mx-2026-27-apertura-most-assists-20260722171621631", "not_relevant", _STAT_LEADER),
    ("lec-summer-split-2026-mvp-20260805200738278", "not_relevant", _STAT_LEADER),

    ("which-players-will-leave-their-team-in-2026-20260805220300453", "not_relevant", _ROSTER),
    ("which-teams-will-make-a-roster-change-before-december-20260806154016844", "not_relevant", _ROSTER),
]

s = SessionLocal()
undecided = s.query(CatalogEntry).filter(CatalogEntry.dismissed == 0).all()
by_ident = {e.identifier: e for e in undecided}
print(f"undecided catalog entries: {len(undecided)}")
print(f"plan covers: {len(PLAN)}")

missing = [i for i, _d, _n in PLAN if i not in by_ident]
extra = [e.identifier for e in undecided if e.identifier not in {i for i, _d, _n in PLAN}]
if missing:
    print(f"  in plan but NOT undecided (already handled / gone): {len(missing)}")
    for i in missing:
        print(f"    {i}")
if extra:
    print(f"  UNDECIDED BUT NOT IN PLAN -- would be left sitting: {len(extra)}")
    for i in extra:
        print(f"    {i}")

flag = [p for p in PLAN if p[1] == "flagged" and p[0] in by_ident]
drop = [p for p in PLAN if p[1] == "not_relevant" and p[0] in by_ident]
print(f"\n  -> flag (deferred, with a reason): {len(flag)}")
print(f"  -> dismiss (not priceable):        {len(drop)}")

if not APPLY:
    print("\n(dry run -- pass --apply to write)")
    raise SystemExit

with db_write_lock():
    w = SessionLocal()
    try:
        n = 0
        for ident, disposition, note in PLAN:
            row = w.query(CatalogEntry).filter(CatalogEntry.identifier == ident).first()
            if row is None or row.dismissed == 1:
                continue
            row.dismissed = 1
            row.disposition = disposition
            row.note = note
            n += 1
        w.commit()
        print(f"\nAPPLIED: triaged {n} entries")
    finally:
        w.close()

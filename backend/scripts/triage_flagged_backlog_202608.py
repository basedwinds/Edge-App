"""Give every entry in the flagged backlog a reason -- or clear it if it's done.

The New Markets tab was worked to zero on 2026-08-06, but the FLAGGED backlog
behind it held 99 entries and only the 13 written that day carried a note. The
other 86 were indistinguishable from "nobody has looked at this", which is the
exact problem the note field exists to solve.

Two passes, in this order:

  1. RESOLVE WHAT IS ALREADY BUILT. A flagged entry is stale the moment the app
     starts ingesting that series, and nothing ever walked back over it. Decided
     by DATA, not judgement: if markets exist in our own DB whose source_ticker
     starts with the series ticker, it is built. Confirmed live before writing
     this -- KXNFL1H 16 markets, KXNFL2H 16, KXWNBAWINS 45, KXWNBAPLAYOFF 15,
     KXTEAMSINWS 225. Those are finished work sitting in a to-do list.

  2. NOTE THE REST BY CLASS. Every remaining entry gets the reason it is
     waiting and what would unblock it.

Pass --apply to write. Default is a dry run.
"""
import sys
from collections import Counter, defaultdict

from app.db.database import SessionLocal
from app.db.models import CatalogEntry, Market
from app.ingestion.poller_lock import db_write_lock

APPLY = "--apply" in sys.argv

_CUP = ("NOT PRICEABLE -- data blocked. Soccer Elo here is PER-LEAGUE, and a cup or "
        "continental competition is by definition cross-league: it pits teams from "
        "different domestic leagues (or different divisions of one) against each "
        "other, and a per-league rating cannot compare them. Many entrants are in no "
        "league this app models at all. The fixtures are available -- that is not the "
        "missing piece. UNBLOCKS WHEN: a cross-league rating scale exists (a single "
        "European rating pool, or a rated-vs-unrated fallback). Applies to every "
        "UEFA/CONMEBOL/AFC comp, domestic cups and super cups alike.")

_LEAGUE_BUILDABLE = ("Priceable in principle -- football-data.co.uk carries this league, so it "
                     "needs the same ingestion template as the existing 5, not new modelling. "
                     "NOT built 2026-08-06: the Kalshi markets are effectively untraded "
                     "(totals quote at a 0.06-0.10 spread but 0 volume; winner/spread series "
                     "quote at a ~0.95 spread, which is a placeholder rather than a market). "
                     "UNBLOCKS WHEN: real volume appears.")

_LEAGUE_NO_SOURCE = ("Priceable only via a SECOND data path. football-data.co.uk does not carry "
                     "this league, so it would follow the MLS template (ESPN), which brings its "
                     "own name-matching and, for J-League, a Feb-Dec season instead of Aug-May. "
                     "Combined with 0 traded volume on the Kalshi series, not worth the second "
                     "pipeline yet. UNBLOCKS WHEN: volume appears AND the football-data leagues "
                     "are proven first.")

_PLAYER_AWARD = ("NOT PRICEABLE -- resolves on a VOTE (media/panel balloting), not on play, so "
                 "there is nothing in box-score data to model. Distinct from a stat leader, "
                 "which is at least a counting problem. No plan to build.")

_PLAYER_STAT = ("NOT PRICEABLE with what exists -- needs a per-player projection model (per-90 "
                "rates plus minutes/availability) that this app has for no sport. Buildable in "
                "principle, but a whole new model class, and the standing decision after the "
                "player-stat mis-stake (~$3,790) is that player-stat futures stay TRACKING-ONLY. "
                "UNBLOCKS WHEN: a validated player projection model exists and that policy is "
                "deliberately revisited.")

_SEASON_SIM = ("Buildable from a model that ALREADY EXISTS -- this is a season/bracket outcome "
               "of a sport with a working season sim, not a new model. It was flagged before "
               "that sim covered it. NEXT STEP: confirm the sim emits this specific question "
               "(champion / seed / advance) and wire the market to it, rather than building "
               "anything new.")

_ESPORTS_SIM = ("Buildable from the esports tournament sim (Elo-seeded Monte Carlo), which "
                "already prices bracket questions like this -- but it ships PRICED-NOT-STAKED "
                "behind an 'approx' badge, never having been validated against real bracket "
                "outcomes. UNBLOCKS WHEN: that sim is validated well enough to stake.")

_MMA_SUPPLY = ("Investigated and REJECTED on supply, not on modelling -- non-UFC MMA promotions "
               "were measured to list too few fights, too late, to be worth a pipeline. Kept "
               "flagged rather than dismissed so the decision is visible if supply changes.")

_HALF_BUILDABLE = ("Buildable -- this sport has a working game model and the half-market maths "
                   "already exists for NFL (see game_lines.prob_team_wins_half, with its "
                   "measured half tie-rates). Extending it here is applying a built model to "
                   "another sport, not new research. NEXT STEP: confirm the half tie-rate and "
                   "scoring split for this sport before reusing NFL's constants -- they are "
                   "sport-specific and must be measured, not assumed.")

# Series prefix -> note, for entries that are NOT already built.
_BY_PREFIX = [
    ("KXUEFASC", _CUP), ("KXUECL", _CUP), ("KXUCL", _CUP), ("KXAFCCL", _CUP),
    ("KXCOPPAITALIA", _CUP), ("KXCONMEBOL", _CUP), ("KXFRASUPERCUP", _CUP),
    ("KXEREDIVISIE", _LEAGUE_BUILDABLE), ("KXBELGIANPL", _LEAGUE_BUILDABLE),
    ("KXLIGAPORTUGAL", _LEAGUE_BUILDABLE),
    ("KXJLEAGUE", _LEAGUE_NO_SOURCE), ("KXLIGAMX", _LEAGUE_NO_SOURCE),
    ("KXNCAAF", _HALF_BUILDABLE), ("KXNFL1H", _HALF_BUILDABLE), ("KXNFL2H", _HALF_BUILDABLE),
    ("KXNFLSEED", _SEASON_SIM), ("KXNFLPLAYOFFHOST", _SEASON_SIM),
    ("KXWNBAFINAL", _SEASON_SIM), ("KXWNBASEMIFINAL", _SEASON_SIM),
    ("KXWNBAPLAYOFF", _SEASON_SIM), ("KXWNBA1SEED", _SEASON_SIM),
    ("KXWNBAWINS", _SEASON_SIM), ("KXWNBAOT", _SEASON_SIM), ("KXTEAMSINWS", _SEASON_SIM),
    ("KXWNBA6POY", _PLAYER_AWARD), ("KXWNBAMIMP", _PLAYER_AWARD),
    ("KXWNBAALLDEFENSE", _PLAYER_AWARD), ("KXWNBAALLROOKIE", _PLAYER_AWARD),
    ("KXWNBAALLTEAM", _PLAYER_AWARD),
    ("KXMMAFIGHT", _MMA_SUPPLY),
    # MUST stay last: matching is first-hit on startswith, so the bare series
    # would otherwise swallow KXWNBAWINS, KXWNBA6POY and every other WNBA
    # series before their own, more specific rules were reached.
    ("KXWNBA", _SEASON_SIM),
]
_BY_SUBSTRING = [
    ("champion", _SEASON_SIM), ("defensive-player", _PLAYER_AWARD),
    ("make-postseason", _SEASON_SIM), ("postseas", _SEASON_SIM),
    ("lck-cl", _ESPORTS_SIM), ("winner", _ESPORTS_SIM),
]

s = SessionLocal()
flagged = s.query(CatalogEntry).filter(CatalogEntry.disposition == "flagged").all()
todo = [e for e in flagged if not e.note]
print(f"flagged entries: {len(flagged)}   already carrying a note: {len(flagged) - len(todo)}   to handle: {len(todo)}")

built, noted, unmatched = [], [], []
for e in todo:
    ident = e.identifier
    # MUST match "SERIES-" not "SERIES", because Kalshi tickers are
    # SERIES-EVENT-OUTCOME and series names nest: a bare "KXWNBA%" also matches
    # KXWNBAGAME, KXWNBAWINS and every other WNBA series. Caught in the dry run
    # -- "WNBA Championship" (series KXWNBA) showed 910 loose matches and would
    # have been declared already-built, when the strict count is 0 and it is not
    # built at all. A wrong "already done" is worse than no note: it retires a
    # real to-do silently.
    n_markets = (
        s.query(Market).filter(Market.source_ticker.like(f"{ident}-%")).count()
        if ident.startswith("KX") else 0
    )
    if n_markets > 0:
        built.append((e, n_markets))
        continue
    note = next((n for p, n in _BY_PREFIX if ident.startswith(p)), None)
    if note is None:
        low = ident.lower()
        note = next((n for sub, n in _BY_SUBSTRING if sub in low), None)
    if note is None:
        unmatched.append(e)
    else:
        noted.append((e, note))

print(f"\n  ALREADY BUILT -> resolve (markets exist in our DB): {len(built)}")
for e, n in built:
    print(f"    {e.identifier:28} {n:5} markets   {e.title[:38]}")
print(f"\n  note by class: {len(noted)}")
print("   ", dict(Counter(n.split(' --')[0].split(' (')[0][:28] for _e, n in noted)))
if unmatched:
    print(f"\n  UNMATCHED -- would stay un-noted: {len(unmatched)}")
    for e in unmatched:
        print(f"    {e.sport:8} {e.identifier[:46]:48} {e.title[:34]}")

if not APPLY:
    print("\n(dry run -- pass --apply to write)")
    raise SystemExit

with db_write_lock():
    w = SessionLocal()
    try:
        for e, n in built:
            row = w.get(CatalogEntry, e.id)
            row.disposition = "resolved"
            row.note = (f"Already built -- {n} markets with this series ticker are ingested and "
                        f"live in the app. The flag predated the build and nothing walked back "
                        f"over it. Cleared 2026-08-06 by scripts/triage_flagged_backlog_202608.py, "
                        f"which decides this from the DB rather than by eye.")
        for e, note in noted:
            w.get(CatalogEntry, e.id).note = note
        w.commit()
        print(f"\nAPPLIED: resolved {len(built)} already-built, noted {len(noted)}")
    finally:
        w.close()

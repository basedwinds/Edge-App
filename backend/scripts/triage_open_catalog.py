"""Give every open New-Markets entry a disposition, with a reason.

The queue must not contain rows nobody has ruled on. auto_close_ingested
(catalog_resolution.py) closes the ones already being ingested; this rules on
what is left, so the badge reflects genuinely-undecided work rather than
accumulated silence.

DISPOSITIONS USED, and what each promises:

  built         we already ingest it -- handled by auto_close_ingested, not here
  flagged       BUILDABLE, not built. The note says what is missing.
  deferred      real, but deliberately postponed (golf, off-season sports)
  not_relevant  we cannot price it -- no free data source exists for the input

EVERY not_relevant HERE IS A DATA VERDICT, NOT A TASTE ONE. Corners and
goalscorer markets need per-event data (corner counts, scorer identity) that no
free feed in this app supplies; that is the same reason `ftts` is priced but
never validated. Nothing is dismissed for being unfamiliar -- a stale dismissal
note is exactly how the UEFA continental work sat "unpriceable" for months while
uefa_match.py was already built and wired.

flagged IS THE DEFAULT FOR ANYTHING UNCERTAIN. It keeps the entry visible with a
stated blocker rather than silently retiring it.
"""
import argparse
import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.database import SessionLocal  # noqa: E402
from app.db.models import CatalogEntry  # noqa: E402

# Kalshi series prefixes whose league THIS APP ALREADY RATES, so the model input
# exists and only the series wiring is missing. Verified against the live Elo
# pool (33 leagues carrying fixtures): EFL Championship=E1, League One=E2,
# Ligue 2=F2, Super Lig=T1, Saudi=KSA1.
RATED_PREFIXES = ("KXEFLCHAMPIONSHIP", "KXEFLL1", "KXLIGUE2", "KXSUPERLIG", "KXSAUDIPL")
CUP_PREFIXES = ("KXGRECUP", "KXISRPLCUP", "KXSCOCUP", "KXSERIECCUP", "KXSVKCUP", "KXUEFASC")

# Domestic cups whose BOTH sides sit in rated countries, so the tier bridge
# applies: England (E0/E1/E2), Spain (SP1/SP2), Netherlands (N1), Portugal (P1),
# Germany (D1/D2). DFB Pokal and EFL Cup already carry fixtures in the pool.
CUP_EXACT = {"KXFACUP", "KXEFLCUP", "KXCOPADELREY", "KXKNVBCUP", "KXTACAPORT", "KXDFBPOKAL"}

# League-title / finishing-position markets for leagues the pool DOES rate, so
# the season sim is the source. D1/SP1/F1/I1/P1/DNK1 all carry fixtures.
SIM_EXACT = {"KXBUNDESLIGATOP", "KXLALIGATOP", "KXLIGUE1TOP", "KXSERIEATOP",
             "KXDENSUPERLIGA", "KXLIGAPORTUGAL"}

# Resolve on an ANNOUNCEMENT or a scheduling decision, not on a result. No
# sports model can price these even in principle -- the same reason the CFB
# "Team to be Ranked #1" poll market is deliberately not ingested.
NOT_A_RESULT = {"KXMLBFODTEAMS"}


def rule(entry) -> tuple[str, str] | None:
    """(disposition, note) for one entry, or None to leave it alone."""
    t = (entry.title or "")
    tl = t.lower()
    ident = (entry.identifier or "")

    # No free data source for the INPUT. Not a taste call.
    if re.search(r"corner", tl):
        return ("not_relevant",
                "No corner-count data in any free feed this app uses, so the input for a "
                "corners model does not exist. Revisit only if a corner feed is added.")
    if re.search(r"goalscorer", tl):
        return ("not_relevant",
                "Needs scorer-level event data, which no free feed here supplies -- the same "
                "gap that leaves `ftts` priced but unvalidatable. Not buildable today.")

    # Deliberately postponed.
    if re.search(r"korn ferry|pga|golf|tiger woods", tl):
        return ("deferred",
                "Golf is parked by user decision (2026-08-13). Real market, no model yet; "
                "revisit if golf is picked up.")

    # Not a sporting result at all.
    if ident in NOT_A_RESULT or "announce his retirement" in tl:
        return ("not_relevant",
                "Resolves on an announcement or a scheduling decision, not on a game result, "
                "so no sports model can price it even in principle -- the same reason the CFB "
                "poll-resolved 'Team to be Ranked #1' market is deliberately not ingested.")

    # Racing. The engine ALREADY prices pole and top-N for IndyCar: 105 pole and
    # 150 top_n IndyCar rows are live right now (from Polymarket). Only the
    # Kalshi series is unwired -- do NOT note these as unmodelled.
    if ident in ("KXINDYCARPOLE", "KXINDYCARTOP5"):
        return ("flagged",
                "BUILDABLE NOW: the racing engine already prices this exact market type for "
                "IndyCar -- 105 pole and 150 top_n IndyCar rows are live from Polymarket, with "
                "per-series attrition already fitted for top_n. Blocked only on adding this "
                "Kalshi series; the model needs no new work.")
    if ident == "KXINDYCARFASTLAP":
        return ("flagged",
                "NEW MARKET TYPE: no fastest-lap model exists for any series, and the racing "
                "result scraper captures finishing position and pole, not lap times. Needs a "
                "lap-level result feed before anything can be priced or settled.")

    # Buildable from a season sim that already runs.
    if "runner-up" in tl:
        return ("flagged",
                "BUILDABLE: a season sim already yields P(finish 1st) for the leagues we rate, "
                "and runner-up is P(finish 2nd) from the same simulation. Blocked on wiring "
                "that output, and only for rated leagues -- many of these (Bolivia, Morocco, "
                "Egypt, Kazakhstan, Iceland) are not in the 33-league pool at all.")
    if "winning margin" in tl:
        return ("flagged",
                "BUILDABLE: needs a points-margin distribution out of the league season sim, "
                "which currently returns finishing positions only. Bigger than the runner-up "
                "wiring, same source.")
    if ident in SIM_EXACT:
        return ("flagged",
                "BUILDABLE NOW: this league IS rated and carries fixtures (Bundesliga=D1, La "
                "Liga=SP1, Ligue 1=F1, Serie A=I1, Liga Portugal=P1, Danish Superliga=DNK1), "
                "so the season sim already produces the finishing-position distribution this "
                "market needs. Blocked only on wiring the series and its position rungs.")
    if ident == "KXNFLLASTTOLOSE":
        return ("flagged",
                "BUILDABLE: the NFL season sim already simulates every game, so P(team is the "
                "last undefeated) is a read off the same trial loop that feeds the existing NFL "
                "futures. Needs a survivor-style aggregation the sim does not currently emit.")
    if ident == "KXUCLLEAGUE":
        return ("flagged",
                "Subject is a domestic LEAGUE, not a club. uefa_match.py prices individual UCL "
                "ties, but there is no UCL bracket sim to aggregate club win probabilities up "
                "to a league, so this needs the tournament sim first. Same shape as the CFB "
                "'Conference to Win National Championship' market, which is also not ingested.")

    if ident.startswith(RATED_PREFIXES):
        return ("flagged",
                "BUILDABLE NOW: this app already rates this league (EFL Championship=E1, "
                "League One=E2, Ligue 2=F2, Super Lig=T1, Saudi=KSA1) and already prices this "
                "market type for other leagues. Blocked only on adding the series to the "
                "soccer Kalshi client.")
    if ident.startswith(CUP_PREFIXES) or ident in CUP_EXACT:
        return ("flagged",
                "BUILDABLE: cup MATCH pricing exists via the promotion-derived tier bridge "
                "(cup_match.py, live for Coppa Italia/DFB Pokal), and both countries here are "
                "rated. A cup WINNER future additionally needs a bracket sim, which does not "
                "exist -- so the match markets are the near-term win, not the outright.")

    # Titles this app carries no rating pool for at all.
    if ident in ("KXDOTA2", "KXVOLLEYBALLMATCH"):
        return ("flagged",
                "No rating pool exists for this title/sport -- it would need a full Elo build "
                "plus a free results feed for settlement, not a wiring change. Ranked behind "
                "the sports already carrying live markets.")
    if ident == "KXROCKETLEAGUE":
        return ("flagged",
                "Rocket League is already tracked as its own backlog item and is BLOCKED on "
                "data: the usable results source needs an API key, which conflicts with the "
                "free-sources-only constraint. Not a wiring gap.")

    # Everything else: a real listing in a league we do not rate.
    return ("flagged",
            "Needs the underlying league added to the Elo pool first -- there is no rating "
            "for these clubs, so nothing can price it. Ranked behind leagues with live "
            "markets AND an ESPN settlement feed.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    s = SessionLocal()
    try:
        open_entries = [r for r in s.query(CatalogEntry).all()
                        if not r.dismissed and not r.disposition]
        plan = []
        for r in open_entries:
            got = rule(r)
            if got:
                plan.append((r, got[0], got[1]))

        by = collections.Counter(d for _, d, _ in plan)
        print(f"open entries: {len(open_entries)}   ruled on: {len(plan)}")
        print("plan:", dict(by.most_common()))
        print()
        for disp in ("not_relevant", "deferred", "flagged"):
            sample = [(r, n) for r, d, n in plan if d == disp][:3]
            if sample:
                print(f"--- {disp} ({by[disp]}) ---")
                for r, n in sample:
                    print(f"   {(r.title or '')[:46]:46s} {n[:76]}")
                print()

        if not args.apply:
            print("DRY RUN -- nothing written. Re-run with --apply.")
            return
        for r, d, n in plan:
            r.disposition = d
            r.note = n
        s.commit()
        left = [r for r in s.query(CatalogEntry).all()
                if not r.dismissed and not r.disposition]
        print(f"APPLIED. New Markets badge: {len(open_entries)} -> {len(left)}")
    finally:
        s.close()


if __name__ == "__main__":
    main()

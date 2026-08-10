"""Triage the 120 catalog entries the scan-crash fix unhid.

Every entry gets a disposition AND a real reason. The reason is the point: the
Boxing entries were dismissed with no note, so months later nobody could tell
"evaluated and rejected" from "swept away in a bulk triage", and the whole
analysis had to be redone from scratch. See catalog.py::DismissIn.

Run with --apply to write; without it, prints the plan only.
"""
import sys

from app.db.database import SessionLocal
from app.db.models import CatalogEntry

FLAG, DROP = "flagged", "not_relevant"

# ---- explicit, per-series calls ------------------------------------------
CALLS: dict[str, tuple[str, str]] = {
    # --- REAL COVERAGE GAPS in sports this app already prices ---
    "KXNASCARCUPCHAMP": (FLAG,
        "NASCAR Cup title on KALSHI, and we do not ingest it. The model already exists: "
        "racing_playoff_sim prices the NASCAR championship and /racing/futures serves 36 such "
        "rows -- but every one is POLYMARKET. Kalshi's racing championship ingestion covers "
        "KXF1, KXF1CONSTRUCTORS and KXINDYCARSERIES only, so NASCAR is the one series missing. "
        "Pure ingestion work, no new model."),
    "KXWNBAEAST": (FLAG,
        "WNBA Eastern Conference champion. season_sim_wnba already simulates the season and the "
        "app prices championship/one_seed/playoff_qualifier for WNBA -- but there is no "
        "conference_champion market type at all. The sim produces this directly."),
    "KXWNBAWEST": (FLAG,
        "WNBA Western Conference champion -- same gap and same fix as KXWNBAEAST."),
    "KXWNBAEAST1": (FLAG,
        "WNBA East #1 seed. The app has a league-wide one_seed type but nothing per-conference; "
        "season_sim_wnba can answer this without new modelling."),
    "KXWNBAWEST1": (FLAG,
        "WNBA West #1 seed -- same gap as KXWNBAEAST1."),
    "KXWNBASERIES": (FLAG,
        "Playoff SERIES winner. The app already prices WNBA semifinal_qualifier/finals_qualifier, "
        "so the bracket state exists; confirm the series shape (best-of length) before building, "
        "since that is what decides whether the existing sim answers it directly."),

    # --- BLOCKED ON A KNOWN, RECORDED ISSUE ---
    "KXNASCARPOLE": (FLAG,
        "NASCAR pole. Already a known open item (racing loose ends): pole prices sit at zero, so "
        "ingesting more pole supply is not the blocker -- the pole model is. Flagged so it moves "
        "with that fix rather than being rediscovered."),
    "KXF1TOP10": (FLAG,
        "F1 top-10 finishers. The app prices KXF1TOP5 already, but racing top_n is currently "
        "GATED OFF staking for miscalibration in the deep tail -- the spread was fitted on P(1st) "
        "and never constrained beyond it. Blocked on that fix, not on ingestion."),
    "KXF1RACETOP10": (FLAG, "F1 race top-10 -- same top_n calibration blocker as KXF1TOP10."),
    "KXF1RACETOP5": (FLAG, "F1 race top-5 -- same top_n calibration blocker as KXF1TOP10."),
    "KXF1RACETOPX": (FLAG, "F1 race top-N (variable N) -- same top_n calibration blocker."),

    # --- F1 SPRINT FAMILY: verify against the earlier sprint build ---
    "KXF1RACESPRINT": (FLAG,
        "F1 sprint race. A sprint family was built previously, yet this ticker is not ingested -- "
        "so either it is an alternate/renamed series or the build missed it. Check which before "
        "writing code; the sprint uses a different points/finish structure from the main race."),
    "KXF1SPRINTPOLE": (FLAG, "F1 sprint qualifying pole -- verify against the earlier sprint build, as KXF1RACESPRINT."),
    "KXF1SPRINTTOP5": (FLAG, "F1 sprint top-5 -- sprint build check PLUS the top_n calibration blocker."),
    "KXF1SPRINTTOP10": (FLAG, "F1 sprint top-10 -- sprint build check PLUS the top_n calibration blocker."),
    "KXF1SPRINTTOPCONSTRUCTOR": (FLAG,
        "F1 sprint top constructor. Needs a constructor-level finish model; the championship sim "
        "has constructor strength but not per-race constructor ordering."),
    "KXF1TOPCONSTRUCTOR": (FLAG,
        "F1 race top constructor -- same constructor-ordering gap as the sprint version."),

    # --- POSSIBLE DUPLICATES: confirm before building ---
    "KXF1POLEPOSITION": (FLAG,
        "Looks like an alternate spelling of KXF1POLE, which IS ingested and priced. Confirm "
        "whether Kalshi lists both or renamed one; if it is a rename, the ingestion needs to "
        "follow it or pole coverage silently lapses when the old ticker retires."),
    "KXF1QUALIFY": (FLAG,
        "F1 qualifying. Related to the priced KXF1POLE but a broader question (grid position, not "
        "just P1). Worth confirming what it resolves on before deciding it is a duplicate."),
    "KXF1FASTESTLAP": (FLAG,
        "F1 fastest lap. No fastest-lap model exists -- it is only weakly related to race pace "
        "(teams pit for it late), so this needs its own study rather than reuse of race odds."),
    "KXF1FASTLAP": (FLAG, "Duplicate-looking ticker of KXF1FASTESTLAP; confirm which one Kalshi actually lists."),
    "KXF1SPRINTFASTLAP": (FLAG, "F1 sprint fastest lap -- same missing model as KXF1FASTESTLAP."),
    "KXF1H2H": (FLAG,
        "F1 head-to-head. Racing H2H was built previously, so confirm whether this ticker is "
        "already covered under another series before building it again."),
    "KXNASCARH2H": (FLAG, "NASCAR head-to-head -- same check as KXF1H2H."),
    "2026-f1-drivers-champion": (FLAG,
        "Polymarket F1 drivers' title by EVENT SLUG. The app already ingests Polymarket F1 "
        "championship markets, but keyed by condition_id, so this may already be covered. Verify "
        "before building -- if it is covered, dismiss it; if not, it is a live futures gap."),
    "f1-constructors-champion": (FLAG, "Polymarket F1 constructors' title -- same verification as the drivers' slug."),
    "blast-open-porto-2026-winner": (FLAG,
        "CS2 tournament winner on Polymarket. The app already runs a CS2 tournament Monte Carlo "
        "(priced, not staked), so this is ingestion of a venue we do not read for this event type."),

    # --- NEW SPORT: GOLF (largest uncovered supply in this batch) ---
    "KXDPWORLDTOUR": (FLAG,
        "GOLF is the biggest uncovered sport in this batch: 7 Polymarket PGA markets plus DP World "
        "Tour, LPGA and Champions Tour on Kalshi. Needs a golf model from scratch (field-strength "
        "simulation, not Elo -- golf is a 150-player field, not a two-sided matchup) and a free "
        "results source. Flagged as the expansion candidate, not as quick ingestion."),
    "KXDOTA2TOTALMAPS": (FLAG,
        "Dota 2 -- previously identified as a live esports expansion candidate. Note the market "
        "itself is total-maps, which is the tracking-only class in every other title here, so it "
        "would not be stakeable on arrival; it is evidence of supply, not a build target."),

    # --- CORRECTED after a boundary bug in this script's own already-ingested
    # check dismissed these four as "already priced" when they are not. See
    # decide(). ---
    "KXNASCAR": (FLAG,
        "NASCAR Cup PLAYOFF QUALIFICATION ('make the 16'). Only KXNASCARRACE is ingested; this "
        "series is not. racing_playoff_sim already simulates the regular season to decide the "
        "playoff field, so qualification probability falls out of state it computes anyway."),
    "KXLEAGUESCUP": (FLAG,
        "Leagues Cup WINNER. The app already prices Leagues Cup fixtures "
        "(leagues_cup_moneyline_3way), so ratings exist for both the MLS and Liga MX sides -- the "
        "missing piece is a bracket sim over the competition, not the ratings. That makes this a "
        "genuinely different case from the continental cups dismissed above, where no rating "
        "spans the clubs at all."),
    "KXWNBA1H": (FLAG,
        "WNBA 1st-half winner. The app DOES price first_half_winner, but from KXWNBA1HWINNER -- "
        "this is a different ticker for what looks like the same proposition. Confirm whether "
        "Kalshi renamed the series; if so the ingestion must follow it or half-winner coverage "
        "lapses silently when the old ticker retires."),
    "KXWNBA2H": (FLAG, "WNBA 2nd-half winner -- same ticker-rename question as KXWNBA1H (priced today from KXWNBA2HWINNER)."),

    # --- NOT RELEVANT: no model path, with the reason recorded ---
    "KXNCAAFCONFLEAVE": (DROP,
        "Conference realignment, not a game outcome -- resolves on administrative announcements "
        "this app has no feed for and no way to model. (Also the ticker that crashed every "
        "catalog scan: 'CO-NFL-EAVE' contains 'NFL', so nfl and cfb both claimed it.)"),
    "KXNCAAFJOINCONF": (DROP, "Conference realignment announcement -- same as KXNCAAFCONFLEAVE, no feed and no model."),
    "KXEWCCALLOFDUTYBLOPS6": (DROP, "2025 Esports World Cup CoD event -- a PAST tournament, not live supply."),
    "KXWCCODWARZONE": (DROP, "2025 Esports World Cup Warzone -- past tournament, and Warzone is a battle-royale format this app models nothing for."),
    "KXLEADERNFLAPYDS": (DROP,
        "Player season-stat leader. Player stat futures are deliberately tracking-only here after "
        "a post-inclusion bug mis-staked them; adding more of the same class has no path to a stake."),
    "KXNBACOMPETE": (DROP, "Whether a named player competes -- an availability/news question with no model and no reliable free feed."),
    "KXMLBTRIPLECROWN": (DROP, "Individual batting award -- needs player-level projections this app does not build; same class as the tracking-only stat futures."),
    "KXVALORANTTOTALMAPS": (DROP,
        "Total maps. Per-map markets are tracking-only across all three esports titles here "
        "because the map model returns the same probability for every map -- so this is a known, "
        "already-rejected class, not new supply."),
    "KXNASCARRACEOLD": (DROP, "Superseded series ('NASCAR Race winner' old) -- KXNASCARRACE is the live one and is already priced."),
    "KXF1DELAY": (DROP, "Whether an F1 event is delayed -- a logistics/weather question with no model."),
    "KXF1OCCUR": (DROP, "Generic 'event occurrence' novelty -- not a sporting outcome."),
    "KXNASCARCHALLENGE": (DROP,
        "NASCAR In-Season Challenge is a separate bracket competition with its own seeding rules; "
        "the Cup playoff sim does not answer it, and it is a small one-off. Revisit only if the "
        "Cup title work leaves the bracket machinery reusable."),
    "KXNASCARTOPMANU": (DROP, "Top manufacturer -- requires a manufacturer-level model; racing ratings are per-driver with a constructor term only for F1."),
    "who-will-attend-the-us-open-finals": (DROP, "Celebrity attendance novelty -- not a sporting outcome."),
    "will-enes-kanter-freedom-be-drafted": (DROP, "Novelty draft market on a retired player -- not a sporting outcome this app models."),
    "KXCHAMPTOURR1LEAD": (DROP, "Champions Tour round-1 leader -- golf, and gated behind the same missing golf model as KXDPWORLDTOUR (flagged there so the decision lives in one place)."),
    "KXDPWORLDTOURR1LEAD": (DROP, "DP World Tour round-1 leader -- same golf blocker, tracked under KXDPWORLDTOUR."),
    "KXLPGAR1LEAD": (DROP, "LPGA round-1 leader -- same golf blocker, tracked under KXDPWORLDTOUR."),
    "KXASEANADVANCE": (DROP, "ASEAN club competition advancement -- soccer Elo here is PER-LEAGUE, so no rating exists that spans these clubs."),
}

# ---- pattern fallbacks ----------------------------------------------------
PATTERNS: list[tuple[tuple[str, ...], str, str]] = [
    (("2026-fedex-st-jude-championship",), DROP,
     "PGA Tour FedEx St. Jude market. Golf has no model here yet; the whole golf opportunity is "
     "tracked under KXDPWORLDTOUR so one decision covers the sport instead of seven duplicates."),
    (("KXAFCCL",), DROP,
     "AFC Champions League market type. Soccer ratings here are PER-LEAGUE, so no rating spans a "
     "continental competition -- the same blocker that keeps UCL and other cross-border cups "
     "unpriced. Unblocked only by a cross-league soccer model, which was measured and deferred."),
    (("KXCONMEBOLLIB", "KXCONMEBOLSUD"), DROP,
     "CONMEBOL Libertadores/Sudamericana market type -- same per-league rating blocker as the AFC "
     "Champions League entries; no rating spans these clubs."),
    (("KXFRASUPERCUP",), DROP,
     "French Super Cup -- a one-match cup between clubs rated in different competitions; the "
     "domestic-cup bridge covers within-country tiers, not a super cup fixture like this."),
    (("KXUSLCUP",), DROP,
     "USL Cup -- the USL is not a rated league in this app (football-data.co.uk carries no USL), "
     "so there is no rating for either side."),
    # WNBA remainder: awards, draft, contests, novelty, player props.
    (("KXWNBA3PT", "KXWNBASSTARS"), DROP,
     "All-Star skills contest -- a novelty event with no historical model and no free result feed."),
    (("KXWNBADRAFT",), DROP,
     "WNBA draft pick market -- resolves on team decisions, not play; no draft-board model or feed."),
    (("KXWNBAALLSTARS", "KXWNBAASGAME", "KXWNBAASGMVP", "KXWNBAFIRSTTEAM", "KXWNBASECONDTEAM",
      "KXWNBAROOKAS", "KXWNBAROTY", "KXWNBACCUPMVP"), DROP,
     "WNBA award/selection market -- resolves on a media or league vote, which this app models "
     "nothing for and has no data source on."),
    (("KXWNBAH2H",), DROP,
     "WNBA player-vs-player stat head-to-head -- needs player-level projections; WNBA modelling "
     "here is team-level Elo only, and player stat futures are tracking-only by policy anyway."),
    (("KXWNBA40PTS",), DROP,
     "Player to score 40 -- player-level scoring distribution, which this app does not model for WNBA."),
    (("KXWNBA7FIGS", "KXWNBARAISE", "KXWNBAPORTNOY", "KXWNBADELAY"), DROP,
     "Off-court novelty (salary, media personality, schedule) -- not a sporting outcome."),
    (("KXWNBACCUP",), DROP,
     "Commissioner's Cup -- an in-season tournament with its own qualification rules that the "
     "season sim does not represent; small, one-off, and not worth a bespoke bracket."),
]


ALREADY_INGESTED_NOTE = (
    "Already ingested and priced by this app -- surfaced only because the catalog scan had never "
    "successfully committed (it died on an IntegrityError every run), so the per-sport bootstrap "
    "that normally records existing series as baseline had never happened for this sport. Not new "
    "supply; nothing to build."
)


def _live_kalshi_prefixes(session) -> list[str]:
    from app.db.models import Market
    return [t[0] for t in session.query(Market.source_ticker)
            .filter(Market.source == "kalshi").distinct().all() if t[0]]


def decide(ident: str, live: list[str]) -> tuple[str, str] | None:
    # Explicit judgements win over everything.
    if ident in CALLS:
        return CALLS[ident]
    # Polymarket slugs carry a trailing event id, so match by prefix.
    for key, val in CALLS.items():
        if not key.startswith("KX") and ident.startswith(key):
            return val
    # Data-driven: is this series ALREADY being ingested? Derived from the
    # markets table rather than hand-listed, so it stays true as coverage
    # changes and cannot go stale the way a typed list would.
    # BOUNDARY-AWARE. A bare startswith() is wrong here and produced four false
    # dismissals on the first run: "KXNASCAR" (the playoffs series) matched
    # "KXNASCARRACE-...", "KXLEAGUESCUP" matched "KXLEAGUESCUPBTTS-...", and
    # "KXWNBA1H"/"KXWNBA2H" matched "KXWNBA1HSPREAD-...". Kalshi tickers are
    # "<SERIES>-<EVENT>-<SIDE>", so the series ends at the first hyphen and a
    # real match must respect that. Exactly the same prefix/substring sloppiness
    # as the NFL matcher bug this whole batch started with.
    if ident.startswith("KX") and any(t == ident or t.startswith(ident + "-") for t in live):
        return DROP, ALREADY_INGESTED_NOTE
    for prefixes, disp, note in PATTERNS:
        if ident.startswith(prefixes):
            return disp, note
    return None


def main() -> int:
    apply = "--apply" in sys.argv
    s = SessionLocal()
    rows = s.query(CatalogEntry).filter(CatalogEntry.dismissed == 0).all()
    live = _live_kalshi_prefixes(s)
    flagged, dropped, missed = [], [], []
    for r in rows:
        d = decide(r.identifier, live)
        if d is None:
            missed.append(r)
            continue
        (flagged if d[0] == FLAG else dropped).append((r, d[1]))
        if apply:
            r.dismissed = 1
            r.disposition = d[0]
            r.note = d[1]
    print(f"{len(rows)} undismissed -> flagged {len(flagged)}, not_relevant {len(dropped)}, UNCLASSIFIED {len(missed)}")
    if missed:
        print("\nUNCLASSIFIED (no rule matched -- fix before applying):")
        for r in missed:
            print(f"   {r.sport:9s} {r.platform:10s} {r.identifier[:40]:42s} {(r.title or '')[:40]}")
        if apply:
            s.rollback(); s.close()
            print("\nREFUSING TO APPLY while entries are unclassified.")
            return 1
    print("\nFLAGGED (worth building):")
    for r, note in sorted(flagged, key=lambda x: (x[0].sport, x[0].identifier)):
        print(f"   {r.sport:9s} {r.identifier[:34]:36s} {note[:78]}")
    if apply:
        s.commit()
        print("\nAPPLIED.")
    else:
        print("\n(dry run -- pass --apply to write)")
    s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

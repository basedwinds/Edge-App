"""CS2 polling/refresh entrypoints -- parallel to poller_valorant.py.

Covers series winner + total maps (both real, live Kalshi inventory,
confirmed 2026-07-19) + map winner + tournament winner futures (both real
Kalshi SERIES but zero open markets at build time -- kept so they start
working the moment either populates, see kalshi_cs2_client.py's own
docstring), plus the team Elo baseline. Polymarket covers series/match winner
+ map winner + series total + map handicap (see polymarket_cs2_client.py).

CORRECTED 2026-08-02 -- this docstring used to claim "No Polymarket CS2 client
exists -- checked live, real current Polymarket CS2 inventory is prop/roster-
change/futures markets only (FaZe tier-1 event, map pool changes, roster
changes), no standard match-outcome market type at all -- an honest inventory
gap, not a build gap." That was wrong. The check behind it queried
`tag_slug=cs2`, which really does return only those props -- but the real
head-to-head CS2 events are tagged `counter-strike-2`, and there are 62 of
them live carrying ~$2.7M of liquidity. It was a wrong-tag gap, not an
inventory gap. Found by catalog_scan.py's own "other" catch-all bucket; see
polymarket_cs2_client.py for the full story, including why the naive fix
(just swapping the slug) would have injected ~98 months-dead markets.

REAL COVERAGE GAP found live (2026-07-19, not a matcher bug -- verified by
checking every unmatched team name against the full raw liquipedia.net
fetch): Liquipedia:Matches' own default listing is curated toward bigger/
higher-tier tournaments -- real lower-tier/regional/qualifier/Academy-roster
matches (e.g. "eSuba vs EAC Rising", "MIBR Academy vs QUINTESSENCIA", "ARCRED
vs 1WIN") simply never appear anywhere in Liquipedia's own ~72-row fetch,
upcoming or completed. Same category of honest, structural free-source gap
as Tennis's original ATP/WTA-only coverage before tennisexplorer.com closed
the Challenger/ITF gap -- a similarly deeper Liquipedia scrape (individual
lower-tier tournament/qualifier pages, not just the curated Matches page,
see cs2_data.py::parse_matches_from_html, already built for the historical
crawl and reusable here) could close this further, not attempted yet.

REAL COVERAGE GAP found live and mostly closed (2026-07-20, user-reported --
"what else should we do for esports"): being unmatched to a real Liquipedia
row turned out to matter far less than initially assumed -- find_or_create_
upcoming_match's live-fallback path already gives every one of these matches
a real Cs2Match row. The ACTUAL blocker was best_of staying None forever on
those live-fallback rows (no Liquipedia schedule text to read "(Bo3)" from),
which gates _game_model_prob entirely regardless of Elo rating quality --
confirmed live: 24/30 real open series_winner matches had zero model_prob,
not the ~6/30 the "unmatched" framing above implied. Closed via
market_catalog_cs2.py::backfill_best_of_from_total_maps_line (KXCS2TOTALMAPS's
own O/U line structurally implies best_of even though KXCS2MAPWINNER itself
has zero real inventory) -- lifted live coverage from 6/30 to 26/30 matches
with a real model_prob. The remaining 4/30 have no KXCS2TOTALMAPS market
either, so there's genuinely no signal to backfill best_of from yet -- the
deeper Liquipedia scrape noted above is the only way to close that last
slice.

REAL BUG fixed here (found live 2026-07-19, user report: "Matches tracked"
showing 0 despite real Kalshi market rows existing): same root cause and
fix as poller_valorant.py's own docstring -- market_catalog_cs2.py::
find_or_create_upcoming_match existed but was never actually called from
refresh_kalshi_cs2_markets(), which used a strict lookup-only helper
instead and gave up with a permanently-unmatched market whenever
liquipedia.net hadn't (yet) captured that match. Fixed by calling
find_or_create_upcoming_match, same "the live listing IS the schedule"
fallback Tennis/Soccer already rely on.
"""
import datetime as dt
import logging

import httpx

from app.clients import kalshi_cs2_client, polymarket_cs2_client
from app.db.database import SessionLocal
from app.db.models import Setting
from app.ingestion import cs2_data, market_catalog_cs2
from app.ingestion.start_times import apply_start
from app.ingestion.poller_lock import db_write_lock
from app.models.baseline import elo_service_cs2

log = logging.getLogger("poller_cs2")

# Liquipedia rate-limit backoff.
#
# WHY THIS EXISTS (2026-08-09). Liquipedia temporarily IP-banned this app --
# "Your IP address has been temporarily blocked from accessing Liquipedia due to
# excessive or invalid requests" -- earned by a Call of Duty crawl that paced
# itself too aggressively. The ban is site-wide, so it took CS2 down with it:
# refresh_cs2_matches is the only route by which new CS2 fixtures enter the app.
#
# THE PART THAT MATTERS MORE THAN THE BAN. The step is wrapped in the poller's
# non-fatal try/except, so a 429 just logged a traceback and the scheduler
# retried FIVE MINUTES LATER, forever. That is 288 requests a day into an
# endpoint that has explicitly said stop -- which plausibly keeps renewing the
# very block it is trying to recover from, and is exactly the behaviour that
# turns a temporary ban into a permanent one.
#
# So on a 429 the step now stands down for COOLDOWN_HOURS instead of retrying on
# the poll interval. The deadline is stored in the DB, not in memory, because
# this process gets restarted often enough that an in-memory cooldown would
# reset on every restart and resume hammering -- which is how it behaved today.
LIQUIPEDIA_COOLDOWN_KEY = "cs2_liquipedia_cooldown_until"
COOLDOWN_HOURS = 6.0


def _cooldown_until() -> dt.datetime | None:
    session = SessionLocal()
    try:
        row = session.get(Setting, LIQUIPEDIA_COOLDOWN_KEY)
        if not row or not row.value:
            return None
        try:
            return dt.datetime.fromisoformat(row.value)
        except ValueError:
            return None
    finally:
        session.close()


def _set_cooldown(until: dt.datetime | None) -> None:
    with db_write_lock():
        session = SessionLocal()
        try:
            row = session.get(Setting, LIQUIPEDIA_COOLDOWN_KEY)
            value = until.isoformat() if until else ""
            if row is None:
                session.add(Setting(key=LIQUIPEDIA_COOLDOWN_KEY, value=value))
            else:
                row.value = value
            session.commit()
        finally:
            session.close()


def refresh_cs2_ratings():
    elo_service_cs2.refresh_ratings()


def _match_date_from_iso(occurrence_datetime: str | None) -> str | None:
    if not occurrence_datetime:
        return None
    try:
        return dt.datetime.fromisoformat(occurrence_datetime.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def refresh_cs2_matches():
    """liquipedia.net's own live schedule (upcoming + recently-decided, one
    page) -- NOT a full historical crawl, same cold-start caveat as
    elo_service_cs2.py's own docstring.

    REAL BUG this fixes (found live 2026-07-20, same "hold the DB
    connection across slow network I/O" anti-pattern this app's other
    pollers all had -- see poller_lock.py's own docstring): the fetch used
    to happen INSIDE an open SessionLocal().

    Honours the Liquipedia rate-limit cooldown -- see LIQUIPEDIA_COOLDOWN_KEY.
    A skipped pass is logged at WARNING, not swallowed: "CS2 fixtures are
    frozen" has to be visible, because everything downstream keeps working off
    the fixtures already in the DB and so looks healthy from the outside."""
    until = _cooldown_until()
    now = dt.datetime.now(dt.timezone.utc)
    if until is not None and now < until:
        mins = (until - now).total_seconds() / 60.0
        log.warning(
            "cs2 liquipedia fetch SKIPPED -- rate-limited, standing down for another "
            "%.0f min (until %s). No new CS2 fixtures or start times until then; "
            "fixtures already in the DB still price normally.",
            mins, until.isoformat(timespec="seconds"))
        return

    try:
        rows = cs2_data.fetch_matches()
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            resume = now + dt.timedelta(hours=COOLDOWN_HOURS)
            _set_cooldown(resume)
            log.error(
                "cs2 liquipedia fetch RATE-LIMITED (429). Standing down until %s (%.1fh) "
                "instead of retrying every poll -- retrying into a block is what turns a "
                "temporary one permanent. CS2 fixture ingestion is PAUSED; Kalshi and "
                "Polymarket CS2 market ingestion and pricing are unaffected.",
                resume.isoformat(timespec="seconds"), COOLDOWN_HOURS)
            return
        raise

    # A clean fetch means the block lifted -- clear the deadline so a later 429
    # starts a fresh cooldown rather than inheriting a stale one.
    if until is not None:
        _set_cooldown(None)
        log.info("cs2 liquipedia fetch recovered; rate-limit cooldown cleared")

    with db_write_lock():
        session = SessionLocal()
        try:
            count = 0
            for row in rows:
                market_catalog_cs2.upsert_liquipedia_match(session, row)
                count += 1
            session.commit()
            log.info("refreshed %d liquipedia.net cs2 matches", count)
        finally:
            session.close()


def refresh_kalshi_cs2_markets():
    """REAL BUG this fixes (found live 2026-07-20): all 4 of this
    function's own Kalshi calls used to happen INSIDE an open session,
    interleaved with real DB reads/writes -- fixed by fetching all 4 up
    front, then doing every DB-dependent step under
    poller_lock.py::db_write_lock()."""
    winner_rows = kalshi_cs2_client.get_series_winner_markets()
    total_maps_rows = kalshi_cs2_client.get_total_maps_markets()
    map_rows = kalshi_cs2_client.get_map_winner_markets()
    tournament_winner_rows = kalshi_cs2_client.get_tournament_winner_markets()

    with db_write_lock():
        session = SessionLocal()
        try:
            # Series winner (KXCS2GAME): one event per match, resolve team
            # names -> match_id ONCE per event, reuse for the total-maps
            # series below (different Kalshi series but same real match,
            # joined by team-name pair rather than a shared ticker suffix
            # -- KXCS2GAME and KXCS2TOTALMAPS confirmed live to use the
            # SAME event-ticker date+team-code suffix, e.g. both
            # "...26JUL211200FOKAST", so this could also join on that
            # shared prefix; joining by team names instead is simpler and
            # doesn't assume that ticker convention holds forever).
            # find_or_create_upcoming_match matches onto a real
            # liquipedia.net-sourced row when one exists, or creates a
            # live fallback row from Kalshi's OWN team names when it
            # doesn't (see module docstring's real-bug note).
            teams_by_event: dict[str, set[str]] = {}
            date_by_event: dict[str, str | None] = {}
            occurrence_by_event: dict[str, str | None] = {}
            for row in winner_rows:
                teams_by_event.setdefault(row["event_ticker"], set()).add(row["team_name"])
                date_by_event.setdefault(row["event_ticker"], _match_date_from_iso(row.get("occurrence_datetime")))
                occurrence_by_event.setdefault(row["event_ticker"], row.get("occurrence_datetime"))

            match_id_by_event: dict[str, int | None] = {}
            for event_ticker, teams in teams_by_event.items():
                if len(teams) == 2:
                    team_a, team_b = tuple(teams)
                    match = market_catalog_cs2.find_or_create_upcoming_match(
                        session, team_a, team_b, match_date=date_by_event.get(event_ticker)
                    )
                    match_id_by_event[event_ticker] = match.id if match else None
                    # REAL BUG this fixes (user-reported 2026-07-20: esports
                    # recommended bets missing a real match start time) --
                    # see poller_valorant.py's own version of this comment.
                    # occurrence_datetime was already being fetched above
                    # (for date_by_event) and then thrown away -- only the
                    # date survived onto the match record.
                    occurrence = occurrence_by_event.get(event_ticker)
                    if match is not None and match.winner is None:
                        apply_start(match, occurrence, source="kalshi")
                else:
                    match_id_by_event[event_ticker] = None

            matched = sum(1 for v in match_id_by_event.values() if v is not None)
            for row in winner_rows:
                market_catalog_cs2.upsert_kalshi_cs2_series_winner_market(
                    session, row, match_id_by_event.get(row["event_ticker"])
                )

            best_of_backfilled = 0
            for row in total_maps_rows:
                occurrence = row.get("occurrence_datetime")
                # match_date from the real start time -- otherwise
                # find_or_create_upcoming_match stamps today (the SCRAPE date).
                match = market_catalog_cs2.find_or_create_upcoming_match(session, row["team_a"], row["team_b"], match_date=str(occurrence)[:10] if occurrence else None)
                if match is not None and match.winner is None:
                    apply_start(match, occurrence, source="kalshi")
                # REAL COVERAGE GAP this closes (found live 2026-07-20) --
                # see market_catalog_cs2.py::backfill_best_of_from_total_maps_line's
                # own docstring for the full story. Kept even after
                # KXCS2MAP's own real per-map ladder data became available
                # below (see kalshi_cs2_client.py's own real-bug fix) --
                # this total-maps-line signal covers matches that have a
                # KXCS2TOTALMAPS market but no KXCS2MAP one, or vice versa,
                # and never overwrites a value the other one already set.
                if match is not None and row.get("line") is not None:
                    if market_catalog_cs2.backfill_best_of_from_total_maps_line(session, match.id, row["line"]):
                        best_of_backfilled += 1
                market_catalog_cs2.upsert_kalshi_cs2_total_maps_market(
                    session, row, match.id if match else None
                )

            teams_by_code: dict[str, set[str]] = {}
            occurrence_by_code: dict[str, str | None] = {}
            max_map_by_code: dict[str, int] = {}
            for row in map_rows:
                teams_by_code.setdefault(row["match_code"], set()).add(row["team_name"])
                occurrence_by_code.setdefault(row["match_code"], row.get("occurrence_datetime"))
                max_map_by_code[row["match_code"]] = max(max_map_by_code.get(row["match_code"], 0), row["map_number"])
            match_id_by_code: dict[str, int | None] = {}
            for code, teams in teams_by_code.items():
                if len(teams) == 2:
                    team_a, team_b = tuple(teams)
                    occurrence = occurrence_by_code.get(code)
                    # match_date from the real start time -- otherwise
                    # find_or_create_upcoming_match stamps today (the SCRAPE date).
                    match = market_catalog_cs2.find_or_create_upcoming_match(session, team_a, team_b, match_date=str(occurrence)[:10] if occurrence else None)
                    match_id_by_code[code] = match.id if match else None
                    if match is not None and match.winner is None:
                        apply_start(match, occurrence, source="kalshi")
                    # REAL COVERAGE GAP this closes (found live 2026-07-20,
                    # part of the same KXCS2MAP ticker fix -- see
                    # kalshi_cs2_client.py's own docstring): CS2 never had
                    # this per-map-ladder-depth backfill at all (Valorant/
                    # LoL both already had it), since its own map_winner
                    # inventory was genuinely empty until now.
                    if match is not None:
                        if market_catalog_cs2.backfill_best_of(session, match.id, max_map_by_code[code]):
                            best_of_backfilled += 1
            for row in map_rows:
                market_catalog_cs2.upsert_kalshi_cs2_map_winner_market(
                    session, row, match_id_by_code.get(row["match_code"])
                )

            for row in tournament_winner_rows:
                market_catalog_cs2.upsert_kalshi_cs2_tournament_winner_market(session, row)

            session.commit()
            log.info("kalshi cs2: %d/%d matches matched, %d best_of backfilled (total-maps line + map ladder)", matched, len(match_id_by_event), best_of_backfilled)
        finally:
            session.close()


def refresh_polymarket_cs2_markets():
    """Polymarket CS2 match-outcome ingestion -- see this module's own
    CORRECTED note above and polymarket_cs2_client.py for why this didn't
    exist until 2026-08-02.

    Same shape as poller_valorant.py's own Polymarket refresh: every market
    type for a given real match is bundled under ONE Polymarket event
    (event_slug), unlike Kalshi's per-map events, so the two real team names
    are resolved to a cs2_match_id ONCE per event_slug and reused for every
    other market type from that same event. All network I/O happens up front,
    before the session opens (poller_lock.py's own "never hold the DB
    connection across slow network I/O" rule) -- and here it is a SINGLE
    listing fetch for all four market types rather than four, see
    polymarket_cs2_client.get_all_markets on why this sport in particular
    can't afford the sibling clients' fetch-per-getter shape."""
    markets = polymarket_cs2_client.get_all_markets()
    winner_rows = markets["match_winner"]
    map_rows = markets["map_winner"]
    total_maps_rows = markets["total_maps"]
    handicap_rows = markets["map_handicap"]

    with db_write_lock():
        session = SessionLocal()
        try:
            teams_by_slug: dict[str, set[str]] = {}
            event_by_slug: dict[str, str] = {}
            start_time_by_slug: dict[str, str | None] = {}
            best_of_by_slug: dict[str, int | None] = {}
            for row in winner_rows:
                teams_by_slug.setdefault(row["event_slug"], set()).add(row["team_name"])
                event_by_slug.setdefault(row["event_slug"], row.get("event_title", ""))
                start_time_by_slug.setdefault(row["event_slug"], row.get("estimated_start_time"))
                best_of_by_slug.setdefault(row["event_slug"], row.get("best_of"))

            match_id_by_slug: dict[str, int | None] = {}
            for slug, teams in teams_by_slug.items():
                if len(teams) != 2:
                    match_id_by_slug[slug] = None
                    continue
                team_a, team_b = tuple(teams)
                match = market_catalog_cs2.find_or_create_upcoming_match(
                    session, team_a, team_b,
                    match_date=_match_date_from_iso(start_time_by_slug.get(slug)),
                    event_name=event_by_slug.get(slug),
                )
                match_id_by_slug[slug] = match.id if match else None
                if match is None:
                    continue
                # Polymarket carries a real gameStartTime on 100% of CS2 match
                # markets (494/494, confirmed live) -- a stronger signal than
                # the Kalshi path's, which depends on a liquipedia.net scrape
                # that lags real trading. This is what cs2_markets.py's
                # `_match_already_started` router gate reads, so wiring it is
                # what actually keeps started/finished matches out of
                # recommendations.
                start_time = start_time_by_slug.get(slug)
                if match.winner is None:
                    apply_start(match, start_time, source="polymarket")
                # Third best_of path, and the only direct one -- Polymarket
                # states it in the event title ("(BO3)"). Never overwrites a
                # value Kalshi's two inferred backfills already set.
                market_catalog_cs2.set_best_of(session, match.id, best_of_by_slug.get(slug))

            matched = sum(1 for v in match_id_by_slug.values() if v is not None)

            for row in winner_rows:
                market_catalog_cs2.upsert_polymarket_cs2_match_winner_row(
                    session, row, match_id_by_slug.get(row["event_slug"])
                )
            for row in map_rows:
                market_catalog_cs2.upsert_polymarket_cs2_map_winner_row(
                    session, row, match_id_by_slug.get(row["event_slug"])
                )
            for row in total_maps_rows:
                market_catalog_cs2.upsert_polymarket_cs2_total_row(
                    session, row, match_id_by_slug.get(row["event_slug"])
                )
            for row in handicap_rows:
                market_catalog_cs2.upsert_polymarket_cs2_handicap_row(
                    session, row, match_id_by_slug.get(row["event_slug"])
                )

            # Same per-map-ladder-depth backfill the Kalshi path runs, for
            # matches Polymarket lists map markets for but Kalshi doesn't --
            # idempotent, and a no-op when set_best_of above already resolved
            # this match from the title.
            max_map_by_slug: dict[str, int] = {}
            for row in map_rows:
                max_map_by_slug[row["event_slug"]] = max(
                    max_map_by_slug.get(row["event_slug"], 0), row["map_number"]
                )
            for slug, match_id in match_id_by_slug.items():
                if match_id is not None and slug in max_map_by_slug:
                    market_catalog_cs2.backfill_best_of(session, match_id, max_map_by_slug[slug])

            session.commit()
            log.info(
                "polymarket cs2: %d/%d matches matched (%d winner, %d map, %d total, %d handicap rows)",
                matched, len(match_id_by_slug),
                len(winner_rows), len(map_rows), len(total_maps_rows), len(handicap_rows),
            )
        finally:
            session.close()


def run_full_refresh_cs2():
    """REAL BUG this fixes (found live 2026-08-02 via /health-check: "782 active
    cs2 markets but the newest price snapshot is 189h old"): the Kalshi PRICE
    refresh used to run LAST, behind refresh_cs2_matches(), whose HLTV/Liquipedia
    sources are Cloudflare-gated and now hang or fail. One blocked scrape
    therefore silently starved the whole sport of price updates -- 782 markets
    that DO carry real quotes went ~8 days stale, so cs2 produced no edges and no
    alerts, while valorant (identical structure, working scraper) stayed current.

    Two changes: prices go FIRST (they're the time-sensitive part, and the market
    upsert can create its own fallback match rows from Kalshi's team names, so it
    doesn't depend on the scrape), and each stage is isolated so a failing one
    can't starve the others. Note the scrape can HANG rather than raise, which
    try/except alone wouldn't survive -- ordering is what actually protects the
    prices here.

    HONEST CAVEAT (measured, don't assume this is the whole fix):
    refresh_kalshi_cs2_markets() is ITSELF slow -- it did not finish within 2
    minutes when run alone, so ordering may not be sufficient on its own. It
    crawls 4 Kalshi series and then upserts ~780 markets under the write lock.
    If cs2 prices are STILL stale after this ships, the next thing to measure is
    where that time actually goes (Kalshi pagination vs the per-event
    find_or_create_upcoming_match work), rather than assuming the scrape."""
    # Polymarket runs FIRST, ahead of even the Kalshi price step, by the same
    # reasoning this docstring already established for prices-before-scrape:
    # it is a single listing fetch (fast, not Cloudflare-gated), whereas
    # refresh_kalshi_cs2_markets is measured NOT to finish inside 2 minutes
    # and refresh_cs2_matches can hang outright rather than raise -- which
    # try/except cannot survive, so ORDER is the only real protection a step
    # has. Putting the cheap, reliable step first costs the others nothing.
    # RATINGS MOVED AHEAD OF THE SCRAPE 2026-08-11. This function already
    # protected PRICES from a hanging refresh_cs2_matches, but left ratings
    # behind it -- so the same hang would still leave every cs2 team unrated and
    # the whole sport unpriced. That is not hypothetical: it is exactly what
    # happened to valorant today (36 priced rows -> 0, see poller_valorant.py).
    # Ratings train from the historical cache plus CS2Match rows already in the
    # DB, so they never needed this cycle's scrape.
    for step in (refresh_polymarket_cs2_markets, refresh_kalshi_cs2_markets,
                 refresh_cs2_ratings, refresh_cs2_matches):
        try:
            step()
        except Exception:
            log.exception("cs2 refresh step %s failed; continuing", step.__name__)
    # Roster-change scrape removed 2026-07-23: the informational "Wait" badge
    # it fed was retired for esports (no post-roster-change accuracy penalty
    # found -- see scripts/calibrate_cs2_roster_window.py), so there's nothing
    # left to display and no reason to hit Liquipedia every cycle for it.

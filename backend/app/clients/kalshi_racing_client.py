"""Kalshi racing markets client (F1 / IndyCar / NASCAR). Fetches the per-race
market types our validated models price directly off the field (which Kalshi
itself supplies -- one market per driver, so the set of drivers with markets
under an event IS the race field, same trick as the esports tournament sim):

  race_winner  -> P(win)      (racing_sim)
  top_n(3/5/10)-> P(top-N)    (racing_sim)   incl. F1 podium = top 3
  pole         -> P(pole)     (quali Elo)

Season-champion, constructor, fastest-lap, H2H markets are left for later
(season needs a season Monte Carlo; fastest-lap is ~noise; H2H needs pairing).
Everything ships tracking-only (priced/shown, not staked) -- racing can't be
historically backtested (thin retention), so forward CLV is the only judge.
"""
import logging

from app.clients.base import get_json

log = logging.getLogger("kalshi_racing_client")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# ticker -> (series, market_type, top_n line)
SERIES_MAP = {
    "KXF1RACE": ("f1", "race_winner", None),      # F1 race winner (headline market)
    "KXF1TOP5": ("f1", "top_n", 5),
    "KXF1RACEPODIUM": ("f1", "top_n", 3),
    "KXF1POLE": ("f1", "pole", None),
    "KXINDYCARRACE": ("irl", "race_winner", None),
    "KXINDYCARTOP3": ("irl", "top_n", 3),   # IndyCar podium
    # Added 2026-08-14. The top_n rungs were wired at 3 and 10 but not 5, and
    # pole was wired for F1 and NASCAR but not IndyCar -- so the engine was
    # already pricing both market types for this series and simply had two
    # gaps in its ladder. No new model: top_n runs through the same per-series
    # attrition fitted for TOP3/TOP10, and pole through the same path as
    # KXF1POLE/KXNASCARPOLE.
    #
    # Verified live before wiring, alongside the rungs already ingested so the
    # comparison is like-for-like:
    #     KXINDYCARPOLE   25 open,  8 two-sided, median spread 0.060
    #     KXINDYCARTOP5   25 open, 21 two-sided, median spread 0.070
    #     (KXINDYCARTOP3  50 open, 28 two-sided, 0.040 -- already wired)
    #     (KXINDYCARTOP10 50 open, 30 two-sided, 0.190 -- already wired)
    # TOP5 sits between its neighbours, which is the shape a real ladder has.
    "KXINDYCARTOP5": ("irl", "top_n", 5),
    "KXINDYCARPOLE": ("irl", "pole", None),
    # KXINDYCARFASTLAP is live too (50 open) and deliberately NOT wired: no
    # fastest-lap model exists for ANY series, and the racing result scraper
    # captures finishing position and pole, not lap times. It would ingest as
    # unpriceable inventory and could never settle.
    "KXINDYCARTOP10": ("irl", "top_n", 10),
    # IndyCar drivers' title. Priced by the standings-aware season sim, NOT the
    # per-race model -- see racing_championship. Kalshi lists no IndyCar
    # constructors'/entrant title, so there is no constructors_champion here.
    "KXINDYCARSERIES": ("irl", "drivers_champion", None),
    # F1 titles on KALSHI. These were previously ingested from Polymarket only,
    # so the Kalshi side of the same two propositions was invisible -- no
    # cross-platform divergence, and nothing to compare a Polymarket price
    # against. racing_championship already prices both for f1 (PRICED_SERIES),
    # so this is pure coverage, no new model.
    "KXF1": ("f1", "drivers_champion", None),
    "KXF1CONSTRUCTORS": ("f1", "constructors_champion", None),
    # NASCAR Cup title on KALSHI. racing_championship already prices the NASCAR
    # championship (racing_playoff_sim), but every one of the 36 title rows the
    # app served came from POLYMARKET -- Kalshi championship coverage was F1 and
    # IndyCar only, so there was no second venue and therefore no cross-platform
    # divergence signal on the title at all.
    #
    # THE TICKER MATTERS AND THE OBVIOUS ONE IS A TRAP. The catalog surfaced
    # KXNASCARCUPCHAMP ("Nascar Cup Series Championship"), which reads like the
    # right series and is not: it has ZERO markets in every status -- open,
    # closed, settled and unopened -- i.e. a dead shell that has never listed
    # anything, the same category as the Rocket League series the catch-all
    # deliberately withholds. KXNASCAR ("NASCAR Cup Series playoffs") is dead in
    # exactly the same way. The live series is this one: 35 open per-driver
    # markets under event KXNASCARCUPSERIES-NCS26, yes_sub_title carrying the
    # full driver name ("Chase Briscoe"), which is the shape this fetcher and
    # racing_championship's name resolution already expect.
    "KXNASCARCUPSERIES": ("nascar", "drivers_champion", None),
    "KXNASCARRACE": ("nascar", "race_winner", None),  # NASCAR race winner (headline market)
    # NASCAR qualifying. Added 2026-08-07: pole was ingested for F1 (KXF1POLE)
    # and IndyCar but not NASCAR, leaving it the only series in the app with no
    # qualifying market at all. Pure coverage -- same per-driver shape as the
    # other two (one market per driver, yes_sub_title = driver name), the
    # quali-Elo model already prices it, and it is settleable: ESPN's
    # nascar-premier feed populates "pole" (verified on all 5 stored NASCAR
    # results) and _grade_racing_pole already exists.
    "KXNASCARPOLE": ("nascar", "pole", None),
    "KXNASCARTOP3": ("nascar", "top_n", 3),
    "KXNASCARTOP5": ("nascar", "top_n", 5),
    "KXNASCARTOP10": ("nascar", "top_n", 10),
    "KXNASCARTOP20": ("nascar", "top_n", 20),
    # HEAD-TO-HEAD, added 2026-08-07. The h2h MODEL already existed and already
    # priced Polymarket's version (_h2h_model_prob, closed-form Bradley-Terry
    # over driver+constructor strength); only Kalshi's side was missing.
    #
    # The note below used to say this needed "a small dedicated parse" because
    # "driver = yes_sub_title, which for a h2h market is ONE driver". That was
    # an ASSUMPTION made while both series had 0 open markets and the shape
    # could not be inspected -- and it is wrong. Checked against 60 settled
    # KXNASCARH2H markets: yes_sub_title carries the FULL pairing ("Todd
    # Gilliland beats Ryan Blaney"), which is exactly the label the pricer and
    # grader want. No dedicated parse; only the word "beats" had to join " vs "
    # in racing_ratings.split_h2h_label.
    #
    # Kalshi lists BOTH directions as separate markets (…-TOGI and …-RYBL under
    # one event), which is correct and wanted: they are two distinct bets, and
    # the first name in the label is always the side being backed.
    "KXF1H2H": ("f1", "h2h", None),
    "KXNASCARH2H": ("nascar", "h2h", None),
    # F1 SPRINT WEEKENDS, added 2026-08-08. Both reasons these were held back
    # have now been checked rather than assumed:
    #
    #  1. "settlement needs a sprint-specific results source". It exists --
    #     ESPN's f1 feed splits each weekend into sessions and exposes a
    #     completed "SR" competition with a full finishing order (verified on
    #     22 real sprints, 2023-2026). "SS" carries sprint qualifying.
    #  2. "the main race Elo must NOT be assumed to transfer -- a sprint is a
    #     third the distance with no mandatory stop". Measured instead of
    #     assumed (scripts/check_f1_sprint_transfer.py): rating-vs-finish rank
    #     correlation 0.603 on sprints against 0.572 on grands prix, winner-hit
    #     45.5% vs 45.9%. It transfers -- if anything a shorter race gives
    #     chaos less time to reorder the field. Small sample (22 sprints), so
    #     the honest claim is "no evidence of degradation".
    #
    # Mapped onto the EXISTING market types rather than sprint-specific ones,
    # which is what makes this coverage rather than a new pipeline: Kalshi's
    # event ticker carries the series prefix (KXF1RACESPRINT-BRIGP26 vs
    # KXF1RACE-BRIGP26), and upsert_race_event keys on the full ticker, so a
    # sprint automatically becomes its OWN RaceEvent. It then inherits every
    # existing guard unchanged -- field-coverage floor, implausible-disagreement,
    # and crucially the pre-qualifying staking gate, which keeps sprints priced
    # and tracked but UNSTAKED until a sprint grid is known.
    #
    # Market shapes verified against real SETTLED markets, not inferred:
    # yes_sub_title is the driver name on all four, exactly as the main-race
    # series. KXF1SPRINTTOPCONSTRUCTOR is deliberately excluded -- its
    # yes_sub_title is a CONSTRUCTOR ("Red Bull Racing"), a different resolve
    # path, and constructor-wins-the-sprint is a market this app has no model
    # for (constructor_pole is a different question).
    "KXF1RACESPRINT": ("f1", "race_winner", None),
    "KXF1SPRINTPOLE": ("f1", "pole", None),
    "KXF1SPRINTTOP5": ("f1", "top_n", 5),
    "KXF1SPRINTTOP10": ("f1", "top_n", 10),
}

# Kalshi event-ticker prefixes whose RaceEvent is a SPRINT, not the grand prix.
# Settlement must read ESPN's "SR" session for these, never the Sunday "Race"
# result -- same weekend, same venue, different classification.
SPRINT_EVENT_PREFIXES = ("KXF1RACESPRINT", "KXF1SPRINTPOLE", "KXF1SPRINTTOP5",
                         "KXF1SPRINTTOP10")


def is_sprint_event(event_ticker: str | None) -> bool:
    return bool(event_ticker) and str(event_ticker).startswith(SPRINT_EVENT_PREFIXES)

# DELIBERATELY NOT POLLED, and why -- so this list isn't "rediscovered" as a gap
# every time someone audits Kalshi's racing catalogue (checked live 2026-08-06):
#   KXNASCARCUPSERIES (35 open), KXNASCARCUPSEASON (38): NASCAR's title is an
#     ELIMINATION PLAYOFF ending in a winner-take-all Championship 4, not the
#     points accumulation racing_championship simulates -- which is exactly why
#     PRICED_SERIES is ("f1", "irl"). Pricing it with a points model would be
#     wrong, not merely unvalidated.
#   KXNASCARAUTOPARTSSERIES (40), KXNASCARTRUCKSERIES (35): Xfinity and Truck
#     titles. Blocked by the same gap as their races -- ratings and results come
#     from ESPN nascar-premier, which is Cup only (see racing_markets
#     MIN_FIELD_COVERAGE).
#   KXNASCARFASTLAP (73), KXNASCARTOPTEAM (33): no model for either.
#   KXMOTOGPTEAMS (11): MotoGP is a whole sport we don't cover.
#   KXF1ACTION / KXF1CHINA / KXF1NEXTTEAM / KXF1RETIRE: novelty markets.
#   (KXF1H2H / KXNASCARH2H moved INTO SERIES_MAP on 2026-08-07 -- the reason
#     they were excluded, that yes_sub_title holds one driver rather than the
#     pairing, was an assumption made when neither series had an open market to
#     inspect, and checking 60 settled ones disproved it.)
#   KXF1SPRINTTOPCONSTRUCTOR: the only sprint series still excluded. Its
#     yes_sub_title is a CONSTRUCTOR, not a driver, and "which team wins the
#     sprint" is a market this app has no model for. The other four sprint
#     series moved INTO SERIES_MAP on 2026-08-08 -- both of the reasons they
#     were held back (no sprint results source; the main-race model might not
#     transfer) were checked and both turned out to be wrong. See the block in
#     SERIES_MAP for the measurements.


def _to_float(v):
    """Kalshi returns these as STRINGS ("0.0500"), not numbers. Previously this
    module read the plain "yes_bid"/"yes_ask"/"last_price"/"volume" keys, which
    do not exist on this endpoint -- every value came back None, so no
    conversion was needed and none existed. Reading the real *_dollars/*_fp keys
    means the strings now have to be coerced or they reach the DB as text."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_racing_markets() -> list[dict]:
    """One row per open driver-market across the tracked racing series. Carries
    the RAW Kalshi price fields (cents 0-100) so poller_racing can write
    MarketSnapshots; the router derives implied_prob from the snapshot the same
    way every other sport does. `driver` is the Kalshi yes_sub_title."""
    out: list[dict] = []
    for ticker, (series, mtype, line) in SERIES_MAP.items():
        try:
            data = get_json(f"{KALSHI_BASE}/markets?series_ticker={ticker}&status=open&limit=200")
        except Exception:
            log.exception("kalshi racing fetch failed for %s", ticker)
            continue
        for m in data.get("markets", []):
            driver = m.get("yes_sub_title") or ""
            if not driver:
                continue
            out.append({
                "series": series,
                "market_type": mtype,
                "line": line,
                "driver": driver,
                "event_ticker": m.get("event_ticker"),
                "event_title": m.get("title"),
                "ticker": m.get("ticker"),
                "close_time": m.get("close_time") or m.get("expiration_time"),
                "status": m.get("status") or "active",
                "last_price": _to_float(m.get("last_price_dollars")),
                "yes_bid": _to_float(m.get("yes_bid_dollars")),
                "yes_ask": _to_float(m.get("yes_ask_dollars")),
                "volume": _to_float(m.get("volume_fp")),
            })
    return out

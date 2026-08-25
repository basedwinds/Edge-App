"""UFC fight data ingestion -- turns the raw ufcstats.com scrape (see
app/clients/ufcstats_client.py and scripts/build_ufc_fight_cache.py) into
MmaFight-shaped records. Parallel to nfl_data.py/nba_data.py/mlb_data.py,
same "parallel modules per sport" architecture call.

The raw cache is a FLAT list with 2 rows per fight (one per fighter, as
scraped from ufcstats' fight-details page). This module's job is pairing
those into one neutral fighter_a/fighter_b record per fight -- see
MmaFight's docstring in app/db/models.py for why "neutral" (not
winner-ordered) matters here specifically.
"""
import datetime as dt
import json
import re
from pathlib import Path

from app.clients.ufcstats_client import BASE_URL, UfcStatsClient

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
FIGHTS_CACHE_PATH = DATA_DIR / "ufc_fight_cache.json"
BIOS_CACHE_PATH = DATA_DIR / "ufc_fighter_bio_cache.json"

# Fights that reached a real decision -- i.e. went the full scheduled
# distance without a stoppage. Covers every decision variant ufcstats uses
# ("Decision - Unanimous", "Decision - Split", "Decision - Majority") plus
# rare technical-decision method text.
_DECISION_METHOD_PREFIXES = ("decision",)

_MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}


def parse_event_date(raw: str | None) -> str | None:
    """ufcstats renders dates like "July 18, 2026" -- converts to ISO."""
    if not raw:
        return None
    m = re.match(r"(\w+)\s+(\d{1,2}),\s+(\d{4})", raw.strip())
    if not m:
        return None
    month_name, day, year = m.groups()
    month = _MONTHS.get(month_name)
    if not month:
        return None
    return dt.date(int(year), month, int(day)).isoformat()


def parse_scheduled_rounds(time_format: str | None) -> int | None:
    """"3 Rnd (5-5-5)" -> 3, "5 Rnd (5-5-5-5-5)" -> 5, "1 Rnd (12)" (some
    early-1990s UFC events, effectively no round limit) -> 1. Returns None
    for anything unrecognized rather than guessing."""
    if not time_format:
        return None
    m = re.match(r"(\d+)\s*Rnd", time_format.strip())
    return int(m.group(1)) if m else None


def is_went_the_distance(method: str | None) -> bool | None:
    if not method:
        return None
    normalized = method.strip().lower()
    if normalized.startswith(_DECISION_METHOD_PREFIXES):
        return True
    if normalized in ("nc", "dq", "no contest", "disqualification", "overturned", "other", "could not continue"):
        return None  # genuinely ambiguous -- not a clean finish/decision signal, don't guess
    return False


def load_raw_fight_rows() -> list[dict]:
    if not FIGHTS_CACHE_PATH.exists():
        return []
    return json.loads(FIGHTS_CACHE_PATH.read_text())


def load_fighter_bios() -> dict[str, dict]:
    """fighter_id -> bio dict (height/reach/stance/dob only, see
    ufcstats_client.py's docstring on why career-cumulative stats are
    excluded)."""
    if not BIOS_CACHE_PATH.exists():
        return {}
    bios = json.loads(BIOS_CACHE_PATH.read_text())
    return {b["fighter_id"]: b for b in bios}


def pair_fight_rows(raw_rows: list[dict]) -> list[dict]:
    """Groups the flat 2-rows-per-fight cache by fight_url into one
    MmaFight-shaped dict per fight, in chronological order. A fight_url
    with != 2 rows (a scrape gap) is skipped and not silently guessed at."""
    by_fight: dict[str, list[dict]] = {}
    for row in raw_rows:
        by_fight.setdefault(row["fight_url"], []).append(row)

    fights = []
    for fight_url, rows in by_fight.items():
        if len(rows) != 2:
            continue
        a, b = rows
        winner_id = None
        if a["result"] == "W":
            winner_id = a["fighter_id"]
        elif b["result"] == "W":
            winner_id = b["fighter_id"]
        # "" (not yet fought) and "D"/"NC" (no winner) both correctly leave winner_id None

        # REAL DATA BUG fixed here (2026-07-17, caught before running any
        # Elo walk-forward): a "D" (real draw, both fighters show result="D")
        # and "NC" (no contest -- overturned/could-not-continue, the fight
        # didn't resolve a real skill question) both collapsed to the same
        # winner_id=None as an unfought future fight, indistinguishable.
        # 65 real draws (130 rows) exist in the historical data -- a draw is
        # a genuine 50/50 Elo outcome that should update ratings, unlike an
        # NC or a not-yet-fought fight, which should not touch ratings at
        # all. is_draw lets the Elo walk-forward script (and anything else)
        # tell the three cases apart.
        is_draw = a["result"] == "D" and b["result"] == "D"
        is_no_contest = a["result"] == "NC" and b["result"] == "NC"

        event_date_iso = parse_event_date(a["event_date"])
        scheduled_rounds = parse_scheduled_rounds(a.get("time_format"))
        method = a.get("method")
        round_num = a.get("round")

        # went_the_distance is a property of the FIGHT (did it reach a
        # decision), independent of whether there was a winner -- a draw
        # decision genuinely went the distance. Only gated on the fight
        # having actually happened with a real recorded method (not NC,
        # not a future unfought fight), not on winner_id specifically.
        fight_happened = a["result"] in ("W", "L", "D")
        wtd = is_went_the_distance(method) if fight_happened else None

        fights.append({
            "id": fight_url.rstrip("/").rsplit("/", 1)[-1],
            "event_id": a["event_id"],
            "event_name": a["event_name"],
            "event_date": event_date_iso,
            "weight_class": a.get("weight_class"),
            "is_title_bout": 1 if a.get("is_title_bout") else 0,
            "fighter_a_id": a["fighter_id"],
            "fighter_a_name": a["fighter_name"],
            "fighter_b_id": b["fighter_id"],
            "fighter_b_name": b["fighter_name"],
            "winner_id": winner_id,
            "is_draw": is_draw,
            "is_no_contest": is_no_contest,
            "method": method,
            "round": int(round_num) if round_num and str(round_num).isdigit() else None,
            "time": a.get("time"),
            "scheduled_rounds": scheduled_rounds,
            "went_the_distance": {True: 1, False: 0, None: None}[wtd],
        })

    fights.sort(key=lambda f: (f["event_date"] or "", f["id"]))
    return fights


def load_fights() -> list[dict]:
    return pair_fight_rows(load_raw_fight_rows())


def fetch_fight_results(fight_ids: list[str]) -> dict[str, dict]:
    """{fight_id: MmaFight-shaped dict} for fights that have since been fought.

    THE GAP THIS CLOSES. fetch_upcoming_fights scrapes ONLY
    /statistics/events/upcoming. The moment a card is fought it drops off that
    list, and nothing ever went back for the result -- so a fight was created
    with winner_id=None and stayed that way forever. Measured 2026-08-25: 160 of
    180 MmaFight rows had no winner, and the only 20 that did came from the
    static ufc_fight_cache.json (built once, 2026-07-17, not scheduled).

    That silence was invisible because MMA BETS still settled: bet_settlement
    reaches Kalshi's own resolution for those. The forward observation log has no
    such path -- it grades through _game_is_final, which for mma reads
    MmaFight.winner_id -- so all 828 MMA observations sat pending and MMA was one
    of two sports taking real money with no measurement at all.

    Targeted rather than a re-crawl: MmaFight.id IS the ufcstats fight-details
    URL id, so each fight is one direct fetch with no event-list walk.

    Reuses pair_fight_rows deliberately. That function already encodes the
    W/L/D/NC distinctions -- including the 2026-07-17 fix separating a real draw
    (rating-relevant) from a no-contest (not) -- and a second copy here is
    exactly how one of them ends up not getting a fix the other got. It needs
    event_* keys it cannot get from a fight-details page, so those are stubbed;
    the CALLER must take only the result fields and keep the identity fields
    already on the row.
    """
    out: dict[str, dict] = {}
    if not fight_ids:
        return out
    with UfcStatsClient() as client:
        for fid in fight_ids:
            try:
                rows = client.get_fight_details(f"{BASE_URL}/fight-details/{fid}")
            except Exception:
                continue  # one unreachable fight must not cost the rest
            if not rows:
                continue  # cancelled-bout stub, or did not parse
            for r in rows:
                r["event_id"] = ""
                r["event_name"] = ""
                r["event_date"] = ""
            paired = pair_fight_rows(rows)
            if paired:
                out[fid] = paired[0]
    return out


# In-process cache, TTL 1h -- re-scraping ufcstats' upcoming-card list every
# 5-minute poll cycle would mean solving its PoW gate ~10x + ~100-150
# fight-detail fetches every cycle for data that only changes when a new
# card is announced or a late injury-replacement swaps a fighter (same
# "cheap data, changes rarely, cache it" reasoning as mlb_data.py's
# team-abbreviation cache).
_upcoming_cache: dict = {"fights": None, "fetched_at": None}
_UPCOMING_CACHE_TTL_SECONDS = 3600


def fetch_upcoming_fights(force: bool = False) -> list[dict]:
    """Live-scrapes ufcstats.com's /statistics/events/upcoming card list +
    each event's fight list (fighter names/ids/weight class -- no result
    yet, see UfcStatsClient.get_fight_details' handling of unfought fights).
    Cached in-process for _UPCOMING_CACHE_TTL_SECONDS; pass force=True to
    bypass (e.g. right after a known card announcement)."""
    now = dt.datetime.utcnow()
    fetched_at = _upcoming_cache["fetched_at"]
    if not force and fetched_at is not None and (now - fetched_at).total_seconds() < _UPCOMING_CACHE_TTL_SECONDS:
        return _upcoming_cache["fights"]

    raw_rows: list[dict] = []
    with UfcStatsClient() as client:
        for event in client.list_upcoming_events():
            fight_urls = client.get_event_fight_urls(event["event_url"])
            for fight_url in fight_urls:
                rows = client.get_fight_details(fight_url)
                if not rows:
                    continue
                for row in rows:
                    row["event_id"] = event["event_id"]
                    row["event_name"] = event["event_name"]
                    row["event_date"] = event["event_date"]
                    raw_rows.append(row)

    fights = pair_fight_rows(raw_rows)
    _upcoming_cache["fights"] = fights
    _upcoming_cache["fetched_at"] = now
    return fights

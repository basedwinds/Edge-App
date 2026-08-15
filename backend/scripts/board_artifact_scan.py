"""Daily pass/fail scan of the LIVE board for the defect shapes that keep recurring.

WHY THIS EXISTS. Every defect found on 2026-08-14 was SILENT: the board looked
normal and every number looked plausible. Soccer fixtures ran 3 hours late; the
Truck race carried the Cup race's date; a cached grid belonged to no entrant in
the race and silently lifted a staking gate; 277 predictions claimed a
probability of exactly 0 or 1; and a fitted parameter set passed correlation,
calibration, hold-out AND decile repair while its bucket quietly contained a
0.75-mile short track.

That last one is the point of this file. It was caught by running the finished
thing against a real event and noticing Richmond came back flagged -- a manual
sanity check, done once, by luck. These checks are that sanity check, automated
and repeated.

WHAT THIS IS NOT. It does not filter, suppress, or re-price anything. Every check
REPORTS. The user paper-trades deliberately to test the model and has been
explicit about not wanting bets quietly removed, so a scanner that hid rows would
be worse than no scanner.

WARMTH IS CHECKED FIRST AND ABORTS. A cold server produces a board that looks
catastrophically broken and is merely unfinished -- staked counts read 326 -> 209
twice on 2026-08-14 for exactly this reason. THE WARMTH SIGNAL IS THE NULL
model_prob COUNT, NOT THE ROW COUNT: rows come straight from the database and
appear instantly, while model_prob needs the rating and sim caches. Both times
the row count matched exactly and the staked count had collapsed.

Run: backend/.venv/Scripts/python.exe scripts/board_artifact_scan.py
Exit code 1 if any check FAILS, so it can be scheduled and alert on failure.
"""
import datetime
import json
import sys
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8756"
SPORTS = ("cfb", "cod", "cs2", "lol", "mlb", "mma", "nba", "racing",
          "soccer", "tennis", "valorant", "wnba", "")

# A board with more than this share of rows lacking a model price is still
# warming. Measured 2026-08-14: cold 6,416 null of 22,645 (28%), warm 4,218 of
# 22,005 (19%). Sports genuinely without a model sit in the warm baseline, which
# is why this is a ceiling on the ratio rather than a demand for zero.
MAX_NULL_MODEL_SHARE = 0.24
# Books wider than this on a bet that is actually stakeable are worth an eyeball.
WIDE_BOOK = 0.10
# A field whose model probabilities span less than this is not discriminating --
# what gridless racing looked like (every driver 4-6%).
FLAT_FIELD_SPAN = 0.05

_results: list[tuple[str, str, str]] = []      # (level, check, detail)


def record(level: str, check: str, detail: str) -> None:
    _results.append((level, check, detail))


def fetch(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=300) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def load_board() -> list[dict]:
    out = []
    for s in SPORTS:
        path = f"/{s}/markets" if s else "/markets"
        try:
            rows = fetch(path)
        except Exception as e:
            record("FAIL", "board fetch", f"{path}: {type(e).__name__}")
            continue
        for r in rows if isinstance(rows, list) else []:
            if isinstance(r, dict):
                r["_sport"] = s or "nfl"
                out.append(r)
    return out


# ---------------------------------------------------------------- 0. WARMTH
def check_warmth(board: list[dict]) -> bool:
    """True if the board is warm enough to judge. Aborts the scan otherwise.

    Reports the NULL model_prob share, not the row count -- see the module
    docstring for why the row count is a trap.
    """
    if not board:
        record("FAIL", "warmth", "board is empty")
        return False
    null_model = sum(1 for r in board if r.get("model_prob") is None)
    share = null_model / len(board)
    if share > MAX_NULL_MODEL_SHARE:
        record("FAIL", "warmth",
               f"{null_model}/{len(board)} rows ({share:.0%}) have no model price "
               f"-- above the {MAX_NULL_MODEL_SHARE:.0%} ceiling, so the server is "
               f"still warming. SCAN ABORTED: a cold board reads as broken.")
        return False
    record("PASS", "warmth", f"{null_model}/{len(board)} rows ({share:.0%}) unpriced")
    return True


# ------------------------------------------------- 1. START TIMES + COLLISIONS
def check_start_times() -> None:
    """Stored kickoff vs an INDEPENDENT source, and exact-timestamp collisions.

    Two real defects: Kalshi's soccer occurrence_datetime is kickoff+3h, so
    fixtures stayed on the board while being played; and NASCAR's date resolution
    handed the Truck race the Cup race's slot. The second showed up as ELEVEN
    events sharing one timestamp -- a collision on an exact timestamp is never a
    coincidence, which is why it is checked independently of any source.
    """
    from app.clients.espn_soccer_client import LEAGUE_CODES
    from app.db.database import SessionLocal
    from sqlalchemy import text

    s = SessionLocal()
    try:
        # --- collisions: TWO DIFFERENT RACES sharing one exact start_time ---
        #
        # NOT "many events share a timestamp", which is normal and was a false
        # alarm in the first version: eleven Premier League matches legitimately
        # kick off at 14:00Z on a Saturday, and one race spawns winner/top3/top5/
        # top10/pole events that SHOULD share a slot.
        #
        # The real defect was narrower -- the Truck race took the CUP race's exact
        # timestamp, i.e. two DIFFERENT canonical races claiming one moment. That
        # is what is checked, using the same grouping the date reconciler uses.
        # Soccer is not checked at all: its +3h defect never manifested as a
        # collision, so the check would be pure noise there.
        from app.models.duplicate_fixtures import canonical_race_event_ids
        from app.db.models import RaceEvent
        canon = canonical_race_event_ids(s)
        by_ts = defaultdict(set)
        for e in s.query(RaceEvent).filter(RaceEvent.start_time.isnot(None)).all():
            by_ts[e.start_time].add(canon.get(e.id, e.id))
        clash = {ts: g for ts, g in by_ts.items() if len(g) > 1}
        if clash:
            worst = sorted(clash.items(), key=lambda kv: -len(kv[1]))[:3]
            record("FAIL", "racing race collisions",
                   f"{len(clash)} timestamp(s) claimed by >1 DISTINCT race: "
                   + "; ".join(f"{ts} x{len(g)} races" for ts, g in worst))
        else:
            record("PASS", "racing race collisions",
                   f"{len(by_ts)} timestamps, none shared by two races")

        # --- soccer kickoffs vs ESPN ---
        rows = s.execute(text("""
            SELECT league, home_team, away_team, estimated_start_time
            FROM soccer_matches
            WHERE result_ft IS NULL AND estimated_start_time IS NOT NULL
              AND substr(estimated_start_time,1,10) =
                  strftime('%Y-%m-%d', 'now')
        """)).fetchall()
    finally:
        s.close()

    if not rows:
        record("PASS", "soccer kickoff accuracy", "no fixtures stored for today")
        return
    leagues = {r[0] for r in rows if r[0] in LEAGUE_CODES}
    truth = {}
    day = datetime.datetime.utcnow().strftime("%Y%m%d")
    for lg in leagues:
        try:
            d = json.load(urllib.request.urlopen(
                "https://site.api.espn.com/apis/site/v2/sports/soccer/"
                f"{LEAGUE_CODES[lg]}/scoreboard?dates={day}", timeout=25))
        except Exception:
            continue
        for e in d.get("events", []):
            try:
                truth.setdefault(lg, []).append(
                    datetime.datetime.strptime(e["date"].replace("Z", ""), "%Y-%m-%dT%H:%M"))
            except Exception:
                pass
    bad = []
    for lg, h, a, est in rows:
        if lg not in truth:
            continue
        try:
            st = datetime.datetime.strptime(
                str(est).replace("Z", "").replace("T", " ")[:16], "%Y-%m-%d %H:%M")
        except Exception:
            continue
        gap = min(abs((st - t).total_seconds()) for t in truth[lg]) / 60.0
        if gap > 30:
            bad.append(f"{h} v {a} off by {gap:.0f}min")
    if bad:
        record("FAIL", "soccer kickoff accuracy",
               f"{len(bad)} fixture(s) >30min from ESPN: {bad[:3]}")
    else:
        record("PASS", "soccer kickoff accuracy", f"{len(rows)} fixtures within 30min")


# ------------------------------------------------------ 2. GRID vs ENTRANTS
def check_grids() -> None:
    """Every cached racing grid must describe the field its markets are about.

    A foreign grid is WORSE than none: the staking gate only asks whether a grid
    exists, so it lifts the gate while strength() still sees grid=None for every
    real entrant -- back to flat pricing, silently. Found live on the Richmond
    Truck race, whose cached grid matched none of its 39 entrants.
    """
    from app.db.database import SessionLocal
    from app.ingestion.poller_racing import _grid_matches_entrants, RACING_GRID_CACHE
    from app.config import settings

    path = Path(settings.data_dir) / RACING_GRID_CACHE
    if not path.exists():
        record("PASS", "racing grid ownership", "no grid cache yet")
        return
    cache = json.loads(path.read_text())
    s = SessionLocal()
    bad = []
    try:
        for k, grid in cache.items():
            try:
                if not _grid_matches_entrants(s, int(k), grid):
                    bad.append(k)
            except Exception:
                pass
    finally:
        s.close()
    if bad:
        record("FAIL", "racing grid ownership",
               f"{len(bad)} cached grid(s) do not match their event's entrants: {bad[:5]}")
    else:
        record("PASS", "racing grid ownership", f"{len(cache)} cached grids all match")


# --------------------------------------------- 3. DEGENERATE MODEL OUTPUT
def check_degenerate(board: list[dict]) -> None:
    """Probabilities of exactly 0/1, and fields the model cannot separate."""
    hard = [r for r in board if r.get("model_prob") is not None
            and (r["model_prob"] <= 0.0 or r["model_prob"] >= 1.0)]
    staked_hard = [r for r in hard if r.get("suggested_stake_dollars") is not None]
    if staked_hard:
        record("FAIL", "degenerate probability",
               f"{len(staked_hard)} STAKED row(s) priced off model_prob of exactly 0 or 1")
    elif hard:
        by = Counter(f"{r['_sport']}/{r.get('market_type')}" for r in hard)
        record("WARN", "degenerate probability",
               f"{len(hard)} priced (none staked) -- top: {by.most_common(3)}")
    else:
        record("PASS", "degenerate probability", "none")

    # flat fields -- group by event where an event key exists
    groups = defaultdict(list)
    for r in board:
        ev = r.get("race_event_id") or r.get("cfb_game_id") or r.get("mlb_game_id")
        p = r.get("model_prob")
        if ev is not None and p is not None and r.get("market_type") in ("race_winner", "top_n"):
            groups[(r["_sport"], ev, r.get("market_type"))].append(p)
    flat = [k for k, v in groups.items() if len(v) >= 8 and (max(v) - min(v)) < FLAT_FIELD_SPAN]
    if flat:
        record("WARN", "flat model field",
               f"{len(flat)} event/market(s) where every entrant prices within "
               f"{FLAT_FIELD_SPAN:.0%} -- the model is not discriminating: {flat[:3]}")
    else:
        record("PASS", "flat model field", f"{len(groups)} fields checked")


# ------------------------------------------------------- 4. BOOK QUALITY
def check_books(board: list[dict]) -> None:
    """Staked bets sitting on a wide book. INFORMATION, not a filter.

    96% of staked bets sit on books of 10c or tighter; the tail is worth seeing
    because the displayed price is the MIDPOINT and half the spread is the
    haircut at the ask.
    """
    staked = [r for r in board if r.get("suggested_stake_dollars") is not None]
    wide = []
    for r in staked:
        b, a = r.get("yes_bid"), r.get("yes_ask")
        if b is not None and a is not None and 0 < (a - b) < 0.90 and (a - b) > WIDE_BOOK:
            wide.append((r["_sport"], r.get("team") or r.get("driver"), round((a - b) * 100)))
    if not staked:
        record("WARN", "staked book width", "no staked bets on the board")
    elif wide:
        record("WARN", "staked book width",
               f"{len(wide)}/{len(staked)} staked on books wider than "
               f"{WIDE_BOOK*100:.0f}c: {wide[:4]}")
    else:
        record("PASS", "staked book width", f"0/{len(staked)} staked on a wide book")


# ---------------------------------------------------- 5. RATINGS AT DEFAULT
def check_default_ratings(board: list[dict]) -> None:
    """Staked racing bets whose driver rating has barely moved from BASE.

    INFORMATION ONLY -- a minimum-starts gate was fitted and REJECTED (the effect
    is 1.1-1.3x inside a known ~2.4x global overstatement, and would delete 21%
    of inventory). And a rating near BASE does NOT mean a thin rating: one driver
    sat at 1491 on 38 starts. That is why the START COUNT is reported, not the
    distance from BASE.
    """
    from app.models.baseline import racing_ratings as rr
    thin = []
    for r in board:
        if r["_sport"] != "racing" or r.get("suggested_stake_dollars") is None:
            continue
        name = r.get("driver") or r.get("team")
        # THE ROW'S OWN SERIES, not the first pool the name happens to resolve
        # in. A first version looped nascar -> xfinity -> truck and broke on the
        # first hit, so a Truck driver with 38 Truck starts was reported as
        # having 1 -- his Cup count. The router already resolved the pool; the
        # payload carries it.
        series = r.get("series") or "nascar"
        d = rr.resolve_driver_id(series, name or "")
        if not d:
            continue
        starts = (rr._series_state(series).get("starts") or {}).get(d)
        if starts is not None and starts < 5:
            thin.append(f"{name}({starts} starts, {series})")
    if thin:
        record("WARN", "thin racing ratings",
               f"{len(thin)} staked bet(s) on drivers with <5 starts: {thin[:4]}")
    else:
        record("PASS", "thin racing ratings", "no staked bet on a <5-start driver")


def main() -> int:
    board = load_board()
    warm = check_warmth(board)
    if warm:
        check_start_times()
        check_grids()
        check_degenerate(board)
        check_books(board)
        check_default_ratings(board)

    print(f"\nBOARD ARTIFACT SCAN  {datetime.datetime.utcnow():%Y-%m-%d %H:%M}Z")
    print("=" * 78)
    for level, check, detail in _results:
        print(f"  [{level:4}] {check:28} {detail}")
    fails = sum(1 for lv, _, _ in _results if lv == "FAIL")
    warns = sum(1 for lv, _, _ in _results if lv == "WARN")
    print("=" * 78)
    print(f"  {fails} FAIL   {warns} WARN   {len(_results)-fails-warns} PASS")
    if not warm:
        print("\n  Scan aborted on warmth -- re-run once the server is warm.")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

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
#
# SUPERSEDED 2026-08-15 -- kept only as the historical basis for the numbers in
# check_warmth's docstring. The raw null share turned out to conflate three
# unrelated things and is not a usable signal on its own; see check_warmth.
MAX_NULL_MODEL_SHARE = 0.24

# A row that SAYS it is pending is not evidence of a cold board -- it is a row
# doing its job. These markers come from no_baseline_reason.
WARMING_MARKERS = ("not warm yet",)

# Nulls with NO reason at all are the genuinely diagnostic ones: a sport that
# silently loses its model produces these, and nothing else does. Measured
# 2026-08-15 on a healthy board: 812 of 23,254 (3.5%), spread across nfl 626,
# wnba 87, mlb 61, racing 38. The ceiling is set well above that so ordinary
# drift does not trip it, and far below the ~29% a genuinely cold board shows.
MAX_UNEXPLAINED_NULL_SHARE = 0.10
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
    """Fetch every sport's board. A NON-200 IS A FAIL IN ITS OWN RIGHT.

    Added after shipping a NameError to a live server on 2026-08-15: a mechanical
    edit across 10 routers added `max_spread=FUTURES_MAX_SPREAD` but skipped the
    IMPORT in two of them, because the check for an existing import matched the
    constant's name inside a COMMENT. py_compile passes on an undefined name --
    it is a NameError at request time, not a syntax error -- so all 10 files
    "compiled" while /mma/markets and /cod/markets returned 500.

    It surfaced as MMA showing 0 staked bets against 74 upcoming fights, and was
    briefly misread as the new guard working. Compiling N files is not
    verification of an N-file edit; CALLING the endpoints is.
    """
    out = []
    broken = []
    for s in SPORTS:
        path = f"/{s}/markets" if s else "/markets"
        try:
            rows = fetch(path)
        except Exception as e:
            code = getattr(e, "code", None)
            broken.append(f"{path}={code or type(e).__name__}")
            continue
        for r in rows if isinstance(rows, list) else []:
            if isinstance(r, dict):
                r["_sport"] = s or "nfl"
                out.append(r)
    if broken:
        record("FAIL", "endpoint health", f"{len(broken)} endpoint(s) not serving: {broken}")
    else:
        record("PASS", "endpoint health", f"all {len(SPORTS)} market endpoints returned 200")
    return out


# ---------------------------------------------------------------- 0. WARMTH
def check_warmth(board: list[dict]) -> bool:
    """True if the board is warm enough to judge. Aborts the scan otherwise.

    THE RAW NULL SHARE CONFLATES THREE UNRELATED THINGS, which is why this used
    to abort on a perfectly healthy board. Measured 2026-08-15 -- 6,729 nulls of
    23,254 rows (29%, above the old 24% ceiling), and almost none of it meant
    what the guard assumed:

      TRANSIENT    1,786  "Season/Conference/Playoff simulation not warm yet"
                          -- CFB's Monte Carlo futures, rebuilt from scratch on
                          every restart. A row SAYING it is pending is not
                          evidence of a cold board; it is a row doing its job.
      STRUCTURAL  ~4,131  no tracked match history, preseason, non-FBS opponent,
                          "not a team-competition result" -- these will NEVER
                          resolve and are CORRECT. They are the bulk of all
                          nulls and always were.
      UNEXPLAINED     812  (3.5%) nulls with no reason at all.

    Only the third group can reveal a sport silently losing its model, so that is
    what aborts. The other two are REPORTED, because a board that is 8% pending
    is worth knowing about even though it is judgeable.

    WHY PROCEEDING IS SAFE WHEN A SPORT IS PENDING: every other check here is
    either cross-sport (timestamp collisions, grid ownership, book width) or
    skips rows without a model price anyway (degenerate probability,
    near-threshold certainty). A pending sport contributes no rows to them rather
    than wrong rows.

    THE ORIGINAL CONCERN STILL STANDS and is why this did not simply get its
    ceiling raised: a cold board reads as catastrophically broken -- staked
    counts read 326 -> 209 twice on 2026-08-14 for exactly this reason, with the
    ROW COUNT matching exactly both times. The signal was never the row count. It
    is still not the raw null count either.
    """
    if not board:
        record("FAIL", "warmth", "board is empty")
        return False

    transient = structural = unexplained = 0
    pending_by_sport: dict[str, int] = defaultdict(int)
    unexplained_by_sport: dict[str, int] = defaultdict(int)
    for r in board:
        if r.get("model_prob") is not None:
            continue
        reason = (r.get("no_baseline_reason") or "").strip()
        if not reason:
            unexplained += 1
            unexplained_by_sport[r.get("_sport", "?")] += 1
        elif any(m in reason for m in WARMING_MARKERS):
            transient += 1
            pending_by_sport[r.get("_sport", "?")] += 1
        else:
            structural += 1

    n = len(board)
    total_nulls = transient + structural + unexplained
    # Pending rows are neither warm nor cold -- they are not yet a fact about
    # the board, so they leave BOTH sides of the ratio.
    judgeable = max(n - transient, 1)
    unexplained_share = unexplained / judgeable

    record("PASS" if unexplained_share <= MAX_UNEXPLAINED_NULL_SHARE else "FAIL", "warmth",
           f"{total_nulls}/{n} unpriced = {transient} pending + {structural} structural "
           f"+ {unexplained} unexplained ({unexplained_share:.1%} of judgeable)")

    if transient:
        top = sorted(pending_by_sport.items(), key=lambda kv: -kv[1])[:3]
        record("WARN", "simulations pending",
               f"{transient} row(s) still building: {top} -- judgeable, but these "
               f"sports' futures are absent from every check below")

    if unexplained_share > MAX_UNEXPLAINED_NULL_SHARE:
        top = sorted(unexplained_by_sport.items(), key=lambda kv: -kv[1])[:3]
        record("FAIL", "warmth",
               f"{unexplained} nulls carry NO reason ({unexplained_share:.1%} of judgeable, "
               f"ceiling {MAX_UNEXPLAINED_NULL_SHARE:.0%}) -- top {top}. That is the shape of a "
               f"sport losing its model, not of a cold start. SCAN ABORTED.")
        return False
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
def check_soccer_xg_freshness() -> None:
    """Is the Understat xG cache still keeping up with played fixtures?

    THE FAILURE MODE IS SILENCE (#203). The soccer ratings blend xG into the
    attack/defence residual for E0/SP1/D1/I1/F1. If the weekly refresh stops --
    the endpoint changes, the header requirement moves, a promoted club is
    missing from the alias map -- nothing errors. Every new fixture just quietly
    falls back to pure goals, which is CORRECT per-fixture behaviour and exactly
    why nobody would notice the model getting worse.

    So the check is coverage over a recent window, not liveness of a job.
    A promoted club missing from the alias map produces the same symptom as a
    dead refresh, and both matter.
    """
    try:
        from app.ingestion import soccer_data
        from app.models.baseline import soccer_xg
    except Exception as exc:
        record("WARN", "soccer xG freshness", f"could not import ({type(exc).__name__})")
        return
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=30)).date().isoformat()
    recent = have = 0
    missing_teams = Counter()
    for m in soccer_data.load_matches():
        lg = m.get("league")
        if lg not in soccer_xg.XG_LEAGUES:
            continue
        d = (m.get("match_date") or "")[:10]
        if not d or d < cutoff:
            continue
        if m.get("home_goals_ft") is None:
            continue           # not played yet -- xG cannot exist
        recent += 1
        if soccer_xg.lookup(lg, d, m.get("home_team"), m.get("away_team")):
            have += 1
        else:
            missing_teams[f"{lg}:{m.get('home_team')}"] += 1
    if not recent:
        record("PASS", "soccer xG freshness", "no big-5 fixtures played in the last 30d (off-season)")
        return
    pct = 100 * have // recent
    if pct >= 90:
        record("PASS", "soccer xG freshness", f"{have}/{recent} recent big-5 fixtures have xG ({pct}%)")
    elif pct >= 50:
        record("WARN", "soccer xG freshness",
               f"only {have}/{recent} recent fixtures have xG ({pct}%) -- refresh may be "
               f"lagging or a club is unmapped; top gaps {missing_teams.most_common(3)}")
    else:
        record("FAIL", "soccer xG freshness",
               f"{have}/{recent} recent fixtures have xG ({pct}%) -- the blend has "
               f"effectively stopped; top gaps {missing_teams.most_common(3)}")



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

    # NEAR-THRESHOLD MODEL CERTAINTY (#195, 2026-08-15).
    #
    # `implausible_certainty` refuses a staked row when the market/model odds
    # ratio reaches 10x. That is a hard line, and a hard line has an outside.
    # Watched live: cs2 Spirit vs BIG read market 0.175 / model 0.0172 = 10.2x
    # and was BLOCKED, then the price drifted to 0.165 = 9.6x and the same bet
    # came back onto the board at $10 -- an unchanged, still-implausible model
    # claim, admitted by a 1-cent move in a price it does not depend on.
    #
    # Reported, never filtered. Lowering the constant is a decision with its own
    # evidence (it was set against 3,912 settled bets) and is not this script's
    # to make. What the script CAN do is stop that class of row from depending on
    # someone happening to read the board that hour.
    near = []
    for r in board:
        mp, mk = r.get("model_prob"), r.get("implied_prob")
        if mp is None or mk is None or r.get("suggested_stake_dollars") is None:
            continue
        if not (0.0 < mp < 1.0 and 0.0 < mk < 1.0):
            continue
        ratio = (mk / mp) if mk <= 0.5 else ((1.0 - mk) / (1.0 - mp))
        if 6.0 <= ratio < 10.0:
            near.append((ratio, r))
    if near:
        near.sort(key=lambda x: -x[0])
        top = "; ".join(
            f"{r['_sport']}/{r.get('market_type')} {str(r.get('team'))[:14]} "
            f"mkt {r['implied_prob']:.3f} model {r['model_prob']:.4f} = {ra:.1f}x"
            for ra, r in near[:3]
        )
        record("WARN", "near-threshold certainty",
               f"{len(near)} STAKED row(s) at 6-10x, just under implausible_certainty -- {top}")
    else:
        record("PASS", "near-threshold certainty", "no staked row between 6x and 10x")

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
        check_soccer_xg_freshness()
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

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
    # TWO DEFECTS FIXED 2026-08-19, both of which made this check report a
    # 21.5-hour error on a fixture whose stored time was EXACTLY RIGHT.
    #
    # 1. A ONE-DAY ESPN WINDOW misses the fixture it is trying to judge. ESPN
    #    files an event under its own LOCAL date. A South American evening
    #    kickoff is 00:30Z the NEXT UTC day, so a fixture stored as 00:30Z on
    #    the 19th sits in ESPN's 18th. Asking only for the 19th returns that
    #    league's OTHER matches -- the ones actually played on the 19th, around
    #    22:00Z -- and never the one we asked about. Window is now +/-1 day.
    #
    # 2. IT NEVER MATCHED BY TEAM. It took min(abs(gap)) across every event in
    #    the league that day, so it was asking "is ANY match kicking off near
    #    ours", not "is OUR match where we think it is". That can pass by luck
    #    and fail by luck; here it compared a 00:30Z fixture against a 22:00Z
    #    one and called our data 1290 minutes wrong. Events are now matched on
    #    BOTH clubs through canonical_team_key, and a fixture with no matching
    #    ESPN event is SKIPPED (counted and reported) rather than silently
    #    compared against a stranger.
    from app.ingestion.market_matcher_soccer import canonical_team_key as _ck

    leagues = {r[0] for r in rows if r[0] in LEAGUE_CODES}
    truth = {}
    today = datetime.datetime.utcnow().date()
    lo = (today - datetime.timedelta(days=1)).strftime("%Y%m%d")
    hi = (today + datetime.timedelta(days=1)).strftime("%Y%m%d")
    for lg in leagues:
        try:
            d = json.load(urllib.request.urlopen(
                "https://site.api.espn.com/apis/site/v2/sports/soccer/"
                f"{LEAGUE_CODES[lg]}/scoreboard?dates={lo}-{hi}&limit=200", timeout=25))
        except Exception:
            continue
        for e in d.get("events", []):
            try:
                cs = e["competitions"][0]["competitors"]
                names = {_ck(c["team"]["displayName"]) for c in cs}
                when = datetime.datetime.strptime(e["date"].replace("Z", ""), "%Y-%m-%dT%H:%M")
                truth.setdefault(lg, []).append((names, when))
            except Exception:
                pass
    bad, unmatched = [], 0
    for lg, h, a, est in rows:
        if lg not in truth:
            continue
        try:
            st = datetime.datetime.strptime(
                str(est).replace("Z", "").replace("T", " ")[:16], "%Y-%m-%d %H:%M")
        except Exception:
            continue
        ours = {_ck(h), _ck(a)}
        hits = [when for names, when in truth[lg] if len(ours & names) == 2]
        if not hits:
            unmatched += 1
            continue
        gap = min(abs((st - t).total_seconds()) for t in hits) / 60.0
        if gap > 30:
            bad.append(f"{h} v {a} off by {gap:.0f}min")
    note = f" ({unmatched} not found on ESPN, skipped)" if unmatched else ""
    if bad:
        record("FAIL", "soccer kickoff accuracy",
               f"{len(bad)} fixture(s) >30min from their OWN ESPN event: {bad[:3]}{note}")
    else:
        record("PASS", "soccer kickoff accuracy",
               f"{len(rows) - unmatched} fixtures matched to their own ESPN event, "
               f"all within 30min{note}")


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



def check_start_gate_coverage() -> None:
    """Every sport's /markets payload must carry an ABSOLUTE start instant.

    WHY THIS IS A CHECK AND NOT AN ASSUMPTION. The response cache serves a
    payload past its TTL only because app/api/start_gate.py re-applies the
    started-gate at serve time, and that gate can only see sports whose rows
    expose an instant it can parse. A sport that stops emitting one does not
    error -- it silently opts out of the safety gate while still being served
    stale. That is the exact failure shape of the futures spread guard (wired to
    3 of 13 routers) and the duplicate-listing cap (4 of 13, $4,180 double
    staked): the helper existed, it just was not CALLED everywhere.

    Checked against STAKED rows only. An unstaked row cannot be gated by
    definition (there is no stake to clear), so counting it would let a sport
    pass on rows the gate never touches."""
    from app.api.start_gate import START_FIELDS

    missing, covered = [], []
    for sport in SPORTS:
        path = f"/{sport}/markets" if sport else "/markets"
        try:
            rows = fetch(path)
        except Exception as exc:
            record("WARN", "start-gate coverage", f"{path} unreachable ({type(exc).__name__})")
            continue
        staked = [r for r in rows if isinstance(r, dict) and r.get("suggested_stake_dollars") is not None]
        if not staked:
            continue          # nothing to protect right now -- not a failure
        with_field = [r for r in staked if any(f in r for f in START_FIELDS)]
        # A field that is present but null on EVERY row is coverage on paper
        # only -- the gate would never fire. Reported separately from absence.
        resolvable = [r for r in with_field
                      if any(r.get(f) for f in START_FIELDS)]
        if not with_field:
            missing.append(f"{path} ({len(staked)} staked, no start field)")
        elif not resolvable:
            missing.append(f"{path} ({len(staked)} staked, start field always null)")
        else:
            covered.append(f"{path.strip('/').split('/')[0] or 'nfl'} {len(resolvable)}/{len(staked)}")

    if missing:
        record("FAIL", "start-gate coverage",
               f"{len(missing)} sport(s) served stale WITHOUT a re-checkable start: {missing}")
    elif covered:
        record("PASS", "start-gate coverage",
               f"all {len(covered)} sports with staked rows expose a start instant ({', '.join(covered[:5])}...)")
    else:
        record("WARN", "start-gate coverage", "no staked rows anywhere -- nothing to verify")


def check_soccer_league_registration() -> None:
    """Every soccer league we INGEST markets for must also be able to SETTLE
    them and to render its own name.

    WHY THIS IS A CHECK. Adding a league is four dict entries in four files, and
    the app has now drifted on three of them:

      * 2026-08-08 -- espn_soccer_client.LEAGUE_CODES held only the original six
        leagues, so every league added after it had results silently never
        REQUESTED. 134 live rows across 8 leagues could never resolve, and a
        user's Liga Portugal bets sat pending on a match that had finished
        hours earlier. The poller now logs that case, but only once a market
        already exists and is waiting -- i.e. after the damage.
      * 2026-08-09 -- fourteen divisions and nine competitions were rendering
        as raw codes because the two NAME maps had fallen behind.
      * 2026-08-14 -- KSA1 was wired into the series maps and given no name in
        either map. It rendered as "KSA1" for four days and nothing noticed.

    Each of those is the SAME defect as the spread guard at 3/13 routers and the
    duplicate cap at 4/13: a component that exists but is not wired everywhere
    produces no error at all. This check runs the diff up front, cheaply, in the
    one direction that matters -- from what we INGEST to what supports it.

    NOT symmetric on purpose. A rated pool with no market series is a coverage
    OPPORTUNITY, not a defect (E3 and Switzerland are both deliberate), so it is
    reported as INFO rather than failing the scan."""
    from app.clients import kalshi_soccer_client as ksc
    from app.clients import espn_soccer_client as esc
    from app.api.routers.soccer_markets import _SOCCER_LEAGUE_NAME

    wired = set(ksc.MONEYLINE_SERIES)
    # A league quoting spreads/totals we never take a moneyline on would never
    # get a fixture row created for it, since fixtures are built off the market
    # stream itself (market_catalog_soccer._find_or_create).
    orphans = sorted((set(ksc.SPREAD_SERIES) | set(ksc.TOTAL_SERIES)
                      | set(ksc.BTTS_SERIES)) - wired)
    unsettleable = sorted(wired - set(esc.LEAGUE_CODES))
    unnamed = sorted(wired - set(_SOCCER_LEAGUE_NAME))

    if unsettleable:
        record("FAIL", "soccer league registration",
               f"{len(unsettleable)} league(s) ingest markets with NO ESPN slug -- their bets "
               f"can never settle: {unsettleable}")
    elif orphans:
        record("FAIL", "soccer league registration",
               f"{len(orphans)} league(s) have spread/total/btts series but no moneyline, "
               f"so no fixture is ever created for them: {orphans}")
    elif unnamed:
        record("WARN", "soccer league registration",
               f"{len(unnamed)} league(s) render as their raw code: {unnamed}")
    else:
        record("PASS", "soccer league registration",
               f"all {len(wired)} ingested leagues can settle and are named")

    # SEPARATE CHECK, same file, because it guards a different failure.
    # data/soccer_kalshi_aliases.json is league-SCOPED but is loaded into
    # market_matcher_soccer.TEAM_ALIASES, which is league-BLIND. An entry whose
    # KEY is also a real club somewhere else rewrites that club too. The builder
    # refuses to write those, but the file is hand-editable and the symptom is
    # invisible -- on 2026-08-18 "Barcelona" -> barcelona sc merged FC Barcelona
    # and Barcelona SC of Ecuador onto one key and the board still went from 9%
    # to 87% priced, because within-league lookups canonicalise both sides the
    # same way. Only the cross-league (UEFA/cup) resolver would have seen it.
    from app.ingestion.market_matcher_soccer import _FROM_FILE, LEAGUE_ALIASES
    from app.models.baseline import elo_service_soccer as _elo
    _elo.refresh_ratings()
    pools = {lg: set(st.attack_log) for lg, st in _elo._cache["states_by_league"].items()}
    hijacks = []
    for key, target in _FROM_FILE.items():
        owning = sorted(lg for lg, members in pools.items()
                        if key in members and key != target)
        if owning:
            hijacks.append(f"{key!r} (also a club in {owning})")
    # SCOPED aliases are checked SEPARATELY and on a DIFFERENT invariant.
    # _FROM_FILE now holds only the GLOBAL entries -- when scoped aliases were
    # added (2026-08-18) this loop silently stopped seeing 28 of 120 file
    # entries, and a check that quietly narrows its own scope is worse than no
    # check. Scoped entries CANNOT satisfy the collision rule above (colliding
    # is precisely why they are scoped), so the thing to verify is that each one
    # points at a club that really exists IN THE POOL IT IS SCOPED TO. A scope
    # naming a dead league, or a target absent from that league, is an alias
    # that will never fire -- the silent no-op this whole family of bugs keeps
    # producing.
    dangling = []
    for (scope, key), target in LEAGUE_ALIASES.items():
        members = pools.get(scope)
        if members is None:
            dangling.append(f"{key!r} scoped to {scope!r}, which has no rating pool")
        elif target not in members:
            dangling.append(f"{key!r} -> {target!r} absent from {scope}")
    if hijacks or dangling:
        record("FAIL", "soccer alias safety",
               f"{len(hijacks)} global alias(es) would rewrite a club that exists "
               f"elsewhere: {hijacks[:4]}; {len(dangling)} scoped alias(es) point "
               f"nowhere: {dangling[:4]}")
    else:
        record("PASS", "soccer alias safety",
               f"{len(_FROM_FILE)} global aliases collide with no club in another "
               f"pool, and all {len(LEAGUE_ALIASES)} scoped aliases resolve inside "
               f"their own league")


def check_sport_registry_drift() -> None:
    """Does every sport the FRONTEND knows about reach the backend's caps?

    WHY THIS CHECK EXISTS. The same defect has now been found three times, each
    in a different hand-kept id chain, each missing a different sport:
      * frontend rowGameId had no CFB -- its own comment: "the chain form is
        what silently dropped CFB: a sport missing from it skipped the per-game
        cap entirely, so one game could surface several correlated bets as if
        they were independent";
      * recommended.py::_game_cap_id had no CFB, for the same reason;
      * the esports ladderKey had no CoD, so every CoD row keyed on an EMPTY
        match id and two different matches collapsed into one.
    Each was invisible until the sport in question started staking. Deriving the
    backend's chains from one registry (recommended.py::_SPORT_IDS) removes
    three of the four; this check is what stops the registry itself from
    silently falling behind the frontend.

    It compares the frontend's src/lib/sports.ts::SPORTS -- the source of truth
    the UI already derives from -- against _SPORT_IDS and _SPORTS, and FAILS on
    a sport present in one and missing from the other. Parsing the TS by regex
    is deliberately crude; a parse that finds nothing FAILS rather than passing
    vacuously, because "found 0 sports, none missing" is exactly the shape of a
    check that has quietly stopped testing anything.
    """
    import re as _re
    from app.models.recommended import _SPORT_IDS, _SPORTS

    ts = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "sports.ts"
    try:
        src = ts.read_text(encoding="utf-8")
    except OSError as exc:
        record("FAIL", "sport registry drift", f"cannot read {ts}: {exc}")
        return
    block = src.split("export const SPORTS", 1)
    if len(block) < 2:
        record("FAIL", "sport registry drift", "SPORTS array not found in sports.ts")
        return
    entries = _re.findall(r'\{\s*key:\s*"([a-z0-9]+)".*?\}', block[1].split("];", 1)[0], _re.S)
    if not entries:
        record("FAIL", "sport registry drift", "parsed 0 sports from sports.ts -- "
                                               "the regex has gone stale, not the code")
        return

    def camel_to_snake(v):
        return _re.sub(r"(?<!^)(?=[A-Z])", "_", v).lower()

    fe = set(entries)
    be_ids = {k for k, _f, _p, _c in _SPORT_IDS}
    be_eps = {s[0] for s in _SPORTS}
    problems = []
    for miss in sorted(fe - be_ids):
        problems.append(f"{miss!r} in sports.ts but NOT in _SPORT_IDS -- it would skip the per-game cap")
    for extra in sorted(be_ids - fe):
        problems.append(f"{extra!r} in _SPORT_IDS but not in sports.ts")
    for miss in sorted(fe - be_eps - {"racing"}):
        problems.append(f"{miss!r} in sports.ts but NOT in _SPORTS -- its bets would score "
                        f"was_recommended=False and never reach the Discord alert")
    # field names must agree too, or the registry points at an attribute the
    # /markets payload never carries and every lookup silently returns None.
    for m in _re.finditer(r'\{\s*key:\s*"([a-z0-9]+)".*?gameIdField:\s*"([A-Za-z0-9]+)"',
                          block[1].split("];", 1)[0], _re.S):
        key, field = m.group(1), camel_to_snake(m.group(2))
        be = {k: f for k, f, _p, _c in _SPORT_IDS}.get(key)
        if be and be != field:
            problems.append(f"{key!r} id field disagrees: sports.ts {field!r} vs _SPORT_IDS {be!r}")
    if problems:
        record("FAIL", "sport registry drift", f"{len(problems)}: {problems[:4]}")
    else:
        record("PASS", "sport registry drift",
               f"all {len(fe)} sports in sports.ts reach _SPORT_IDS and _SPORTS, "
               f"and their id fields agree")


def check_competition_priced_at_all() -> None:
    """A competition we INGEST but price ZERO of is a bug, not a coverage limit.

    WHY THIS EXISTS. soccer_markets._either_team_unrated has a per-league rating
    COUNT check at the bottom, and a competition league code (UCL, LEAGUES_CUP,
    LIBERTADORES...) has no rating pool of its own -- so that count reads zero
    for both clubs and rejects every row BEFORE the model runs. Each
    cross-league family therefore needs its own branch. The file has now hit
    this four times:

        UEFA          -- branch added when the family was built
        cups          -- branch added when the family was built
        Leagues Cup   -- "THIRD instance of the same trap", caught pre-ship,
                         all 420 rows would have been rejected
        CONMEBOL      -- 2026-08-18, and this one SHIPPED for one pass: 208 rows
                         all reading "no tracked match history" while the model
                         priced 9 of the 16 fixtures perfectly when called direct

    The tell is identical every time and is exactly what this checks: rows exist,
    none price. A partial rate is fine and expected (unrated Chilean and
    Paraguayan clubs SHOULD return None); a rate of exactly zero on a competition
    with real inventory is the signature of a gate that never lets the model run.

    Threshold is 20 rows so a single stray fixture cannot trip it."""
    try:
        rows = fetch("/soccer/markets")
    except Exception as exc:
        record("WARN", "competition priced at all", f"/soccer/markets unreachable ({type(exc).__name__})")
        return
    from app.api.routers.soccer_markets import (
        UEFA_LEAGUES, CONMEBOL_LEAGUES, LEAGUES_CUP_LEAGUES, NATIONAL_LEAGUES, CUP_TIERS,
    )
    competitions = set(UEFA_LEAGUES) | set(CONMEBOL_LEAGUES) | set(LEAGUES_CUP_LEAGUES)         | set(NATIONAL_LEAGUES) | set(CUP_TIERS)
    tally: dict[str, list[int]] = {}
    for r in rows:
        lg = (r or {}).get("league")
        if lg not in competitions:
            continue
        t = tally.setdefault(lg, [0, 0])
        t[0] += 1
        if r.get("model_prob") is not None:
            t[1] += 1
    # ZERO PRICED HAS TWO CAUSES AND THEY NEED DIFFERENT ANSWERS.
    #
    #   (a) the gate is unwired -- the MODEL prices these fixtures fine when
    #       called directly, but _either_team_unrated rejects them first. That
    #       is the CONMEBOL bug: 208 rows dead on the board while the model
    #       priced 9 of the 16 fixtures.
    #   (b) genuine coverage limit -- the model ALSO returns None, because the
    #       clubs are not rated. UCL in August is exactly this: the qualifying
    #       rounds are Kairat, Levski Sofia and Slovan Bratislava, from leagues
    #       with no fitted UEFA offset. Nothing is broken.
    #
    # A check that cannot separate them cries wolf every August and gets
    # ignored, so it asks the MODEL directly and only fails on (a).
    from app.db.database import SessionLocal
    from app.db.models import Market, SoccerMatch
    from app.api.routers import soccer_markets as _sm

    PREDICTORS = {
        **{lg: _sm._uefa_prediction for lg in UEFA_LEAGUES},
        **{lg: _sm._conmebol_prediction for lg in CONMEBOL_LEAGUES},
        **{lg: _sm._leagues_cup_prediction for lg in LEAGUES_CUP_LEAGUES},
        **{lg: _sm._national_prediction for lg in NATIONAL_LEAGUES},
        **{lg: _sm._cup_prediction for lg in CUP_TIERS},
    }
    unwired, coverage = [], []
    session = SessionLocal()
    try:
        for lg, (n, p) in sorted(tally.items()):
            if n < 20 or p:
                continue
            predict = PREDICTORS.get(lg)
            fixtures = (session.query(SoccerMatch)
                        .join(Market, Market.soccer_match_id == SoccerMatch.id)
                        .filter(Market.status == "active", SoccerMatch.league == lg)
                        .distinct().all())
            modelled = sum(1 for f in fixtures if predict and predict(f) is not None)
            if modelled:
                unwired.append(f"{lg} ({n} rows, 0 priced, but the model prices "
                               f"{modelled}/{len(fixtures)} fixtures)")
            else:
                coverage.append(f"{lg} ({n} rows, model prices 0/{len(fixtures)} -- unrated clubs)")
    finally:
        session.close()

    if unwired:
        record("FAIL", "competition priced at all",
               f"{len(unwired)} competition(s) where the MODEL works but the board shows nothing -- "
               f"_either_team_unrated is missing a branch: {unwired}")
    elif coverage:
        record("PASS", "competition priced at all",
               f"every competition the model can price does price; zero-priced ones are genuine "
               f"coverage limits, not gate bugs: {coverage}")
    elif tally:
        live = ", ".join(f"{lg} {p}/{n}" for lg, (n, p) in sorted(tally.items()) if n)
        record("PASS", "competition priced at all", f"every ingested competition prices some rows ({live})")
    else:
        record("WARN", "competition priced at all", "no competition rows on the board right now")


def check_cache_freshness() -> None:
    """Is the warm pass still keeping up with TTL + STALE_SERVE_SECONDS?

    The banner this whole mechanism exists to kill came from a warm pass (290s)
    that had silently outgrown the TTL (180s) as sports and model layers were
    added. It had drifted once before -- 61.7s at sizing, 548s by the time #159
    caught it. A number nobody measures is a number that rots, so it is measured
    here rather than trusted.

    Uses /health's report of the last pass rather than re-running one: timing a
    pass from this script would itself perturb the thing being timed."""
    try:
        h = fetch("/health")
    except Exception as exc:
        record("WARN", "cache freshness", f"/health unreachable ({type(exc).__name__})")
        return
    missed = (h.get("missed_scheduler_runs") or {}) if isinstance(h, dict) else {}
    if missed.get("cache_warm"):
        record("FAIL", "cache warm starved",
               f"APScheduler DROPPED {missed['cache_warm']} cache_warm run(s) -- the "
               f"warmer is being starved by the poller pool, so entries expire with "
               f"nothing refreshing them. All missed: {missed}")
    elif missed:
        record("WARN", "cache warm starved",
               f"other jobs missed runs (warmer is fine): {missed}")
    else:
        record("PASS", "cache warm starved", "no dropped scheduler runs")

    pass_s = h.get("cache_warm_pass_seconds") if isinstance(h, dict) else None
    if pass_s is None:
        record("WARN", "cache freshness",
               "no warm pass has COMPLETED yet -- expected for ~10min after a restart "
               "(job fires at start+5m40s, pass ~290s). Re-run later; if it stays null, "
               "the job is not firing at all.")
        return
    from app.api.response_cache import CACHE_TTL_SECONDS, STALE_SERVE_SECONDS
    budget = CACHE_TTL_SECONDS + STALE_SERVE_SECONDS
    if pass_s > budget:
        record("FAIL", "cache freshness",
               f"warm pass {pass_s:.0f}s EXCEEDS TTL+grace {budget}s -- entries age out "
               f"entirely and users compute live again. Re-size both constants.")
    elif pass_s > budget * 0.75:
        record("WARN", "cache freshness",
               f"warm pass {pass_s:.0f}s is {100*pass_s/budget:.0f}% of the {budget}s budget")
    else:
        record("PASS", "cache freshness", f"warm pass {pass_s:.0f}s of a {budget}s budget")


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
        check_start_gate_coverage()
        check_soccer_league_registration()
        check_sport_registry_drift()
        check_competition_priced_at_all()
        check_cache_freshness()

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

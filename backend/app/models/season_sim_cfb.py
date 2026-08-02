"""College-football season Monte Carlo -- the win distribution behind Kalshi's
KXNCAAFWINS ladders ("Will <team> win at least N games this season?", 583 open
markets across 69 teams as of 2026-08-02).

Simulates every REMAINING game on each team's schedule from the Elo win
probability, counts wins per simulated season, and returns a per-team histogram.
Games already played contribute their real result, so the answer sharpens as the
season progresses rather than being a preseason guess forever.

Two CFB-specific points:

* The schedule this needs is the FULL season (late August to early December),
  which is wider than the poller's rolling 90-day window. It therefore fetches
  its own season-wide schedule rather than reading CfbGame, and caches the
  result -- a season schedule changes rarely and the sim is thousands of seasons.

* Only games against RATED opponents are simulated. A team's schedule includes
  FCS opponents ESPN's FBS filter lets through, and elo_cfb would price those as
  exactly league-average (BASE_RATING). Those games are instead counted as a
  near-certain win at FCS_WIN_PROB, which is what they overwhelmingly are, rather
  than as a coin flip that would understate every FBS team's win total.
"""
import datetime
import logging
import threading
import time

import numpy as np

from app.clients import espn_cfb_client
from app.models.baseline import elo_cfb, elo_service_cfb

log = logging.getLogger("season_sim_cfb")

# FBS teams beat FCS opponents about 95% of the time. Used only for games whose
# opponent has no rating -- see module docstring.
FCS_WIN_PROB = 0.95

# A CFB season runs late August to early December (conference title games), plus
# bowls/playoff in Dec-Jan which do NOT count toward these regular-season win
# ladders.
_SEASON_START = (8, 15)
_SEASON_END = (12, 15)

_TTL = 3600
_lock = threading.Lock()
_cache: dict = {}


def _season_bounds(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    year = today.year if today.month >= 7 else today.year - 1
    return datetime.date(year, *_SEASON_START), datetime.date(year, *_SEASON_END)


def _fetch_season_games() -> list[dict]:
    start, end = _season_bounds(datetime.date.today())
    events = espn_cfb_client.fetch_scoreboard_events(start, end)
    return [g for g in (espn_cfb_client.parse_event(e) for e in events) if g]


_CONF_FILE = None


def load_conferences() -> dict[str, str]:
    """{team abbr: conference name}, from data/cfb_conferences.json (fetched from
    ESPN's own FBS group tree -- 138 teams across 11 conferences). Empty dict if
    missing, which leaves conference markets unpriced rather than guessing."""
    import json
    from pathlib import Path
    global _CONF_FILE
    if _CONF_FILE is None:
        _CONF_FILE = Path(__file__).resolve().parents[3] / "data" / "cfb_conferences.json"
    try:
        raw = json.loads(_CONF_FILE.read_text(encoding="utf-8"))
    except Exception:
        # SELF-HEALING: data/ is gitignored (it holds caches and the DB), so a
        # fresh clone has no conference file -- and without it 256 conference
        # markets would silently go unpriced with only a log line to show for it.
        # Rebuild it from ESPN once, then carry on.
        raw = _fetch_conferences_from_espn()
        if raw:
            try:
                _CONF_FILE.parent.mkdir(parents=True, exist_ok=True)
                _CONF_FILE.write_text(json.dumps(raw, indent=1), encoding="utf-8")
            except Exception:
                log.warning("cfb conferences: could not cache to %s", _CONF_FILE)
        else:
            log.warning("cfb conferences unavailable -- conference markets stay unpriced")
            return {}
    return {abbr: conf for conf, abbrs in raw.items() for abbr in abbrs if abbr}


def _fetch_conferences_from_espn() -> dict[str, list[str]]:
    """{conference name: [team abbreviations]} from ESPN's FBS group tree
    (group 80 -> its child conference groups -> each group's teams)."""
    import httpx
    base = ("https://sports.core.api.espn.com/v2/sports/football/leagues/"
            f"college-football/seasons/{datetime.date.today().year}/types/2/groups/80")
    out: dict[str, list[str]] = {}
    try:
        with httpx.Client(timeout=40.0, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True) as c:
            children = ((c.get(base).json().get("children") or {}).get("$ref"))
            if not children:
                return {}
            for k in c.get(children, params={"limit": 100}).json().get("items", []):
                kg = c.get(k["$ref"]).json()
                tref = (kg.get("teams") or {}).get("$ref")
                if not tref:
                    continue
                abbrs = []
                for t in c.get(tref, params={"limit": 100}).json().get("items", []):
                    try:
                        a = c.get(t["$ref"]).json().get("abbreviation")
                        if a:
                            abbrs.append(a)
                    except Exception:
                        continue
                if kg.get("name") and abbrs:
                    out[kg["name"]] = abbrs
    except Exception:
        log.exception("cfb conference fetch from ESPN failed")
        return {}
    return out


def simulate_conferences(trials: int = 4000, games: list[dict] | None = None) -> dict:
    """Per-conference finishing distributions, from the SAME game simulation the
    win totals use.

    Returns {"champion": {team: p}, "top2": {team: p}, "top4": {team: p}} where
    ranking inside a conference is by CONFERENCE-ONLY wins (games between two
    members), which is how conference standings actually work -- ranking by
    overall record would let a soft non-conference schedule buy a title.

    APPROXIMATION, stated plainly: "champion" here means FINISHING FIRST IN THE
    REGULAR-SEASON CONFERENCE STANDINGS. It does NOT simulate the conference
    title game, so it will overstate the regular-season leader and understate the
    #2 seed that would meet them in that game. Real conferences also use
    elaborate multi-team tiebreakers
    (head-to-head, division records, common opponents); this breaks ties by Elo,
    which is a reasonable proxy but not the actual rule. Independents (Notre
    Dame, UConn) have no conference and are excluded."""
    games = _fetch_season_games() if games is None else games
    conf_of = load_conferences()
    if not games or not conf_of:
        return {"champion": {}, "top2": {}, "top4": {}}

    teams = sorted({t for g in games for t in (g["home_team"], g["away_team"])} & set(conf_of))
    if not teams:
        return {"champion": {}, "top2": {}, "top4": {}}
    idx = {t: i for i, t in enumerate(teams)}

    banked = np.zeros(len(teams), dtype=np.int32)
    probs, pair = [], []
    for g in games:
        h, a = g["home_team"], g["away_team"]
        # Conference standings only count games between two members of the SAME
        # conference -- cross-conference results are irrelevant to the title.
        if h not in conf_of or a not in conf_of or conf_of[h] != conf_of[a]:
            continue
        if g.get("home_score") is not None and g.get("away_score") is not None:
            banked[idx[h if g["home_score"] > g["away_score"] else a]] += 1
            continue
        if not (elo_service_cfb.is_rated(h) and elo_service_cfb.is_rated(a)):
            continue
        p = elo_service_cfb.get_home_win_prob(h, a, bool(g.get("neutral")))
        if p is None:
            continue
        probs.append(float(p)); pair.append((idx[h], idx[a]))

    wins = np.tile(banked, (trials, 1)).astype(np.int32)
    if probs:
        p_arr = np.array(probs)
        hi = np.array([x[0] for x in pair]); ai = np.array([x[1] for x in pair])
        rng = np.random.default_rng()
        done = 0
        while done < trials:
            n = min(500, trials - done)
            hw = rng.random((n, len(p_arr))) < p_arr
            np.add.at(wins[done:done + n], (slice(None), hi), hw)
            np.add.at(wins[done:done + n], (slice(None), ai), ~hw)
            done += n

    # Elo is the tiebreak, added as a tiny fractional term so it never outweighs
    # a real win but always resolves an exact tie deterministically.
    strength = np.array([(elo_service_cfb.rating(t) or elo_cfb.BASE_RATING) for t in teams])
    score = wins + (strength - strength.min()) / (float(np.ptp(strength)) + 1.0) * 0.5

    champ = {t: 0 for t in teams}
    # Generic top-N: Kalshi's regular-season markets ask for "top 3"/"top 5"/etc
    # per conference (the depth is in the event ticker, e.g. ...-27T5-WAKE), so
    # every depth a conference can support is counted rather than a fixed few.
    topn: dict[int, dict[str, int]] = {n: {t: 0 for t in teams} for n in range(1, 11)}
    by_conf: dict[str, list[int]] = {}
    for t in teams:
        by_conf.setdefault(conf_of[t], []).append(idx[t])

    rng2 = np.random.default_rng()
    for members in by_conf.values():
        cols = np.array(members)
        sub = score[:, cols]
        order = np.argsort(-sub, axis=1)
        for n in topn:
            if sub.shape[1] < n:
                continue
            topd = order[:, :n]
            for i, m in enumerate(members):
                topn[n][teams[m]] += int((topd == i).any(axis=1).sum())
        # CONFERENCE TITLE GAME. The champion markets ask who WINS the title
        # game, not who finishes first in the standings, so the top two seeds
        # play a neutral-site game decided by Elo. Skipping this would overstate
        # the regular-season leader and understate the #2 seed -- for a dominant
        # #1 that is several percentage points.
        if sub.shape[1] < 2:
            for i, m in enumerate(members):
                champ[teams[m]] += int((order[:, 0] == i).sum())
            continue
        seed1 = order[:, 0]
        seed2 = order[:, 1]
        r1 = strength[cols][seed1]
        r2 = strength[cols][seed2]
        p1 = 1.0 / (1.0 + 10 ** (-(r1 - r2) / 400.0))   # neutral site: no home edge
        one_wins = rng2.random(trials) < p1
        winner_local = np.where(one_wins, seed1, seed2)
        for i, m in enumerate(members):
            champ[teams[m]] += int((winner_local == i).sum())

    return {
        "champion": {t: champ[t] / trials for t in teams},
        "top_n": {n: {t: v[t] / trials for t in teams} for n, v in topn.items()},
        "conference_of": conf_of,
    }


def simulate(trials: int = 4000, games: list[dict] | None = None) -> dict[str, dict[int, int]]:
    """{team: {win count: how many simulated seasons ended there}}.

    Vectorised over trials: each remaining game is one Bernoulli draw per
    simulated season, so the whole season is a (trials x games) boolean matrix
    rather than a Python loop per season."""
    games = _fetch_season_games() if games is None else games
    if not games:
        return {}

    teams = sorted({g["home_team"] for g in games} | {g["away_team"] for g in games})
    idx = {t: i for i, t in enumerate(teams)}
    banked = np.zeros(len(teams), dtype=np.int32)   # wins already achieved
    probs: list[float] = []                          # P(home wins) per remaining game
    pair: list[tuple[int, int]] = []                 # (home idx, away idx)

    for g in games:
        h, a = g["home_team"], g["away_team"]
        if g.get("home_score") is not None and g.get("away_score") is not None:
            winner = h if g["home_score"] > g["away_score"] else a
            banked[idx[winner]] += 1
            continue
        h_rated = elo_service_cfb.is_rated(h)
        a_rated = elo_service_cfb.is_rated(a)
        if h_rated and a_rated:
            p = elo_service_cfb.get_home_win_prob(h, a, bool(g.get("neutral")))
            if p is None:
                continue
        elif h_rated and not a_rated:
            p = FCS_WIN_PROB          # rated home team vs unrated (FCS) visitor
        elif a_rated and not h_rated:
            p = 1.0 - FCS_WIN_PROB
        else:
            continue                   # neither side rated -- nothing to say
        probs.append(float(p))
        pair.append((idx[h], idx[a]))

    counts: dict[str, dict[int, int]] = {t: {} for t in teams}
    if not probs:
        for t in teams:
            counts[t][int(banked[idx[t]])] = trials
        return counts

    p_arr = np.array(probs)
    home_idx = np.array([x[0] for x in pair])
    away_idx = np.array([x[1] for x in pair])
    rng = np.random.default_rng()

    wins = np.tile(banked, (trials, 1)).astype(np.int32)
    chunk = 500
    done = 0
    while done < trials:
        n = min(chunk, trials - done)
        home_wins = rng.random((n, len(p_arr))) < p_arr        # (n, games)
        # np.add.at handles repeated indices correctly (a team plays many games).
        np.add.at(wins[done:done + n], (slice(None), home_idx), home_wins)
        np.add.at(wins[done:done + n], (slice(None), away_idx), ~home_wins)
        done += n

    for t in teams:
        col = wins[:, idx[t]]
        vals, cnt = np.unique(col, return_counts=True)
        counts[t] = {int(v): int(c) for v, c in zip(vals, cnt)}
    return counts


def prob_wins_at_least(dist: dict[int, int] | None, threshold: float, trials: int) -> float | None:
    """P(team finishes with at least `threshold` wins). Kalshi states these as a
    floor_strike of N for an "N+ wins" market (note: an INTEGER floor here, not
    the N-0.5 the soccer points ladders use), so the comparison is >= ceil()."""
    if not dist or not trials:
        return None
    import math
    cutoff = math.ceil(threshold)
    return sum(c for w, c in dist.items() if w >= cutoff) / trials


def warm(trials: int = 4000) -> None:
    """Recompute + cache. Called off the request path by the poller: the season
    fetch is ~100 ESPN calls, far too slow to run inside a request."""
    now = time.time()
    with _lock:
        hit = _cache.get("dist")
        if hit and now - hit[0] < _TTL:
            return
    try:
        dist = simulate(trials=trials)
    except Exception:
        log.exception("cfb season sim failed")
        dist = {}
    with _lock:
        _cache["dist"] = (now, dist, trials)
    log.info("cfb season sim: %d teams simulated over %d trials", len(dist), trials)


def get() -> tuple[dict[str, dict[int, int]], int]:
    """(distribution, trials). Empty until warmed -- callers leave markets
    unpriced rather than pricing off a cold cache."""
    with _lock:
        hit = _cache.get("dist")
    return (hit[1], hit[2]) if hit else ({}, 0)

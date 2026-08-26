"""Settle SEASON-LONG futures, which the ordinary settlement path cannot express.

THE GAP THIS FILLS. Every grader in bet_settlement takes `(bet, game)` and
`_get_game` returns ONE event row. A season future has no single event, so
`_get_game` returns None and observation_logger.settle() skips it -- permanently,
not "until the season ends". Confirmed by grep 2026-08-25: there was no grader
for league_winner, relegation, win_total, conference_champion or wins_any in ANY
sport. That is the mechanical reason most of the 16,113 ungradeable forward-log
rows are ungradeable, and why CFB staking was once lifted "pending validation
data that could never arrive" -- nothing graded the market types.

SCOPE: soccer position futures first, because they are the only futures family
with decades of ground truth to validate against. league_winner is 579 live
markets, relegation 96, top_half/top4/top2 20 each.

OBSERVATIONS ONLY -- see grade()'s own note. This must not be wired into
PlacedBet settlement.

THE UNDERLYING COMPUTATION IS VALIDATED, against a fact outside itself. Checking
a final table against the same matches that produced it is circular, so it was
checked against NEXT SEASON'S PARTICIPANT LIST:

    champion present the following season      140 / 141
    relegated absent the following season      134 / 141   (95%)
    misses since 2005                          2 (I1 2005 Messina, SP1 2014
                                               Granada -- both league
                                               restructurings; 4 of the 5 older
                                               misses are 1990s La Liga, which
                                               used relegation playoffs)

So bottom-N is ~98% reliable in the modern era. Good enough to measure a model
against; NOT good enough to settle money, which is the other reason this is
observations-only.
"""
from __future__ import annotations

import collections
import datetime
import logging
import re

from app.clients.kalshi_soccer_client import (
    LEAGUE_WINNER_SERIES,
    RELEGATION_SERIES,
    TOP_N_SERIES,
)
from app.ingestion.market_matcher_soccer import canonical_team_key
from app.models.season_sim_soccer import CALENDAR_YEAR_LEAGUES, RELEGATION_ZONE_SIZE

log = logging.getLogger("season_futures")

# Market types this module can settle. Anything outside it is left to the
# ordinary event-based path, so adding a futures type here is an explicit act.
SEASON_FUTURES_MARKET_TYPES = frozenset(
    {"league_winner", "relegation", "top_half", "top4", "top2",   # soccer
     "win_total"}                                                 # cfb
)

# series ticker -> division, built from the SAME dicts the ingestion uses, so a
# newly-wired league cannot be settleable-but-unmapped or the reverse.
_SERIES_TO_DIVISION: dict[str, str] = {}
for _div, (_series, _label) in LEAGUE_WINNER_SERIES.items():
    _SERIES_TO_DIVISION[_series] = _div
for _div, (_series, _label) in RELEGATION_SERIES.items():
    _SERIES_TO_DIVISION[_series] = _div
for _div, _series in TOP_N_SERIES.items():
    _SERIES_TO_DIVISION[_series] = _div

_KALSHI_TICKER = re.compile(r"^([A-Z0-9]+?)-(\d{2})")
# KXNCAAFWINS-26UCF -- season then team, no separator.
_CFB_WINS_TICKER = re.compile(r"^KXNCAAFWINS-(\d{2})([A-Z0-9]+)$")
# Polymarket puts a creation timestamp in the slug instead of a season:
# ncaa-football-team-win-totals-20260731193847013
_POLY_TIMESTAMP = re.compile(r"-(\d{4})(\d{2})(\d{2})\d*$")
_POLY_SLUG_YEAR = re.compile(r"^(\d{4})-")


def _season_start_year(division: str, end_year: int) -> int:
    """Both platforms label a season by the year it ENDS.

    Kalshi writes KXPREMIERLEAGUE-27 for 2026-27 and KXBRASILEIRO-26 for Brazil's
    2026 calendar season; Polymarket writes 2027-soccer-eredivisie-winner and
    2026-soccer-allsvenskan-sweden-winner. So the same number means "start this
    year" for a calendar league and "start last year" for a split-year one, and
    CALENDAR_YEAR_LEAGUES is already the app's own list of which is which.
    """
    return end_year if division in CALENDAR_YEAR_LEAGUES else end_year - 1


def resolve_division_and_season(market) -> tuple[str | None, int | None]:
    """(division, season START year) for a futures market, or (None, None).

    Returns None rather than guessing: a wrong division silently grades a club
    against another league's table, which is worse than not grading at all.
    """
    if market is None:
        return None, None
    event = (getattr(market, "source_event_id", None) or "").strip()
    if not event:
        return None, None

    source = (getattr(market, "source", None) or "").lower()
    if source == "kalshi":
        m = _KALSHI_TICKER.match(event)
        if not m:
            return None, None
        series, yy = m.group(1), int(m.group(2))
        division = _SERIES_TO_DIVISION.get(series)
        if division is None:
            return None, None
        return division, _season_start_year(division, 2000 + yy)

    # Polymarket carries the league in the slug; reuse the router's own map so
    # the two cannot disagree about what a slug means.
    try:
        from app.api.routers.soccer_markets import _POLYMARKET_SLUG_TO_DIVISION
    except Exception:
        return None, None
    division = _POLYMARKET_SLUG_TO_DIVISION.get(event)
    if division is None:
        return None, None
    ym = _POLY_SLUG_YEAR.match(event)
    if not ym:
        return None, None
    return division, _season_start_year(division, int(ym.group(1)))


def _season_of(division: str, d: datetime.date) -> int:
    if division in CALENDAR_YEAR_LEAGUES:
        return d.year
    return d.year if d.month >= 7 else d.year - 1


def final_standings(division: str, season_start: int, matches=None) -> list[str] | None:
    """Ranked canonical team keys for a COMPLETED season, or None.

    None when the season is not finished. That check is the whole safety of this
    module: grading a future off a partial table settles it before it is decided,
    and a league leader in March is not a champion. Completeness is defined as
    every club having played a full double round-robin -- 2*(n-1) matches -- which
    is the same fixture structure season_sim_soccer simulates.
    """
    from app.ingestion import soccer_data
    from app.models.baseline import elo_service_soccer

    rows = matches if matches is not None else soccer_data.load_matches()
    games = []
    for m in rows:
        if m.get("league") != division or m.get("home_goals_ft") is None:
            continue
        try:
            d = datetime.date.fromisoformat(str(m.get("match_date"))[:10])
        except (ValueError, TypeError):
            continue
        if _season_of(division, d) != season_start:
            continue
        if elo_service_soccer._is_exhibition(m):
            continue
        games.append((canonical_team_key(m["home_team"]),
                      canonical_team_key(m["away_team"]),
                      int(m["home_goals_ft"]), int(m["away_goals_ft"])))
    if not games:
        return None

    agg: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0, 0])
    played: collections.Counter = collections.Counter()
    for h, a, hg, ag in games:
        played[h] += 1
        played[a] += 1
        for team, gf, ga in ((h, hg, ag), (a, ag, hg)):
            row = agg[team]
            row[1] += gf
            row[2] += ga
            if gf > ga:
                row[0] += 3
            elif gf == ga:
                row[0] += 1
    n = len(agg)
    if n < 4 or any(played[t] != 2 * (n - 1) for t in agg):
        return None      # season in progress, or a scrape gap -- say nothing
    return sorted(agg, key=lambda t: (-agg[t][0], -(agg[t][1] - agg[t][2]),
                                      -agg[t][1], t))


def _position(order: list[str], team: str | None) -> int | None:
    key = canonical_team_key((team or "").strip())
    if not key:
        return None
    try:
        return order.index(key) + 1
    except ValueError:
        return None      # club not in this table -- never assume it finished last


def cfb_season_from_market(market) -> int | None:
    """Season year for a CFB win-total market, or None.

    Kalshi states it: KXNCAAFWINS-26UCF is the 2026 season. Polymarket does not
    -- its slug carries a CREATION TIMESTAMP
    (ncaa-football-team-win-totals-20260731193847013), so the year is inferred
    from when the market was made. Weaker than a stated season, which is why the
    caller only trusts it if that season has real fixtures.
    """
    if market is None:
        return None
    event = (getattr(market, "source_event_id", None) or "").strip()
    if not event:
        return None
    m = _CFB_WINS_TICKER.match(event)
    if m:
        return 2000 + int(m.group(1))
    m = _POLY_TIMESTAMP.search(event)
    if m:
        return int(m.group(1))
    return None


def cfb_regular_season_wins(session, season: int) -> dict[str, int] | None:
    """{team abbr: REG-season wins} for a COMPLETED season, or None.

    Kalshi's own rules text, verbatim: "Only official regular season games count
    towards the team's win total. Ties do not count as wins. Bowl games,
    conference championship games, and college football playoff games do not
    count." So this filters on CfbGame.game_type == "REG" -- ESPN's own
    classification, not a date heuristic -- and counts strict wins only.

    None until EVERY regular-season game of that season has a score. A win total
    graded in October would settle a future half a season could still overturn.
    """
    from app.db.models import CfbGame

    games = (session.query(CfbGame)
             .filter(CfbGame.season == season, CfbGame.game_type == "REG").all())
    # RE-FILTER IN PYTHON. The SQL predicate above is the fast path, but relying
    # on it ALONE means the REG/POST distinction lives outside this function --
    # any caller handing over unfiltered rows would silently count bowl and
    # playoff games toward a regular-season win total. Caught by a test whose
    # session stub ignored .filter(): the bowl game counted, and a 0-win team
    # was credited with 1. The rules text is explicit that those do not count,
    # so the rule belongs here as well.
    games = [g for g in games
             if getattr(g, "game_type", None) == "REG"
             and getattr(g, "season", None) == season]
    if not games:
        return None
    if any(g.home_score is None or g.away_score is None for g in games):
        return None
    wins: dict[str, int] = collections.defaultdict(int)
    for g in games:
        wins.setdefault(g.home_team, 0)
        wins.setdefault(g.away_team, 0)
        if g.home_score > g.away_score:
            wins[g.home_team] += 1
        elif g.away_score > g.home_score:
            wins[g.away_team] += 1
        # a tie adds nothing to either -- per the rules text above
    return dict(wins)


def _grade_cfb_win_total(session, row, market, cache) -> str | None:
    season = cfb_season_from_market(market)
    if season is None or getattr(row, "line", None) is None:
        return None
    key = ("cfb_wins", season)
    if key not in cache:
        cache[key] = cfb_regular_season_wins(session, season)
    wins = cache[key]
    if not wins:
        return None
    team = (getattr(row, "team", None) or "").strip()
    if team not in wins:
        return None      # unknown abbreviation -- never assume zero wins
    # A RUNG, not an over/under: "at least N wins" resolves Yes at N or more.
    return "won" if wins[team] >= float(row.line) else "lost"


def grade(session, row, market=None, cache=None) -> str | None:
    """"won" / "lost" for a season future, or None to leave it pending.

    OBSERVATIONS ONLY. Real bets keep settling on the platform's own resolution,
    which is authoritative; this is ~98% accurate in the modern era, which is
    fine for scoring a model and not fine for paying one. Same posture as the
    esports series_handicap grader.

    Returns None -- never a guess -- when the division or season cannot be
    resolved, the season is unfinished, or the club is not in the table.
    """
    if cache is None:
        cache = {}
    try:
        market_type = getattr(row, "market_type", None)
        if market_type not in SEASON_FUTURES_MARKET_TYPES:
            return None
        if market is None:
            from app.db.models import Market
            mid = getattr(row, "market_id", None)
            market = session.get(Market, mid) if mid else None
        if (getattr(row, "sport", None) or "") == "cfb":
            if market_type != "win_total":
                return None
            return _grade_cfb_win_total(session, row, market, cache)
        division, season_start = resolve_division_and_season(market)
        if division is None or season_start is None:
            return None
        # CACHED PER SETTLE PASS. final_standings parses the 122 MB match cache,
        # so computing it per ROW would be thousands of passes over identical
        # data -- the same mistake list_soccer_futures already had to hoist out
        # of its own per-division loop.
        key = ("soccer_table", division, season_start)
        if key not in cache:
            cache[key] = final_standings(division, season_start)
        order = cache[key]
        if not order:
            return None
        pos = _position(order, getattr(row, "team", None))
        if pos is None:
            return None
        n = len(order)

        if market_type == "league_winner":
            return "won" if pos == 1 else "lost"
        if market_type == "relegation":
            zone = RELEGATION_ZONE_SIZE.get(division)
            if not zone:
                return None     # no defined drop zone -> not our call to invent
            return "won" if pos > n - zone else "lost"
        if market_type == "top2":
            return "won" if pos <= 2 else "lost"
        if market_type == "top4":
            return "won" if pos <= 4 else "lost"
        if market_type == "top_half":
            return "won" if pos <= n // 2 else "lost"
        return None
    except Exception:
        log.exception("season futures grade failed for market %s",
                      getattr(row, "market_id", None))
        return None

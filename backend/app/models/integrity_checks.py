"""CORRECTNESS invariants over the stored data -- the complement to
health.py's LIVENESS checks.

WHY THIS EXISTS. health.py already answers "is the plumbing running" (stale
poller, unlinked markets, missing platform, no price, race dates). It answers
nothing about whether what we stored is TRUE. On 2026-08-06 a single session
found nine distinct data-integrity defects, and not one would have tripped any
existing check:

  * a market priced at exactly 0.500 with no book (6,077 of them)
  * a totals ladder quoting every rung the same (120 bets graded off it)
  * a tennis match with a winner and an impossible score, graded anyway (52)
  * markets frozen at "active" that the exchange had already finalized (2,172)
  * finished esports matches with no winner ever written (432)
  * a rated team read as an unrated 1500 because its history sits under
    another spelling

Every one surfaced because a human noticed something odd -- a 100% market still
showing live, a bet not grading, an Elo of exactly 1500 -- and it was chased by
hand. Each is also expressible as a cheap invariant, which is what this module
does. The point is to make the NEXT instance of these classes announce itself.

DESIGN. Every check is a pure DB read, no network and no per-sport scraper, so
this stays cheap enough to run beside the existing health check. Each returns
plain dicts; health.py decides severity and presentation.

These REPORT ONLY. Nothing here mutates a bet or a market -- a false positive
should cost a line in a report, never a wrongly-settled bet.
"""
import datetime
import logging
from collections import defaultdict

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import Market, MarketSnapshot, PlacedBet, TennisMatch

log = logging.getLogger("integrity_checks")

# A ladder quoting every rung within this band is not a tight market, it is a
# market with no opinion -- see the flat-ladder cleanup for the measured case.
_FLAT_LADDER_SPAN = 0.10
_MIN_LADDER_RUNGS = 3
# Prices this close to 0 or 1 on a market we still call active are the shape a
# RESOLVED market leaves behind.
_RESOLVED_EDGE = 0.02


def _latest_snapshots(session: Session, market_ids: list[int]) -> dict:
    """Latest snapshot per market. Imported lazily from the markets router so
    there is exactly one implementation of this query in the app."""
    from app.api.routers.markets import _batch_latest_snapshots

    return _batch_latest_snapshots(session, market_ids)


def _active_with_snapshots(session: Session, cache: dict | None = None):
    """(active markets, {market_id: latest snapshot}) -- fetched ONCE per run.

    Three of the checks below need exactly this, and the first version had each
    of them query it independently: three full passes over ~24k markets and
    their snapshots, which took the /health-check endpoint to 55s. `cache` is
    threaded through run_all so a single run pays for it once.
    """
    if cache is not None and "markets" in cache:
        return cache["markets"], cache["snaps"]
    markets = session.query(Market).filter(Market.status == "active").all()
    snaps = _latest_snapshots(session, [m.id for m in markets])
    if cache is not None:
        cache["markets"], cache["snaps"] = markets, snaps
    return markets, snaps


def phantom_priced_markets(session: Session, cache: dict | None = None) -> list[dict]:
    """Active markets whose only 'price' is a seeded 0.500 with no book.

    _implied_prob now returns None for these, so they cannot be bet -- this
    check exists to notice if that ever regresses, or if a NEW seeded value
    starts appearing (the guard is deliberately narrow: exactly 0.500, no bid,
    no ask, no volume).
    """
    markets, snaps = _active_with_snapshots(session, cache)
    by_sport: dict = defaultdict(int)
    for m in markets:
        sn = snaps.get(m.id)
        if sn is None or sn.yes_bid is not None or sn.yes_ask is not None:
            continue
        if sn.last_price is not None and abs(sn.last_price - 0.5) < 1e-9 and not sn.volume:
            by_sport[m.sport] += 1
    return [{"sport": s, "count": n} for s, n in sorted(by_sport.items(), key=lambda x: -x[1])]


def flat_ladders(session: Session, cache: dict | None = None) -> list[dict]:
    """Totals ladders where every rung carries the same price.

    A totals ladder is monotonic by construction -- P(over 0.5) >= P(over 1.5)
    >= P(over 2.5). Flat means the quotes are placeholders, and anything graded
    or priced off them is fiction.
    """
    all_markets, snaps = _active_with_snapshots(session, cache)
    markets = [
        m for m in all_markets
        if m.line is not None
        and m.market_type in ("game_total", "total", "team_total", "series_total")
    ]
    ladders: dict = defaultdict(dict)
    for m in markets:
        sn = snaps.get(m.id)
        if sn is None or sn.last_price is None:
            continue
        key = (m.sport, m.market_type, m.source,
               m.soccer_match_id or m.mlb_game_id or m.tennis_match_id
               or m.nfl_game_id or m.cs2_match_id or m.lol_match_id, m.team)
        ladders[key][m.line] = sn.last_price
    out: dict = defaultdict(int)
    for (sport, *_rest), rungs in ladders.items():
        if len(rungs) < _MIN_LADDER_RUNGS:
            continue
        if max(rungs.values()) - min(rungs.values()) < _FLAT_LADDER_SPAN:
            out[sport] += 1
    return [{"sport": s, "count": n} for s, n in sorted(out.items(), key=lambda x: -x[1])]


def resolved_looking_active_markets(session: Session, hours: int = 6, cache: dict | None = None) -> list[dict]:
    """Markets we still call active, priced at an extreme, on an event that
    already STARTED over `hours` ago.

    A local proxy for "the exchange resolved this and we never noticed" -- no
    network call, so it can run every cycle. The authoritative fixes are
    reconcile_kalshi_market_status and reconcile_polymarket_market_status (both
    sources are covered now); this is the alarm that says one has not run.

    BOTH CONDITIONS ARE REQUIRED, and the first draft of this check got that
    wrong: price alone flagged 11,901 tennis rows, because a pre-game heavy
    favourite legitimately trades at 0.98. An extreme price is only suspicious
    once the event is well underway, so the start-time gate is what turns this
    from noise into a signal. The band is also tightened to 1c.
    """
    from app.db.models import Cs2Match, LolMatch, SoccerMatch, ValorantMatch

    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=hours)).date().isoformat()
    started: set = set()
    for model, fk in ((Cs2Match, "cs2_match_id"), (ValorantMatch, "valorant_match_id"),
                      (LolMatch, "lol_match_id"), (SoccerMatch, "soccer_match_id"),
                      (TennisMatch, "tennis_match_id")):
        for r in session.query(model).all():
            # PREFER estimated_start_time over match_date. match_date is the less
            # reliable of the two: 75 of 99 soccer fixtures disagree with their
            # own estimated_start_time, and where a Kalshi ticker pins the real
            # date it is estimated_start_time that matches it exactly (Espanyol v
            # Levante: match_date 2026-08-03, est 2026-08-16, ticker 26AUG16).
            #
            # Reading match_date made this check report 86 soccer markets as
            # "status never reconciled" for events that had NOT started -- Kalshi
            # itself still listed all 100 sampled as active. The markets were
            # fine; the date was stale.
            when = getattr(r, "estimated_start_time", None) or getattr(r, "match_date", None)
            if when and str(when)[:10] < cutoff:
                started.add((fk, r.id))

    markets, snaps = _active_with_snapshots(session, cache)
    by: dict = defaultdict(int)
    for m in markets:
        sn = snaps.get(m.id)
        if sn is None or sn.last_price is None:
            continue
        if not (sn.last_price <= 0.01 or sn.last_price >= 0.99):
            continue
        if not any((fk, getattr(m, fk, None)) in started
                   for fk in ("cs2_match_id", "valorant_match_id", "lol_match_id",
                              "soccer_match_id", "tennis_match_id")):
            continue
        by[(m.sport, m.source)] += 1
    return [{"sport": s, "source": src, "count": n}
            for (s, src), n in sorted(by.items(), key=lambda x: -x[1])]


def impossible_tennis_scores(session: Session) -> list[dict]:
    """Tennis matches carrying a winner AND a score that cannot be a finished
    match -- i.e. a retirement the is_retirement flag failed to record.

    That flag is measured to never fire (0 of 95 real cases), so the score is
    the only honest signal. Derivative graders already refuse these; this counts
    them so a change in the rate is visible.
    """
    from app.models.bet_settlement import _tennis_match_incomplete

    rows = session.query(TennisMatch).filter(TennisMatch.winner_key.isnot(None)).all()
    bad = [m for m in rows if _tennis_match_incomplete(m)]
    flagged = sum(1 for m in bad if m.is_retirement)
    return [{
        "resolved": len(rows),
        "incomplete_score": len(bad),
        "flagged_as_retirement": flagged,
        "flag_missed": len(bad) - flagged,
    }]


def finished_without_result(session: Session, hours: int = 12) -> list[dict]:
    """Events that started well over `hours` ago and still have no result.

    Sport-agnostic on purpose: it is the same symptom whatever the cause
    (blocked scraper, name-join failure, a source that never covered the tier),
    which is what makes it keep working when a NEW cause appears.
    """
    from app.db.models import Cs2Match, LolMatch, SoccerMatch, ValorantMatch

    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
    cutoff_date = cutoff.date().isoformat()
    out = []
    for label, model, date_attr, result_attr in (
        ("cs2", Cs2Match, "match_date", "winner"),
        ("valorant", ValorantMatch, "match_date", "winner"),
        ("lol", LolMatch, "match_date", "winner"),
        ("soccer", SoccerMatch, "match_date", "result_ft"),
        ("tennis", TennisMatch, "match_date", "winner_key"),
    ):
        try:
            rows = session.query(model).filter(getattr(model, result_attr).is_(None)).all()
        except Exception:
            log.exception("finished_without_result failed for %s", label)
            continue
        n = sum(
            1 for r in rows
            if (getattr(r, date_attr) or "") and str(getattr(r, date_attr))[:10] < cutoff_date
        )
        if n:
            out.append({"sport": label, "count": n})
    return sorted(out, key=lambda x: -x["count"])


def resolver_dependent_teams(session: Session) -> list[dict]:
    """Esports market names that are unrated as spelled but rated once resolved.

    NOT a defect list -- these are cases where name resolution is doing its job
    ("G2" -> "G2 Esports", "Kiwoom DRX" -> its real history). The first draft of
    this check called them errors, which was backwards.

    It is reported because the count is a HEALTH SIGNAL for the resolver: this
    is the exact machinery whose absence once displayed a rated team as an
    unrated 1500 and mis-seeded futures. A sudden jump means new spellings are
    arriving; a drop to zero when markets exist means the resolver stopped
    working. Both are worth seeing.
    """
    out = []
    for sport, service, fk in (
        ("cs2", "elo_service_cs2", "cs2_match_id"),
        ("valorant", "elo_service_valorant", "valorant_match_id"),
        ("lol", "elo_service_lol", "lol_match_id"),
    ):
        try:
            mod = __import__(f"app.models.baseline.{service}", fromlist=["x"])
            state = mod._cache.get("state")
            if state is None:
                continue
            names = {
                m.team for m in session.query(Market)
                .filter(Market.sport == sport, Market.status == "active", Market.team.isnot(None))
                .all() if m.team
            }
            bad = [
                n for n in names
                if state.games_played(n) == 0 and state.games_played(mod.resolve_team_name(n)) > 0
            ]
            if bad:
                out.append({"sport": sport, "count": len(bad), "examples": sorted(bad)[:5]})
        except Exception:
            log.exception("unrated_but_known_teams failed for %s", sport)
    return out


def stale_bet_market_types(session: Session) -> list[dict]:
    """Bets whose stored market_type disagrees with their market's CURRENT one.

    PlacedBet.market_type is a snapshot taken at placement, kept deliberately as
    a record of what was bet. Settlement no longer DISPATCHES on it (see
    bet_settlement.effective_market_type), so a disagreement is no longer a
    grading bug -- but it is still the fingerprint of a market being re-typed
    under existing bets, and it is worth seeing when that happens.

    Measured 2026-08-06: 499 rows, every one tennis game_spread -> set_spread.
    Before the dispatch fix those 499 routed to a Kalshi-only grader that flipped
    21.7% of them, including 3 real-money bets. A jump here means a re-typing
    just happened and its cohort deserves the same check.
    """
    rows = (
        session.query(Market.sport, PlacedBet.market_type, Market.market_type,
                      func.count(PlacedBet.id))
        .join(Market, PlacedBet.market_id == Market.id)
        .filter(PlacedBet.market_type != Market.market_type)
        .group_by(Market.sport, PlacedBet.market_type, Market.market_type)
        .all()
    )
    return [{"sport": s, "bet_says": bt, "market_says": mt, "count": n}
            for s, bt, mt, n in sorted(rows, key=lambda r: -r[3])]


def foreign_league_seasons(session: Session) -> list[dict]:
    """Soccer league-seasons whose clubs look like ANOTHER league's clubs.

    Guards a silent data-poisoning bug found 2026-08-07. football-data.co.uk does
    not 404 a season/division it never published -- it REDIRECTS to a different
    division and returns 200 with a valid CSV. Requesting 9394/P1.csv (Liga
    Portugal starts 94/95) yields 9394/SP1.csv, a full Spanish La Liga season.
    The client stamped the division it ASKED for onto whatever came back, so
    1,602 Spanish matches trained into the Portuguese and Spanish-second-tier
    rating pools: Barcelona, Ath Madrid, Ath Bilbao, Celta and La Coruna were all
    rated Liga Portugal clubs. Soccer Elo is per-league, so nothing diluted them
    and nothing complained -- the pools just quietly had the wrong teams in them.

    fetch_season_csv now keeps only rows whose own `Div` column matches, which
    fixes the known cause. This checks the SYMPTOM instead, so a new variant --
    a different redirect, a re-labelled file, a bad merge -- still surfaces.

    Method: for each (league, season), compare its club set against each league's
    own recent-5-season club set. A season that matches some OTHER league better
    than its own, by at least half its clubs, is reported. Verified to find
    exactly the four real blocks (P1 93-94, SP2 93-94/94-95/95-96) before the fix
    and zero after it.

    Reads the football-data cache, not the DB; `session` is unused and kept only
    to match the run_all() calling convention.
    """
    from collections import defaultdict

    from app.ingestion import soccer_data

    teams: dict[tuple, set] = defaultdict(set)
    counts: dict[tuple, int] = defaultdict(int)
    for m in soccer_data.load_matches():
        key = (m["league"], m.get("season"))
        teams[key].add(m["home_team"])
        teams[key].add(m["away_team"])
        counts[key] += 1

    by_league: dict[str, list] = defaultdict(list)
    for league, season in teams:
        if season:
            by_league[league].append(season)
    recent = {
        league: set().union(*[teams[(league, s)] for s in sorted(seasons)[-5:]])
        for league, seasons in by_league.items()
    }

    out = []
    for (league, season), clubs in teams.items():
        if not season or not clubs:
            continue
        own = len(clubs & recent.get(league, set())) / len(clubs)
        rival = max(
            ((other, len(clubs & pool) / len(clubs)) for other, pool in recent.items() if other != league),
            key=lambda x: x[1], default=None,
        )
        if rival and rival[1] > own and rival[1] >= 0.5:
            out.append({"league": league, "season": season, "matches": counts[(league, season)],
                        "own_league_overlap": round(own, 3),
                        "looks_like": rival[0], "overlap": round(rival[1], 3)})
    return sorted(out, key=lambda r: -r["matches"])


_CACHE_AWARE = {"phantom_priced_markets", "flat_ladders", "resolved_looking_active_markets"}


def duplicated_paper_bets(session: Session, days: int = 3) -> list[dict]:
    """Markets carrying MORE THAN ONE staked paper bet in the recent window.

    The paper record is this app's only validation harness, so a market logged
    twice is not a cosmetic duplicate -- it silently doubles that pick's weight
    in every forward-CLV and hit-rate number computed from it.

    THIS EXACT BUG HAPPENED. Until 2026-08-07 the logger gated on the PENDING
    paper set alone, so a paper bet's market became loggable again the moment it
    settled: the next poll logged a fresh bet, the settler graded it seconds
    later off the already-known result, and the loop repeated every cycle. Six
    markets reached 119 staked paper bets each -- 118 of them logged on a single
    day -- and F1 as a whole hit a 79x duplication factor.

    The fix (one paper bet per market, ever) holds: 464 staked paper bets since,
    max 1 per market, zero duplicates. Nothing was checking that it KEEPS
    holding, which is what this is for. Bounded to a recent window on purpose --
    the pre-fix rows are still in the table and would otherwise make this fire
    forever about history nobody can change.
    """
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    rows = (
        session.query(PlacedBet.market_id, func.count(PlacedBet.id))
        .filter(PlacedBet.paper == True,  # noqa: E712
                PlacedBet.stake_dollars > 0,
                PlacedBet.placed_at >= cutoff)
        .group_by(PlacedBet.market_id)
        .having(func.count(PlacedBet.id) > 1)
        .all()
    )
    return [
        {"market_id": mid, "staked_paper_bets": n,
         "detail": f"market {mid} has {n} staked paper bets in the last {days}d "
                   f"-- the one-per-market rule has regressed, and every paper "
                   f"metric now double-counts this pick"}
        for mid, n in rows
    ]


# (module, result key, what the key must sum to across all teams, label).
#
# Every entry is a MUTUALLY EXCLUSIVE outcome, so the league-wide sum is fixed by
# arithmetic, not by the model: exactly one team finishes best, one finishes
# worst, one wins the title, and exactly two win a pennant/conference. Division
# legs are deliberately absent -- their total depends on how many divisions a
# league has, and encoding that here would be a rulebook fact this file has no
# business holding.
_SIM_LEG_INVARIANTS = [
    ("app.models.season_sim_service", "best_record_pct", 1.0, "nfl"),
    ("app.models.season_sim_service", "worst_record_pct", 1.0, "nfl"),
    ("app.models.season_sim_service_nba", "best_record_pct", 1.0, "nba"),
    ("app.models.season_sim_service_nba", "worst_record_pct", 1.0, "nba"),
    ("app.models.season_sim_service_nba", "championship_pct", 1.0, "nba"),
    ("app.models.season_sim_service_nba", "conf_champ_pct", 2.0, "nba"),
    ("app.models.season_sim_service_mlb", "best_record_pct", 1.0, "mlb"),
    ("app.models.season_sim_service_mlb", "worst_record_pct", 1.0, "mlb"),
    ("app.models.season_sim_service_mlb", "championship_pct", 1.0, "mlb"),
    ("app.models.season_sim_service_mlb", "pennant_pct", 2.0, "mlb"),
    # WNBA has no conferences -- the top 8 records league-wide make the playoffs
    # and exactly one team is the 1-seed, so this one IS a flat league-wide sum.
    ("app.models.season_sim_service_wnba", "one_seed", 1.0, "wnba"),
]

# CFB AND SOCCER ARE DELIBERATELY ABSENT, and it is a structural limit rather
# than an oversight. Their one-winner legs are PER GROUP: CFB's conference
# champion sums to the number of conferences (11), and soccer's league winner
# and relegation sum per league, once for each. A flat league-wide sum would
# compare them against the wrong target and fire constantly.
#
# Covering them needs a second invariant shape -- group the rows by conference /
# league first, then assert each GROUP sums to 1 (or to that league's relegation
# count). Worth doing: CFB is the largest futures book at 1,380 rows and 46
# staked, and it is exactly the shape of book the NBA bug appeared in.
_SIM_LEG_TOLERANCE = 0.05


def incoherent_sim_legs(session: Session) -> list[dict]:
    """Season-sim legs whose league-wide probabilities do not add up.

    FOUND A LIVE MONEY BUG THE FIRST TIME IT WAS RUN BY HAND (2026-08-11). NBA
    worst_record summed to 20.68 across 30 teams, with five teams simultaneously
    at 1.0000 -- and four of them were staked $2.50 each against a market pricing
    them 0.11-0.225, the largest apparent edges in the whole futures book at +78
    to +90pp. Two independent causes, both invisible to every other check:
    a schedule only 13% published (so the sim ranked teams by how many games
    ESPN had listed for them) and ties awarding a full count to each tied team.
    NFL best_record was separately at 1.49 on a COMPLETE schedule, purely from
    the tie arithmetic.

    Nothing re-ran that sum afterwards, which is what this is for.

    MEASURED ON THE SIM, NOT ON THE RENDERED ROWS, and that distinction matters:
    the futures endpoints list the same outcome once per platform, so summing
    what the page shows double-counts every Kalshi/Polymarket pair. Doing it that
    way is what made the racing championships look broken at exactly 2.00 when
    the model was in fact coherent (1.000 over distinct drivers). The sim output
    has one entry per team and no such ambiguity.

    ONLY AN EXCESS IS REPORTED. A sum ABOVE the target is always a defect --
    probability invented from nowhere. A sum BELOW it usually just means entrants
    the model cannot rate yet, which is normal and is why tennis' 0.71 is fine.

    Reads the in-process sim caches, so it is free; an empty cache (the sim has
    not run, or refused an incomplete schedule) reports nothing rather than
    erroring, since that is a different condition with its own logging.
    """
    import importlib

    out: list[dict] = []
    for mod_name, key, target, sport in _SIM_LEG_INVARIANTS:
        try:
            mod = importlib.import_module(mod_name)
            results = mod.get_results() or {}
        except Exception:
            continue
        vals = []
        for team, row in results.items():
            if team.startswith("_") or not isinstance(row, dict):
                continue
            v = row.get(key)
            if v is None:
                continue
            v = float(v)
            vals.append(v / 100.0 if v > 1.0 else v)
        if not vals:
            continue
        total = sum(vals)
        if total <= target + _SIM_LEG_TOLERANCE:
            continue
        worst = max(vals)
        out.append({
            "sport": sport, "leg": key, "sum": round(total, 4), "expected": target,
            "teams": len(vals), "max_single_team": round(worst, 4),
            "detail": f"{sport} {key} sums to {total:.3f} across {len(vals)} teams "
                      f"but exactly {target:g} can happen -- the excess is invented "
                      f"probability, and any edge computed from it is fictional "
                      f"(largest single value {worst:.3f})",
        })
    return out


def run_all(session: Session, cache: dict | None = None) -> dict:
    """Every invariant, as {check_name: rows}. Never raises -- a check that
    fails is reported as an error string rather than taking the whole report
    down with it, since this runs beside a user-facing health endpoint.

    `cache` lets a caller that has ALREADY fetched the active-markets-and-latest-
    snapshots pair hand it over instead of paying for it twice -- the health
    endpoint's stale-poller check needs exactly the same data.
    """
    checks = {
        "phantom_priced_markets": phantom_priced_markets,
        "flat_ladders": flat_ladders,
        "resolved_looking_active_markets": resolved_looking_active_markets,
        "impossible_tennis_scores": impossible_tennis_scores,
        "finished_without_result": finished_without_result,
        "resolver_dependent_teams": resolver_dependent_teams,
        "stale_bet_market_types": stale_bet_market_types,
        "foreign_league_seasons": foreign_league_seasons,
        "duplicated_paper_bets": duplicated_paper_bets,
        "incoherent_sim_legs": incoherent_sim_legs,
    }
    cache = {} if cache is None else cache
    out: dict = {}
    for name, fn in checks.items():
        try:
            out[name] = fn(session, cache=cache) if name in _CACHE_AWARE else fn(session)
        except Exception as exc:
            log.exception("integrity check %s failed", name)
            out[name] = [{"error": str(exc)}]
    return out

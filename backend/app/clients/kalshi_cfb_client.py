"""Kalshi college-football markets client.

Only KXNCAAFGAME (per-game moneyline) is fetched. KXNCAAFSPREAD and
KXNCAAFTOTAL exist as series but had ZERO open markets when checked
2026-08-02 -- they list closer to kickoff. They're declared below so that
adding them later is a one-line change, and market_matcher_cfb's event-ticker
regex is already permissive enough to parse them.

Uses the bulk /markets?series_ticker=... endpoint rather than
get_open_events + get_markets_for_event per event. That N+1 pattern is what
caused the Kalshi 429s that stalled CS2 for 189 hours; one call returns every
open market in the series.
"""
import logging
import re

from app.clients.base import get_json

log = logging.getLogger("kalshi_cfb_client")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

MONEYLINE_SERIES = "KXNCAAFGAME"      # per-game moneyline (30 open 2026-08-02)
WIN_TOTAL_SERIES = "KXNCAAFWINS"      # season win ladders (583 open, 69 teams)
# These two exist as series but list NOTHING yet -- they populate closer to
# kickoff. Declared so enabling them is a one-line change.
# Conference futures. Champion = wins the conference TITLE GAME; qualifier =
# finishes top-2 (i.e. reaches that game); regtop = finishes top-N in the
# regular-season standings, with N encoded in the event ticker (…-27T5-WAKE).
# KXNCAAFSEC added 2026-08-03: it had no open markets when this list was first
# built and now lists 16 (ticker KXNCAAFSEC-26-VAN, the same "-<season>-<team>"
# shape the other four use, so the existing parser handles it unchanged).
#
# BIG TEN: the ticker is KXNCAAFB10, and it is live (5 open, "Will Wisconsin win
# the College Football Big Ten Championship", found 2026-08-06 via the catalog
# scanner's flagged list). The earlier note here said Big Ten was "STILL
# unlisted" after checking KXNCAAFBIGTEN, KXNCAAFB1G and KXNCAAFBIG10 -- three
# guesses, none of them the real one, and the conclusion drawn was that Kalshi
# does not list it. It does. Guessing ticker spellings proves nothing when they
# all miss; the catalog scanner enumerates what actually exists, which is what
# eventually caught this. The Big Ten is a power conference, so this was one of
# the more valuable conference markets to be missing.
CONF_CHAMPION_SERIES = ["KXNCAAFACC", "KXNCAAFB10", "KXNCAAFB12", "KXNCAAFMAC", "KXNCAAFPAC12", "KXNCAAFSEC"]
CONF_QUALIFIER_SERIES = ["KXNCAAFSECQ", "KXNCAAFB12QUAL", "KXNCAAFMACQUAL", "KXNCAAFMWCQUAL"]
CONF_REGTOP_SERIES = ["KXNCAAFACCREGTOP", "KXNCAAFSECREGTOP", "KXNCAAFB12REGTOP"]
# Playoff futures. Priced by playoff_sim_cfb, which models a COMMITTEE decision
# via a record+Elo proxy -- see that module before trusting these.
PLAYOFF_SERIES = "KXNCAAFPLAYOFF"
QUARTERFINAL_SERIES = "KXNCAAFQF"
TITLE_CONFERENCE_SERIES = "KXNCAAFCONF"

SPREAD_SERIES = "KXNCAAFSPREAD"
TOTAL_SERIES = "KXNCAAFTOTAL"


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def get_moneyline_markets() -> list[dict]:
    """One row per (game, team). `team_abbr_kalshi` is the ticker's own suffix
    and `display_name` is yes_sub_title -- market_matcher_cfb.resolve_team needs
    BOTH, since a 130-team sport can't be covered by an alias table alone and the
    display name is the fallback."""
    try:
        data = get_json(f"{KALSHI_BASE}/markets?series_ticker={MONEYLINE_SERIES}&status=open&limit=1000")
    except Exception:
        log.exception("kalshi cfb moneyline fetch failed")
        return []
    out = []
    for m in data.get("markets", []):
        ticker = m.get("ticker") or ""
        if not ticker or not m.get("event_ticker"):
            continue
        out.append({
            "ticker": ticker,
            "event_ticker": m["event_ticker"],
            "team_abbr_kalshi": ticker.rsplit("-", 1)[-1] if "-" in ticker else None,
            "display_name": m.get("yes_sub_title"),
            "close_time": m.get("close_time") or m.get("expiration_time"),
            "status": m.get("status") or "active",
            "last_price": _to_float(m.get("last_price_dollars")),
            "yes_bid": _to_float(m.get("yes_bid_dollars")),
            "yes_ask": _to_float(m.get("yes_ask_dollars")),
            "volume": _to_float(m.get("volume_fp")),
        })
    return out


def get_spread_markets() -> list[dict]:
    """One row per (game, team, line) for the KXNCAAFSPREAD ladders.

    THE LINE COMES FROM floor_strike, NOT FROM THE TICKER. NFL's equivalent
    ticker ends "-KC8" for a 7.5-point line -- the trailing digit is a rung
    INDEX, not the line -- so parsing the number out of the ticker would be off
    by one and silently mis-price every rung. floor_strike carries the real
    threshold (verified live on KXNFLSPREAD: -KC8 has floor_strike 7.5, -KC7 has
    6.5). Reading it directly also means this does not depend on the rung
    numbering scheme staying the same.

    TEAM RESOLUTION mirrors get_moneyline_markets and for the same reason: 130
    teams cannot be covered by an alias table, so market_matcher_cfb.resolve_team
    needs the ticker suffix AND a display name. The suffix here carries a
    trailing rung index ("ND3"), stripped below. The sub-title is a full
    sentence ("Notre Dame wins by over 3.5 points") rather than the bare team
    name the moneyline markets give, so the phrase is trimmed back to the team.

    STRUCTURE IS INFERRED, NOT OBSERVED -- stated plainly because it matters.
    KXNCAAFSPREAD is a real series ("College Football Spread", confirmed via
    /series) but has ZERO markets in every status as of 2026-08-06: Kalshi opens
    spreads near game week and the season starts late August. The shape above is
    taken from the two verified neighbours -- KXNFLSPREAD's live rung markets and
    KXNCAAFGAME's live event/suffix convention -- and deliberately leans on
    floor_strike so the one unobservable part (rung numbering) cannot break it.
    Re-check the first time real markets appear.
    """
    try:
        data = get_json(f"{KALSHI_BASE}/markets?series_ticker={SPREAD_SERIES}&status=open&limit=1000")
    except Exception:
        log.exception("kalshi cfb spread fetch failed")
        return []
    out = []
    for m in data.get("markets", []):
        ticker = m.get("ticker") or ""
        line = _to_float(m.get("floor_strike"))
        if not ticker or not m.get("event_ticker") or line is None:
            continue  # no threshold -> nothing to price against, don't guess one
        suffix = ticker.rsplit("-", 1)[-1] if "-" in ticker else ""
        team = suffix.rstrip("0123456789")
        if not team:
            continue
        sub = (m.get("yes_sub_title") or "").strip()
        display = sub.split(" wins by")[0].strip() or None
        out.append({
            "ticker": ticker,
            "event_ticker": m["event_ticker"],
            "team_abbr_kalshi": team,
            "display_name": display,
            "line": line,
            "close_time": m.get("close_time") or m.get("expiration_time"),
            "status": m.get("status") or "active",
            "last_price": _to_float(m.get("last_price_dollars")),
            "yes_bid": _to_float(m.get("yes_bid_dollars")),
            "yes_ask": _to_float(m.get("yes_ask_dollars")),
            "volume": _to_float(m.get("volume_fp")),
        })
    return out


def get_win_total_markets() -> list[dict]:
    """One row per (team, win threshold) for the KXNCAAFWINS ladders.

    NOTE the label shape differs from the game markets and it matters:
    yes_sub_title here is "9+ wins", NOT a team name, so the matcher's
    display-name fallback cannot resolve these. The team comes only from the
    EVENT ticker suffix ("KXNCAAFWINS-26UCF" -> "UCF", after stripping the
    two-digit season), which is why market_matcher_cfb's alias table has to be
    complete for every team listed here.

    The threshold is floor_strike, and unlike the soccer points ladders it is an
    INTEGER (9 means 9+), not N-0.5."""
    try:
        data = get_json(f"{KALSHI_BASE}/markets?series_ticker={WIN_TOTAL_SERIES}&status=open&limit=1000")
    except Exception:
        log.exception("kalshi cfb win-total fetch failed")
        return []
    out = []
    for m in data.get("markets", []):
        ev = m.get("event_ticker") or ""
        floor = m.get("floor_strike")
        if not ev or not m.get("ticker") or floor is None:
            continue
        suffix = ev.rsplit("-", 1)[-1]
        abbr = re.sub(r"^\d+", "", suffix)  # "26UCF" -> "UCF"
        if not abbr:
            continue
        out.append({
            "ticker": m["ticker"],
            "event_ticker": ev,
            "team_abbr_kalshi": abbr,
            "line": float(floor),
            "status": m.get("status") or "active",
            "last_price": _to_float(m.get("last_price_dollars")),
            "yes_bid": _to_float(m.get("yes_bid_dollars")),
            "yes_ask": _to_float(m.get("yes_ask_dollars")),
            "volume": _to_float(m.get("volume_fp")),
        })
    return out


def _conf_rows(series_list, market_type):
    """Shared shape for the conference futures: one row per (team, series). Team
    comes from the market ticker suffix; yes_sub_title is the display name and is
    kept so the matcher's name fallback can resolve teams the alias table misses
    (these series DO label by team, unlike the win ladders)."""
    out = []
    for series in series_list:
        try:
            data = get_json(f"{KALSHI_BASE}/markets?series_ticker={series}&status=open&limit=1000")
        except Exception:
            log.exception("kalshi cfb %s fetch failed", series)
            continue
        for m in data.get("markets", []):
            ticker = m.get("ticker") or ""
            ev = m.get("event_ticker") or ""
            if not ticker or not ev:
                continue
            depth = None
            if market_type == "conference_regtop":
                # "KXNCAAFACCREGTOP-27T5-WAKE" -> the middle segment holds T<N>.
                mid = ev.rsplit("-", 1)[-1]
                hit = re.search(r"T(\d+)", mid)
                if not hit:
                    continue          # unparseable depth -> skip, never guess
                depth = float(hit.group(1))
            out.append({
                "ticker": ticker,
                "event_ticker": ev,
                "series": series,
                "team_abbr_kalshi": ticker.rsplit("-", 1)[-1],
                "display_name": m.get("yes_sub_title"),
                "line": depth,
                "status": m.get("status") or "active",
                "last_price": _to_float(m.get("last_price_dollars")),
                "yes_bid": _to_float(m.get("yes_bid_dollars")),
                "yes_ask": _to_float(m.get("yes_ask_dollars")),
                "volume": _to_float(m.get("volume_fp")),
            })
    return out


def get_conference_champion_markets():
    return _conf_rows(CONF_CHAMPION_SERIES, "conference_champion")


def get_conference_qualifier_markets():
    return _conf_rows(CONF_QUALIFIER_SERIES, "conference_qualifier")


def get_conference_regtop_markets():
    return _conf_rows(CONF_REGTOP_SERIES, "conference_regtop")


def get_playoff_markets():
    return _conf_rows([PLAYOFF_SERIES], "cfb_playoff")


def get_quarterfinal_markets():
    return _conf_rows([QUARTERFINAL_SERIES], "cfb_quarterfinal")


def get_title_conference_markets():
    """Which CONFERENCE wins the national title. Labelled by conference name
    ("SEC", "Big Ten"), not by team, so the team-name matcher does not apply --
    the router maps these via playoff_sim_cfb's conference keys."""
    return _conf_rows([TITLE_CONFERENCE_SERIES], "cfb_title_conference")

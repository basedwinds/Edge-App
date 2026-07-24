"""Live-polling Polymarket client for MLB markets. Parallel to
polymarket_nba_client.py, same architecture-decision reasoning as
market_matcher_mlb.py.

Confirmed live 2026-07-17 via tag_slug=mlb (186 open events across 2 pages):
real per-game events are bundled (moneyline + NRFI + run-line spread + total
+ 1st-5-innings spread/total + extra-innings, all as separate markets in ONE
event, e.g. "mlb-lad-nyy-2026-07-17") -- identified by slug shape
(mlb-{away}-{home}-{yyyy}-{mm}-{dd}, same regex market_matcher_mlb.py already
uses to PARSE these slugs, reused here to FILTER the tag listing down to
real per-game events instead of futures/leaderboard/prop events). The base
moneyline market in each bundle has groupItemTitle=None; spread/total
sub-markets are identified by their own groupItemTitle text ("Spread -1.5",
"O/U 9.5") -- confirmed live, not assumed.

Real quirk confirmed live: the "NRFI" groupItemTitle's actual question text
asks "will a run score in the 1st inning" -- the label and the literal
question read as opposite framings. Resolved in get_rfi_markets() below
(2026-07-17): sides are returned matching the QUESTION's real polarity
(yes=RFI), not the label.

Futures slugs (World Series/AL/NL/division/win-totals) hardcoded, confirmed
live against the real tag_slug=mlb listing -- same "known stable slugs, not
title-pattern-matched" choice as polymarket_client.py's NFL futures.
"""
import re

from app.clients.base import get_json, paginate
from app.clients.polymarket_client import extract_market_prices
from app.ingestion.market_matcher_mlb import resolve_polymarket_team_name, _POLYMARKET_SLUG_RE

GAMMA = "https://gamma-api.polymarket.com"

MLB_TAG_SLUG = "mlb"

# "Spread: Boston Red Sox (-1.5)" -> ("Boston Red Sox", "-1.5") -- see
# get_spread_markets()'s docstring for why parsing the NAMED team out of the
# question text (not just outcome position) matters for correctness.
_SPREAD_QUESTION_RE = re.compile(r"Spread:\s*(.+?)\s*\(([+-]?[\d.]+)\)")

FUTURES_EVENT_SLUGS = {
    "championship": ["mlb-world-series-champion-2026"],
    "conference_champion": ["mlb-2026-american-league-champion", "mlb-2026-national-league-champion"],
    "division_winner": [
        "mlb-2026-al-east-champion", "mlb-2026-al-central-champion", "mlb-2026-al-west-champion",
        "mlb-2026-nl-east-champion", "mlb-2026-nl-central-champion", "mlb-2026-nl-west-champion",
    ],
    "playoff_qualifier": ["mlb-team-to-make-postseason"],
}
WIN_TOTAL_EVENT_SLUG = "mlb-2026-regular-season-win-totals"


def get_open_events(tag_slug: str = MLB_TAG_SLUG, limit: int = 100) -> list[dict]:
    def url_builder(offset):
        return f"{GAMMA}/events?tag_slug={tag_slug}&closed=false&limit={limit}&offset={offset}"

    return paginate(url_builder, list_key=None, limit=limit, cursor_style="offset")


def get_game_events() -> list[dict]:
    """Filters the full MLB tag listing down to real per-game bundles by
    slug shape (mlb-{away}-{home}-{yyyy}-{mm}-{dd}) -- everything else
    (futures/leaderboards/props/first-5-innings-winner sibling events/
    player-props sibling events) has a different slug shape and is excluded."""
    return [e for e in get_open_events() if _POLYMARKET_SLUG_RE.match(e.get("slug", ""))]


_F5_SLUG_SUFFIX = "-first-five-winner"


def get_f5_events() -> list[dict]:
    """First-5-innings-winner sibling events (confirmed live 2026-07-17):
    slug is the same mlb-{away}-{home}-{yyyy}-{mm}-{dd} shape as a game
    bundle PLUS "-first-five-winner", a SEPARATE event from the game bundle
    (unlike NRFI/spread/total, which live as sub-markets inside it)."""
    return [e for e in get_open_events() if e.get("slug", "").endswith(_F5_SLUG_SUFFIX) and _POLYMARKET_SLUG_RE.match(e.get("slug", "")[: -len(_F5_SLUG_SUFFIX)])]


def game_slug_for_f5_event(f5_slug: str) -> str:
    return f5_slug[: -len(_F5_SLUG_SUFFIX)]


def get_moneyline_markets() -> list[dict]:
    rows = []
    for event in get_game_events():
        slug = event.get("slug", "")
        for m in event.get("markets", []):
            if m.get("groupItemTitle"):
                continue  # not the base moneyline market -- spread/total/etc sub-market
            prices = extract_market_prices(m)
            outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
            if len(outcomes) != 2 or len(outcome_prices) != 2:
                continue
            for team_name, price in zip(outcomes, outcome_prices):
                rows.append(
                    {
                        "event_slug": slug,
                        "event_title": event.get("title", ""),
                        "team_full_name": team_name,
                        "team_abbr": resolve_polymarket_team_name(team_name),
                        "last_price": price,
                        "condition_id": prices["condition_id"],
                        "volume": prices["volume"],
                    }
                )
    return rows


def get_spread_markets() -> list[dict]:
    """Confirmed live: multiple lines per game (e.g. -1.5 and -2.5), each a
    separate TWO-OUTCOME market (NOT two independent per-team lines) keyed
    by groupItemTitle "Spread {line}" -- the `question` text names exactly
    ONE team the signed line applies to (e.g. "Spread: Boston Red Sox
    (-1.5)"), and the outcomes list is [that team, the other team]. The
    OTHER team's real implied line is the NEGATION of the named team's line
    (a -1.5 favorite is definitionally a +1.5 underdog on the same market),
    NOT the same literal number.

    REAL BUG caught before shipping, not assumed correct: an earlier version
    of this function assigned the SAME literal line to both outcomes'
    rows (e.g. both TB and BOS stored as "line: -1.5"), which would have fed
    game_lines_mlb.py::prob_team_covers the wrong sign for the underdog side
    -- confirmed by inspecting a real live market ("Spread: Boston Red Sox
    (-1.5)", outcomes ["Boston Red Sox", "Tampa Bay Rays"]) where TB's real
    implied line is +1.5, not -1.5. Fixed by parsing the named team +
    signed line directly out of `question`, then negating for whichever of
    the two real teams isn't the named one.

    Real quirk confirmed by inspecting live DB rows, not a bug in this
    client: some games carry TWO markets with the exact same groupItemTitle
    but genuinely different conditionIds/prices (e.g. two separate
    "Spread -1.5" markets for the same TB@NYY game, one naming each team as
    favorite). Both are real, independently tradeable Polymarket markets --
    this function intentionally returns both rather than silently
    collapsing them, same "don't discard real data" discipline as elsewhere
    in this app. The fix above makes each one individually correct
    regardless of how many duplicates exist for a game."""
    rows = []
    for event in get_game_events():
        slug = event.get("slug", "")
        for m in event.get("markets", []):
            git = m.get("groupItemTitle") or ""
            if not git.startswith("Spread "):
                continue
            prices = extract_market_prices(m)
            outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
            if len(outcomes) != 2 or len(outcome_prices) != 2:
                continue
            match = _SPREAD_QUESTION_RE.search(m.get("question") or "")
            if not match:
                continue
            named_team, named_line = match.group(1).strip(), float(match.group(2))
            if named_team not in outcomes:
                continue
            other_idx = 1 - outcomes.index(named_team)
            other_team = outcomes[other_idx]
            # REAL BUG caught live via the Recommended Bets page (2026-07-17,
            # same session as the fix above): `named_line` is the literal
            # BOOKMAKER spread (e.g. -1.5 for the favorite), but
            # game_lines_mlb.py::prob_team_covers needs the THRESHOLD a
            # team's own margin must exceed to win ("wins by more than N"),
            # which is the NEGATION of the bookmaker spread for both sides
            # (favorite -1.5 -> must win by more than +1.5; underdog +1.5 ->
            # must not lose by 1.5, i.e. margin > -1.5). Storing named_line
            # directly (as an earlier version of this fix did) fed the model
            # backwards thresholds -- a favorite's OWN "-1.5" was read as
            # "margin > -1.5" (nearly always true), producing absurd 70-85%
            # model estimates on deep lines that surfaced as 50+pp "edges" on
            # the live Recommended Bets page, which is what caught this.
            team_lines = {named_team: -named_line, other_team: named_line}
            for team_name, price in zip(outcomes, outcome_prices):
                rows.append(
                    {
                        "event_slug": slug,
                        "team_full_name": team_name,
                        "team_abbr": resolve_polymarket_team_name(team_name),
                        "line": team_lines[team_name],
                        "last_price": price,
                        "condition_id": prices["condition_id"],
                        "volume": prices["volume"],
                    }
                )
    return rows


def get_total_markets() -> list[dict]:
    """groupItemTitle "O/U {line}", outcomes ["Over", "Under"]."""
    rows = []
    for event in get_game_events():
        slug = event.get("slug", "")
        for m in event.get("markets", []):
            git = m.get("groupItemTitle") or ""
            if not git.startswith("O/U "):
                continue
            prices = extract_market_prices(m)
            outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
            if len(outcomes) != 2 or len(outcome_prices) != 2:
                continue
            try:
                line = float(git.replace("O/U ", "").strip())
            except ValueError:
                continue
            for side, price in zip(outcomes, outcome_prices):
                rows.append(
                    {
                        "event_slug": slug,
                        "side": side.lower(),
                        "line": line,
                        "last_price": price,
                        "condition_id": prices["condition_id"],
                        "volume": prices["volume"],
                    }
                )
    return rows


def get_f5_markets() -> list[dict]:
    """First-5-innings-winner sibling events -- confirmed live: THREE separate
    binary Yes/No markets per event (one per team + "Draw"), unlike Kalshi's
    three mutually-exclusive single-ticker markets, but the same 3 real
    outcomes. groupItemTitle is the team's full name or literally "Draw"."""
    rows = []
    for event in get_f5_events():
        slug = event.get("slug", "")
        game_slug = game_slug_for_f5_event(slug)
        for m in event.get("markets", []):
            git = m.get("groupItemTitle")
            if not git:
                continue
            prices = extract_market_prices(m)
            outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
            if "Yes" not in outcomes:
                continue
            yes_idx = outcomes.index("Yes")
            yes_price = outcome_prices[yes_idx] if yes_idx < len(outcome_prices) else None
            if git == "Draw":
                outcome, team_abbr = "TIE", None
            else:
                team_abbr = resolve_polymarket_team_name(git)
                outcome = team_abbr
            rows.append(
                {
                    "event_slug": slug,
                    "game_slug": game_slug,
                    "outcome": outcome,  # "TIE" or a team abbreviation
                    "team_abbr": team_abbr,
                    "last_price": yes_price,
                    "condition_id": prices["condition_id"],
                    "volume": prices["volume"],
                }
            )
    return rows


def get_rfi_markets() -> list[dict]:
    """RFI sub-market inside each per-game bundle -- groupItemTitle says
    "NRFI" but confirmed live the actual `question` text asks "Will there be
    a run scored in the first inning?", i.e. "Yes" means a run WAS scored
    (RFI happened), the OPPOSITE of what the "NRFI" label alone would
    suggest. Returns "yes"/"no" sides with prices matching the real question
    polarity (Yes = RFI), not the label -- resolves the polarity trap
    flagged (but not resolved) when moneyline/spread/total were built."""
    rows = []
    for event in get_game_events():
        slug = event.get("slug", "")
        for m in event.get("markets", []):
            if m.get("groupItemTitle") != "NRFI":
                continue
            prices = extract_market_prices(m)
            outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
            if len(outcomes) != 2 or len(outcome_prices) != 2:
                continue
            for side, price in zip(outcomes, outcome_prices):
                rows.append(
                    {
                        "event_slug": slug,
                        "side": side.lower(),  # "yes" = a run scored in the 1st inning (RFI), "no" = NRFI
                        "last_price": price,
                        "condition_id": prices["condition_id"],
                        "volume": prices["volume"],
                    }
                )
    return rows


def get_futures_markets() -> list[dict]:
    rows = []
    for kind, slugs in FUTURES_EVENT_SLUGS.items():
        for slug in slugs:
            try:
                event = get_json(f"{GAMMA}/events/slug/{slug}")
            except Exception:
                continue
            group_label = event.get("title", "")
            for m in event.get("markets", []):
                team_name = m.get("groupItemTitle")
                if not team_name or team_name == "Other":
                    continue
                prices = extract_market_prices(m)
                outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
                if "Yes" not in outcomes or not outcome_prices:
                    continue
                yes_idx = outcomes.index("Yes")
                rows.append(
                    {
                        "market_kind": kind,
                        "slug": slug,
                        "group_label": group_label,
                        "team_full_name": team_name,
                        "team_abbr": resolve_polymarket_team_name(team_name),
                        "yes_price": outcome_prices[yes_idx] if yes_idx < len(outcome_prices) else None,
                        "condition_id": prices["condition_id"],
                        "volume": prices["volume"],
                    }
                )
    return rows


def get_win_total_markets() -> list[dict]:
    """One event, 30 team markets bundled together -- unlike Kalshi's 30
    separate per-team series. groupItemTitle is the team's full name;
    question text carries the line ("...win more than 86.5 games...")."""
    try:
        event = get_json(f"{GAMMA}/events/slug/{WIN_TOTAL_EVENT_SLUG}")
    except Exception:
        return []
    rows = []
    for m in event.get("markets", []):
        team_name = m.get("groupItemTitle")
        if not team_name:
            continue
        line = m.get("line")
        if line is None:
            # Fall back to parsing "more than 86.5 games" out of the question text
            import re

            match = re.search(r"more than ([\d.]+) games", m.get("question", "") or "")
            line = float(match.group(1)) if match else None
        prices = extract_market_prices(m)
        outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
        if "Yes" not in outcomes or line is None:
            continue
        yes_idx = outcomes.index("Yes")
        rows.append(
            {
                "team_full_name": team_name,
                "team_abbr": resolve_polymarket_team_name(team_name),
                "line": float(line),
                "yes_price": outcome_prices[yes_idx] if yes_idx < len(outcome_prices) else None,
                "condition_id": prices["condition_id"],
                "volume": prices["volume"],
            }
        )
    return rows

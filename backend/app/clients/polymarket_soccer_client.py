"""Live-polling Polymarket client for Soccer moneyline (3-way Home/Draw/Away)
markets. Parallel to polymarket_tennis_client.py, same real per-market
`closed`/`active` staleness bug already found and fixed there (see
_market_status below) -- checked proactively here rather than re-discovered,
per the approved build plan's explicit callout of this exact bug class.

Confirmed live 2026-07-19: `tag_slug=mls` lists real open per-match events
(100+, offset-paginated); `tag_slug=epl`/`la-liga`/`serie-a`/`bundesliga`/
`ligue-1` exist but return only 1-4 events each in July (the 5 European
leagues are off-season -- confirmed genuinely seasonal, not a missing tag,
since Kalshi shows the identical near-empty pattern for the same leagues
right now). Each real match event bundles exactly 3 sibling markets (one
binary Yes/No per team + one for the draw, `groupItemTitle` = team name or
"Draw (...)") -- confirmed live via a real MLS event dump, a genuinely
different shape from Tennis's 2-named-outcome moneyline market.

`gameStartTime` was observed MISMATCHED against the real match date embedded
in the market's own `question` text on a live example (question said
"...on 2026-04-12", gameStartTime said "2026-09-24") -- a real, unexplained
data quirk on Polymarket's side, not a parsing bug here. match_date is
therefore taken from the `question` text (reliable, confirmed against
several real events), NOT from gameStartTime -- gameStartTime is only used
as the estimated_start_time signal (same role as everywhere else in this
app), with the same known-unreliable caveat noted.

SPREAD and TOTAL live on a SEPARATE sibling event per match (confirmed live
2026-07-19: slug suffix "-more-markets", e.g. "mls-sea-rsl-2026-04-12-more-
markets") -- already returned by the same tag_slug query as moneyline, just
a different event object. Its own `title` keeps the "X vs. Y" shape with a
" - More Markets" suffix instead of nothing (e.g. "New England Revolution
vs. Houston Dynamo - More Markets") -- same home-first convention, stripped
the same way. Confirmed real shape:
  - SPREAD: "Spread: {Team} ({line})" per market (e.g. "Spread: Seattle
    Sounders FC (-1.5)"), TWO lines confirmed (-1.5/-2.5), one market per
    team per line (4 total) -- outcomes[0] is always the named team,
    outcome_prices[0] is that team's own cover probability.
  - TOTAL: "{home} vs. {away}: O/U {line}" (match-level only -- the same
    bundle also has 1st/2nd-half and per-team O/U variants with extra text,
    deliberately NOT matched by this exact-anchored regex, same "build the
    narrow real thing, not everything" scope as moneyline)."""
import re

from app.clients.base import paginate
from app.clients.polymarket_client import extract_market_prices

GAMMA = "https://gamma-api.polymarket.com"

TAG_SLUGS = {
    "E0": "epl",
    "SP1": "la-liga",
    "I1": "serie-a",
    "D1": "bundesliga",
    "F1": "ligue-1",
    "MLS": "mls",
    # Added 2026-08-07 alongside the Kalshi side of these two leagues. Both were
    # a real one-sided gap, not a Polymarket limitation: checked live, tag_slug
    # "primeira-liga" returns 129 open events and "efl-championship" returns 84,
    # while this app had ZERO Polymarket markets for either. Without them both
    # leagues would be Kalshi-only, so no cross-platform divergence could ever
    # be found on them.
    #
    # The slug is not guessable from the league name -- "liga-portugal",
    # "portugal", "championship" and "efl" all return 0. Check the tag before
    # concluding a league is absent from Polymarket.
    "P1": "primeira-liga",
    "E1": "efl-championship",
    # N1 (Eredivisie) is DELIBERATELY ABSENT, checked 2026-08-07 rather than
    # assumed. "eredivisie", "netherlands-eredivisie" and "dutch-eredivisie" all
    # return 0 open events, AND 0 events with closed=true -- i.e. Polymarket has
    # never listed the league at all, so this is not an off-season artifact of
    # the kind that wrongly deferred Liga Portugal in July. Control run in the
    # same breath: "primeira-liga" returned 100 open events, so the query itself
    # was working. Eredivisie is therefore Kalshi-only and no cross-platform
    # divergence exists for it. Re-check if Polymarket adds Dutch football;
    # adding a slug that resolves to nothing would just cost a fetch per cycle.
}

_QUESTION_DATE_RE = re.compile(r"on (\d{4}-\d{2}-\d{2})\?$")


def get_open_events(tag_slug: str, limit: int = 100) -> list[dict]:
    def url_builder(offset):
        return f"{GAMMA}/events?tag_slug={tag_slug}&closed=false&limit={limit}&offset={offset}"

    return paginate(url_builder, list_key=None, limit=limit, cursor_style="offset")


def _market_status(m: dict) -> str:
    """Same real bug/fix as polymarket_tennis_client.py::_market_status --
    the event-level closed=false filter doesn't guarantee every market
    INSIDE the bundle is still open. Checked proactively for Soccer from day
    one rather than waiting to rediscover it live."""
    if m.get("closed") or not m.get("active", True):
        return "closed"
    return "active"


def _normalize_start_time(raw) -> str | None:
    if not raw:
        return None
    text = str(raw).strip().replace(" ", "T", 1)
    if text.endswith("+00"):
        text = text[:-3] + "Z"
    return text


# REAL BUG this guards against (caught live 2026-07-19, same day, while
# verifying the SECOND batch's own model_prob output): every one of a real
# match's SIBLING events (-halftime-result/-second-half-result/
# -first-to-score, all added this same session) has a market whose
# groupItemTitle is the literal HOME team name, identical to the real
# moneyline event's own home-side market -- get_moneyline_markets' own
# title-parsing (`title.partition(" vs. ")`) never captured the sibling
# event's own " - Halftime Result"/etc SUFFIX on the home side (only the
# AWAY side substring carries it, since the suffix is appended after the
# away team's name), so `group_title == home_team` matched EVERY sibling
# event's own home-side market too, not just the real moneyline one --
# confirmed live: a single real team's home moneyline row was being
# silently overwritten every poll cycle by whichever of 4 real, DIFFERENT
# markets (moneyline itself, halftime result, second-half result, first-
# to-score) happened to upsert last, corrupting live moneyline_3way pricing
# for the home side of every MLS match with any of these sibling markets --
# i.e. essentially all of them. Excluding any event whose own slug carries
# one of these known sibling suffixes is what actually fixes this (the
# titles alone are genuinely ambiguous; the slug is not).
_SIBLING_EVENT_SLUG_SUFFIXES = (
    "-halftime-result", "-second-half-result", "-first-to-score",
    "-more-markets", "-exact-score",
)


def get_moneyline_markets() -> list[dict]:
    """One row per (event, outcome) -- 3 rows per real match (home/away/
    draw). home_team/away_team are parsed from the event's own `title`
    ("Seattle Sounders FC vs. Real Salt Lake", home first -- same "vs."
    convention confirmed on Kalshi's side, and Polymarket's own
    groupItemTitle values for the two team markets directly match these two
    names, so no separate home/away disambiguation heuristic is needed
    beyond string equality) -- but ONLY for the real moneyline event itself,
    see _SIBLING_EVENT_SLUG_SUFFIXES above on why sibling events must be
    excluded explicitly, not just relied on to fail the title/groupItemTitle
    match on their own."""
    rows = []
    for division, tag_slug in TAG_SLUGS.items():
        for event in get_open_events(tag_slug):
            slug = event.get("slug", "")
            if slug.endswith(_SIBLING_EVENT_SLUG_SUFFIXES):
                continue
            title = event.get("title", "")
            if " vs. " not in title:
                continue
            home_team, _, away_team = title.partition(" vs. ")
            home_team, away_team = home_team.strip(), away_team.strip()
            if not home_team or not away_team:
                continue
            for m in event.get("markets", []):
                group_title = m.get("groupItemTitle") or ""
                if group_title.startswith("Draw"):
                    side, team = "draw", None
                elif group_title == home_team:
                    side, team = "home", home_team
                elif group_title == away_team:
                    side, team = "away", away_team
                else:
                    continue
                prices = extract_market_prices(m)
                outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
                if outcomes != ["Yes", "No"] or len(outcome_prices) != 2:
                    continue
                question = m.get("question", "")
                date_match = _QUESTION_DATE_RE.search(question)
                match_date = date_match.group(1) if date_match else None
                rows.append({
                    "event_slug": event.get("slug", ""),
                    "event_title": title,
                    "division": division,
                    "home_team": home_team,
                    "away_team": away_team,
                    "side": side,
                    "team": team,
                    "match_date": match_date,
                    "last_price": outcome_prices[0],  # YES price
                    "condition_id": prices["condition_id"],
                    "volume": prices["volume"],
                        "raw_bid": prices["best_bid"],
                        "raw_ask": prices["best_ask"],
                    "status": _market_status(m),
                    "estimated_start_time": _normalize_start_time(m.get("gameStartTime")),
                })
    return rows


_MORE_MARKETS_TITLE_SUFFIX_RE = re.compile(r"\s*-\s*More Markets$")
_SPREAD_QUESTION_RE = re.compile(r"^Spread: (.+?) \(([+-][\d.]+)\)$")
_TOTAL_QUESTION_RE = re.compile(r"^(.+?) vs\. (.+?): O/U ([\d.]+)$")


def get_spread_markets() -> list[dict]:
    """One row per (event, team, line) -- lives on the same "-more-markets"
    sibling event as get_total_markets, filtered by question shape rather
    than by event slug string matching (more robust than assuming every
    "-more-markets" event has this exact market -- see module docstring)."""
    rows = []
    for division, tag_slug in TAG_SLUGS.items():
        for event in get_open_events(tag_slug):
            title = event.get("title", "")
            if " vs. " not in title:
                continue
            home_team, _, away_team = title.partition(" vs. ")
            home_team = home_team.strip()
            away_team = _MORE_MARKETS_TITLE_SUFFIX_RE.sub("", away_team).strip()
            if not home_team or not away_team:
                continue
            for m in event.get("markets", []):
                sub_match = _SPREAD_QUESTION_RE.match(m.get("question", ""))
                if not sub_match:
                    continue
                name, raw_line = sub_match.group(1), float(sub_match.group(2))
                if name == home_team:
                    team = home_team
                elif name == away_team:
                    team = away_team
                else:
                    continue
                prices = extract_market_prices(m)
                outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
                if len(outcomes) != 2 or len(outcome_prices) != 2 or outcomes[0] != name:
                    continue
                rows.append({
                    "event_slug": event.get("slug", ""),
                    "division": division,
                    "home_team": home_team,
                    "away_team": away_team,
                    "team": team,
                    # REAL BUG this fixes (caught live 2026-07-19 comparing
                    # this app's own routed model_prob output against Kalshi
                    # vs Polymarket rows for the identical real line): this
                    # question's own line is a SIGNED standard-spread value
                    # ("Real Salt Lake (-1.5)" = RSL must win by more than
                    # 1.5 -- confirmed against a real low price on a real
                    # underdog), NOT Kalshi's always-positive "wins by more
                    # than X goals" magnitude (see kalshi_soccer_client.py).
                    # abs() here normalizes to Kalshi's convention so
                    # soccer_markets.py::_game_spread_model_prob can use ONE
                    # formula regardless of which platform a row came from --
                    # without this, applying that formula directly to a
                    # negative Polymarket line silently computed the WRONG
                    # side's cover probability.
                    "line": abs(raw_line),
                    "last_price": outcome_prices[0],
                    "condition_id": prices["condition_id"],
                    "volume": prices["volume"],
                        "raw_bid": prices["best_bid"],
                        "raw_ask": prices["best_ask"],
                    "status": _market_status(m),
                })
    return rows


def get_total_markets() -> list[dict]:
    """One row per (event, line) -- match-level total, home/away parsed
    directly from the question text itself (no dependency on the event's
    own title parsing, unlike get_spread_markets -- the question already
    has everything needed)."""
    rows = []
    for division, tag_slug in TAG_SLUGS.items():
        for event in get_open_events(tag_slug):
            for m in event.get("markets", []):
                sub_match = _TOTAL_QUESTION_RE.match(m.get("question", ""))
                if not sub_match:
                    continue
                home_team, away_team, line = sub_match.group(1), sub_match.group(2), float(sub_match.group(3))
                prices = extract_market_prices(m)
                outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
                if outcomes != ["Over", "Under"] or len(outcome_prices) != 2:
                    continue
                rows.append({
                    "event_slug": event.get("slug", ""),
                    "division": division,
                    "home_team": home_team,
                    "away_team": away_team,
                    "line": line,
                    "over_price": outcome_prices[0],
                    "condition_id": prices["condition_id"],
                    "volume": prices["volume"],
                        "raw_bid": prices["best_bid"],
                        "raw_ask": prices["best_ask"],
                    "status": _market_status(m),
                })
    return rows


# ---------------------------------------------------------------------------
# Second batch (added 2026-07-19, same day, after a full catalog_scan.py
# audit surfaced real, live inventory this app hadn't covered yet). All of
# these live inside the SAME "-more-markets" sibling event get_spread_markets/
# get_total_markets already read (confirmed live via several real MLS
# events, e.g. "mls-chi-vwh-...-more-markets": BTTS, team totals, and BOTH
# 1st/2nd-half variants of everything all bundled together, not separate
# events) -- except First Half/Second Half WINNER and Correct Score, which
# each live on their own dedicated sibling event ("-halftime-result"/
# "-second-half-result"/"-exact-score", confirmed live), and First Team To
# Score ("-first-to-score", confirmed live). BTTS specifically was a real
# pre-existing gap: this client never had a get_btts_markets at all before
# now, even though Kalshi's own BTTS coverage (kalshi_soccer_client.py) has
# existed since the prior build pass -- Polymarket's own real BTTS
# inventory was simply never looked for until this audit.
# ---------------------------------------------------------------------------

_BTTS_QUESTION_RE = re.compile(r"^(.+?) vs\. (.+?): Both Teams to Score$")
_HALF1_BTTS_QUESTION_RE = re.compile(r"^(.+?) vs\. (.+?): Both Teams to Score in First Half$")
_HALF2_BTTS_QUESTION_RE = re.compile(r"^(.+?) vs\. (.+?): Both Teams to Score in Second Half$")


def _get_btts_from_more_markets(question_re: re.Pattern) -> list[dict]:
    rows = []
    for division, tag_slug in TAG_SLUGS.items():
        for event in get_open_events(tag_slug):
            for m in event.get("markets", []):
                sub_match = question_re.match(m.get("question", ""))
                if not sub_match:
                    continue
                home_team, away_team = sub_match.group(1), sub_match.group(2)
                prices = extract_market_prices(m)
                outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
                if outcomes != ["Yes", "No"] or len(outcome_prices) != 2:
                    continue
                rows.append({
                    "event_slug": event.get("slug", ""),
                    "division": division,
                    "home_team": home_team,
                    "away_team": away_team,
                    "yes_price": outcome_prices[0],
                    "condition_id": prices["condition_id"],
                    "volume": prices["volume"],
                        "raw_bid": prices["best_bid"],
                        "raw_ask": prices["best_ask"],
                    "status": _market_status(m),
                })
    return rows


def get_btts_markets() -> list[dict]:
    return _get_btts_from_more_markets(_BTTS_QUESTION_RE)


def get_first_half_btts_markets() -> list[dict]:
    return _get_btts_from_more_markets(_HALF1_BTTS_QUESTION_RE)


def get_second_half_btts_markets() -> list[dict]:
    return _get_btts_from_more_markets(_HALF2_BTTS_QUESTION_RE)


# Team-total and half-total regexes are checked in a specific ORDER at each
# call site (half-variant first, then the general one) -- both the half and
# non-half team-total shapes ("X vs. Y: {team} 1st Half O/U {line}" and
# "X vs. Y: {team} O/U {line}") are structurally overlapping enough that a
# naive single regex could misparse "1st Half"/"2nd Half" itself as a team
# name. Guarded against the same way get_spread_markets already guards
# against a garbage name: the captured "team" text is validated against the
# event's own real home_team/away_team afterward, and anything else
# (including a stray "1st Half"/"2nd Half" match) is silently skipped, not
# guessed.
_HALF1_TEAM_TOTAL_QUESTION_RE = re.compile(r"^(.+?) vs\. (.+?): (.+?) 1st Half O/U ([\d.]+)$")
_HALF2_TEAM_TOTAL_QUESTION_RE = re.compile(r"^(.+?) vs\. (.+?): (.+?) 2nd Half O/U ([\d.]+)$")
_HALF1_TOTAL_QUESTION_RE = re.compile(r"^(.+?) vs\. (.+?): 1st Half O/U ([\d.]+)$")
_HALF2_TOTAL_QUESTION_RE = re.compile(r"^(.+?) vs\. (.+?): 2nd Half O/U ([\d.]+)$")
_TEAM_TOTAL_QUESTION_RE = re.compile(r"^(.+?) vs\. (.+?): (.+?) O/U ([\d.]+)$")


def get_team_total_markets() -> list[dict]:
    """One side's OWN goal total (e.g. "Chicago Fire FC vs. Vancouver
    Whitecaps FC: Chicago Fire FC O/U 0.5"), confirmed live in the same
    "-more-markets" bundle as spread/total. Matched with _TEAM_TOTAL_
    QUESTION_RE, which would ALSO loosely match a half-total question's own
    "1st Half"/"2nd Half O/U ..." shape (see module comment above) -- the
    home/away validation below is what actually excludes those, not the
    regex alone."""
    rows = []
    for division, tag_slug in TAG_SLUGS.items():
        for event in get_open_events(tag_slug):
            for m in event.get("markets", []):
                question = m.get("question", "")
                sub_match = _TEAM_TOTAL_QUESTION_RE.match(question)
                if not sub_match:
                    continue
                home_team, away_team, name, line = sub_match.group(1), sub_match.group(2), sub_match.group(3), float(sub_match.group(4))
                if name == home_team:
                    team = home_team
                elif name == away_team:
                    team = away_team
                else:
                    continue  # e.g. a half-total question's "1st Half"/"2nd Half" text, not a real team
                prices = extract_market_prices(m)
                outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
                if outcomes != ["Over", "Under"] or len(outcome_prices) != 2:
                    continue
                rows.append({
                    "event_slug": event.get("slug", ""), "division": division,
                    "home_team": home_team, "away_team": away_team, "team": team, "line": line,
                    "over_price": outcome_prices[0], "condition_id": prices["condition_id"],
                    "volume": prices["volume"],
                        "raw_bid": prices["best_bid"],
                        "raw_ask": prices["best_ask"], "status": _market_status(m),
                })
    return rows


def _get_half_total_markets(question_re: re.Pattern) -> list[dict]:
    rows = []
    for division, tag_slug in TAG_SLUGS.items():
        for event in get_open_events(tag_slug):
            for m in event.get("markets", []):
                sub_match = question_re.match(m.get("question", ""))
                if not sub_match:
                    continue
                home_team, away_team, line = sub_match.group(1), sub_match.group(2), float(sub_match.group(3))
                prices = extract_market_prices(m)
                outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
                if outcomes != ["Over", "Under"] or len(outcome_prices) != 2:
                    continue
                rows.append({
                    "event_slug": event.get("slug", ""), "division": division,
                    "home_team": home_team, "away_team": away_team, "line": line,
                    "over_price": outcome_prices[0], "condition_id": prices["condition_id"],
                    "volume": prices["volume"],
                        "raw_bid": prices["best_bid"],
                        "raw_ask": prices["best_ask"], "status": _market_status(m),
                })
    return rows


def get_first_half_total_markets() -> list[dict]:
    return _get_half_total_markets(_HALF1_TOTAL_QUESTION_RE)


def get_second_half_total_markets() -> list[dict]:
    return _get_half_total_markets(_HALF2_TOTAL_QUESTION_RE)


def _get_half_team_total_markets(question_re: re.Pattern) -> list[dict]:
    rows = []
    for division, tag_slug in TAG_SLUGS.items():
        for event in get_open_events(tag_slug):
            for m in event.get("markets", []):
                sub_match = question_re.match(m.get("question", ""))
                if not sub_match:
                    continue
                home_team, away_team, name, line = sub_match.group(1), sub_match.group(2), sub_match.group(3), float(sub_match.group(4))
                if name == home_team:
                    team = home_team
                elif name == away_team:
                    team = away_team
                else:
                    continue
                prices = extract_market_prices(m)
                outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
                if outcomes != ["Over", "Under"] or len(outcome_prices) != 2:
                    continue
                rows.append({
                    "event_slug": event.get("slug", ""), "division": division,
                    "home_team": home_team, "away_team": away_team, "team": team, "line": line,
                    "over_price": outcome_prices[0], "condition_id": prices["condition_id"],
                    "volume": prices["volume"],
                        "raw_bid": prices["best_bid"],
                        "raw_ask": prices["best_ask"], "status": _market_status(m),
                })
    return rows


def get_first_half_team_total_markets() -> list[dict]:
    return _get_half_team_total_markets(_HALF1_TEAM_TOTAL_QUESTION_RE)


def get_second_half_team_total_markets() -> list[dict]:
    return _get_half_team_total_markets(_HALF2_TEAM_TOTAL_QUESTION_RE)


def _get_3way_side_event_markets(slug_suffix: str, none_label: str, none_side: str) -> list[dict]:
    """Shared by First Half/Second Half Winner (dedicated "-halftime-
    result"/"-second-half-result" sibling events, confirmed live: 3 markets,
    groupItemTitle = team name or "Draw") and First Team To Score
    ("-first-to-score", confirmed live: groupItemTitle = team name or
    "Neither"). Genuinely different event-discovery shape from the
    "-more-markets" bundle above -- these are their OWN top-level events
    under the same tag_slug query, identified by a real, confirmed slug
    suffix rather than a question-text regex.

    REAL BUG this fixes (caught live 2026-07-19, same day, while verifying
    model_prob actually computes for these rows): the third side was
    ALWAYS hardcoded to "none" regardless of which market this really is --
    Winner's own real "Draw" case needs side="draw" (the value
    soccer_markets.py::_half_moneyline_model_prob actually dispatches on,
    same as moneyline_3way's own "draw"), not "none" (FTTS's own real
    tie-analogue). Every Polymarket first_half_winner/second_half_winner
    Draw row silently got model_prob=None from this the whole time it
    existed -- caught before ever shipping to a live user-facing check,
    not by a user report."""
    rows = []
    for division, tag_slug in TAG_SLUGS.items():
        for event in get_open_events(tag_slug):
            slug = event.get("slug", "")
            if not slug.endswith(slug_suffix):
                continue
            title = event.get("title", "")
            base_title = title
            for suffix in (" - Halftime Result", " - Second Half Result", " - First Team to Score"):
                base_title = base_title.replace(suffix, "")
            if " vs. " not in base_title:
                continue
            home_team, _, away_team = base_title.partition(" vs. ")
            home_team, away_team = home_team.strip(), away_team.strip()
            for m in event.get("markets", []):
                group_title = m.get("groupItemTitle") or ""
                if group_title == none_label:
                    side, team = none_side, None
                elif group_title == home_team:
                    side, team = "home", home_team
                elif group_title == away_team:
                    side, team = "away", away_team
                else:
                    continue
                prices = extract_market_prices(m)
                outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
                if outcomes != ["Yes", "No"] or len(outcome_prices) != 2:
                    continue
                rows.append({
                    "event_slug": slug, "division": division,
                    "home_team": home_team, "away_team": away_team, "side": side, "team": team,
                    "yes_price": outcome_prices[0], "condition_id": prices["condition_id"],
                    "volume": prices["volume"],
                        "raw_bid": prices["best_bid"],
                        "raw_ask": prices["best_ask"], "status": _market_status(m),
                })
    return rows


def get_first_half_markets() -> list[dict]:
    return _get_3way_side_event_markets("-halftime-result", "Draw", "draw")


def get_second_half_markets() -> list[dict]:
    return _get_3way_side_event_markets("-second-half-result", "Draw", "draw")


def get_ftts_markets() -> list[dict]:
    return _get_3way_side_event_markets("-first-to-score", "Neither", "none")


_CORRECT_SCORE_QUESTION_RE = re.compile(r"^Exact Score: (.+?) (\d+) - (\d+) (.+?)\?$")


def get_correct_score_markets() -> list[dict]:
    """Dedicated "-exact-score" sibling event, confirmed live (e.g. "Exact
    Score: Seattle Sounders FC 2 - 1 Real Salt Lake?", one market per real
    scoreline)."""
    rows = []
    for division, tag_slug in TAG_SLUGS.items():
        for event in get_open_events(tag_slug):
            if not event.get("slug", "").endswith("-exact-score"):
                continue
            for m in event.get("markets", []):
                sub_match = _CORRECT_SCORE_QUESTION_RE.match(m.get("question", ""))
                if not sub_match:
                    continue
                home_team, home_score, away_score, away_team = sub_match.group(1), int(sub_match.group(2)), int(sub_match.group(3)), sub_match.group(4)
                prices = extract_market_prices(m)
                outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
                if outcomes != ["Yes", "No"] or len(outcome_prices) != 2:
                    continue
                rows.append({
                    "event_slug": event.get("slug", ""), "division": division,
                    "home_team": home_team, "away_team": away_team,
                    "home_score": home_score, "away_score": away_score,
                    "yes_price": outcome_prices[0], "condition_id": prices["condition_id"],
                    "volume": prices["volume"],
                        "raw_bid": prices["best_bid"],
                        "raw_ask": prices["best_ask"], "status": _market_status(m),
                })
    return rows


# --------------------------------------------------------------------------
# LEAGUE-TITLE FUTURES (2026-08-12)
#
# Fetched BY EVENT SLUG, not by tag_slug like every match market above. That is
# deliberate: these are season-winner events discovered by catalog_scan, and
# their slugs are exact and known, so a per-slug fetch cannot silently return
# the wrong league the way a guessed tag can. TAG_SLUGS' own comment records
# how unguessable Polymarket tags are ("liga-portugal", "portugal",
# "championship" and "efl" all return 0).
#
# ONLY LEAGUES THIS APP ALREADY RATES ARE LISTED. simulate_season is
# league-agnostic -- it builds its own round-robin and takes its team list from
# the ingested markets -- so a league prices the moment its rows appear AND its
# Elo pool exists. Listing a league we cannot rate would just add permanently
# unpriced rows.
#
# N1 IS HERE NOW AND THAT IS A REAL CHANGE: TAG_SLUGS above documents Eredivisie
# as "Polymarket has never listed the league at all" (checked 2026-08-07, 0 open
# AND 0 closed events) with an explicit "re-check if Polymarket adds Dutch
# football". They have. It is also on Kalshi, so its rows must go through the
# cross-platform duplicate cap or the same title gets staked twice.
#
# LIQUIDITY CAVEAT, measured 2026-08-12: every one of these events listed that
# day and reported 0 volume with live two-sided quotes (18-30 legs carrying a
# real bestBid). Zero volume here means "no trades yet", NOT a dead book -- but
# has_real_trading will correctly gate them out of staking until a book forms.
LEAGUE_WINNER_EVENT_SLUGS = {
    "SWE1": "2026-soccer-allsvenskan-sweden-winner",
    "ARG1": "2026-soccer-liga-profesional-argentina-winner",
    "D2":   "2027-soccer-2-bundesliga-winner",
    "DNK1": "2027-soccer-denmark-superliga-winner",
    "JPN1": "2027-soccer-japan-j-league-winner",
    "F2":   "2027-soccer-ligue-2-winner",
    "SC0":  "2027-soccer-scottish-premiership-winner",
    "T1":   "2027-soccer-sper-lig-winner",
    "N1":   "2027-soccer-eredivisie-winner",
    # WAVE 2 (2026-08-12): leagues football-data.co.uk does not carry, whose
    # ratings now come from the ESPN crawl in
    # scripts/build_espn_soccer_league_caches.py (27,601 matches, 2019-2026).
    # Held back from the first wave on purpose until the club-form resolver
    # landed -- without it these would have been ~400 permanently unpriced rows,
    # since ESPN's own spellings differ from the market's the same way
    # football-data's do.
    "COL1": "2026-soccer-colombia-primera-a-finalizacion-winner",
    "USL1": "2026-soccer-usl-championship-winner",
    "URU1": "2026-soccer-uruguayan-primera-divisin-winner",
    "ROU1": "2027-soccer-romania-superliga-winner",
    "GUA1": "2026-soccer-liga-nacional-guatemala-winner",
    "ECU1": "2026-soccer-ligapro-serie-a-ecuador-winner",
    "CRC1": "2026-soccer-liga-fpd-costa-rica-winner",
    "VEN1": "2026-soccer-venezuelan-primera-divisin-winner",
    "KSA1": "2027-soccer-saudi-professional-league-winner",
    "RSA1": "2027-soccer-south-africa-premiership-winner",
    "AUT1": "2027-soccer-austrian-bundesliga-winner",
    "SUI1": "2027-soccer-swiss-super-league-winner",
    "AUS1": "2027-soccer-a-league-soccer-winner",
    "IRL1": "2026-soccer-league-of-ireland-premier-division-winner",
    "NWSL": "2026-soccer-nwsl-winner",
}


# "Team with Most Clean Sheets", by exact event slug for the same reason
# LEAGUE_WINNER_EVENT_SLUGS is: asking by tag can silently return the wrong
# league, and a season futures market priced for the wrong league is worse than
# an absent one.
#
# ONLY LEAGUES THIS APP RATES. simulate_season is league-agnostic and prices a
# league the moment its rows and its Elo pool both exist, so an unrated league
# would just add permanent blanks -- the same rule
# refresh_polymarket_soccer_futures already states for league_winner. Polymarket
# lists this market for ~35 more leagues (Iceland, Estonia, Uzbekistan, ...);
# they stay out until those leagues are rated.
MOST_CLEAN_SHEETS_EVENT_SLUGS = {
    # SP1 AND F1 ARE DELIBERATELY ABSENT. Their events look identical by slug
    # and title ("LaLiga: Most Clean Sheets 2026-27") but ask a DIFFERENT
    # QUESTION: "Will Thibaut Courtois (Real Madrid) record the most clean
    # sheets" -- a GOALKEEPER market, not a team one. 54 and 55 legs against a
    # 20- and 18-team league is the tell; 13-14 legs name a person and 26 are
    # placeholders ("Player A" ... "Player Z") that are nobody at all.
    #
    # simulate_season produces a per-TEAM clean-sheet distribution, so wiring
    # these would price a keeper as if he were his club -- ignoring rotation,
    # injury and transfers -- and would hand the sim names it has never rated.
    # A goalkeeper market is not a lesser version of the team market, it is a
    # different one, and this app has no keeper-level model. Every other league
    # here returns 0 named-person and 0 placeholder legs.
    "E0":   "premier-league-team-most-clean-sheets-2026-27",
    "I1":   "serie-a-team-most-clean-sheets-2026-27",
    "N1":   "eredivisie-team-most-clean-sheets-2026-27",
    "P1":   "primeira-liga-team-most-clean-sheets-2026-27",
    "T1":   "super-lig-team-most-clean-sheets-2026-27",
    "SC0":  "scottish-premiership-team-most-clean-sheets-2026-27",
    "BRA1": "brazil-serie-a-team-most-clean-sheets-2026",
    "ARG1": "liga-profesional-argentina-team-most-clean-sheets-2026",
    "JPN1": "japan-j-league-team-most-clean-sheets-2026-27",
    "CHN1": "chinese-super-league-team-most-clean-sheets-2026",
    "NOR1": "norway-eliteserien-team-most-clean-sheets-2026",
    "SWE1": "allsvenskan-sweden-team-most-clean-sheets-2026",
}


def get_most_clean_sheets_markets() -> list[dict]:
    """One row per (league, team) leg of each most-clean-sheets event.

    Same shape and same skip rule as get_league_winner_markets below: a leg with
    no usable Yes price is dropped rather than defaulted, because an outright
    carrying a guessed price is worse than one that is simply absent.
    """
    from app.clients.polymarket_client import GAMMA as _GAMMA, get_json

    rows = []
    for division, slug in MOST_CLEAN_SHEETS_EVENT_SLUGS.items():
        try:
            event = get_json(f"{_GAMMA}/events/slug/{slug}")
        except Exception:
            continue  # one missing league must not cost the others
        group_label = event.get("title") or slug
        for m in event.get("markets", []):
            team = (m.get("groupItemTitle") or "").strip()
            if not team:
                continue
            prices = extract_market_prices(m)
            outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
            if "Yes" not in outcomes or not outcome_prices:
                continue
            yes_idx = outcomes.index("Yes")
            if yes_idx >= len(outcome_prices):
                continue
            rows.append({
                "event_slug": slug,
                "division": division,
                "group_label": group_label,
                "team": team,
                "yes_price": outcome_prices[yes_idx],
                "condition_id": prices["condition_id"],
                "volume": prices["volume"],
                "raw_bid": prices["best_bid"],
                "raw_ask": prices["best_ask"],
                "status": _market_status(m),
            })
    return rows


def get_league_winner_markets() -> list[dict]:
    """One row per (league, team) leg of each season-title event.

    Skips a leg with no usable Yes price rather than defaulting it -- an
    outright with a guessed price is worse than an absent one.
    """
    from app.clients.polymarket_client import GAMMA as _GAMMA, get_json

    rows = []
    for division, slug in LEAGUE_WINNER_EVENT_SLUGS.items():
        try:
            event = get_json(f"{_GAMMA}/events/slug/{slug}")
        except Exception:
            continue  # one missing league must not cost the others
        group_label = event.get("title") or slug
        for m in event.get("markets", []):
            team = (m.get("groupItemTitle") or "").strip()
            if not team:
                continue
            prices = extract_market_prices(m)
            outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
            if "Yes" not in outcomes or not outcome_prices:
                continue
            yes_idx = outcomes.index("Yes")
            if yes_idx >= len(outcome_prices):
                continue
            rows.append({
                "event_slug": slug,
                "division": division,
                "group_label": group_label,
                "team": team,
                "yes_price": outcome_prices[yes_idx],
                "condition_id": prices["condition_id"],
                "volume": prices["volume"],
                "raw_bid": prices["best_bid"],
                "raw_ask": prices["best_ask"],
                "status": _market_status(m),
            })
    return rows

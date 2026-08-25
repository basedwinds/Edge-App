"""First-pass auto-triage for newly-detected catalog markets (see
catalog_scan.py). Classifies each new series/event by what KIND of market it
is, from its identifier + title, so the New Markets page can tell at a glance
which bucket a thing falls in instead of the user opening each one.

This is deliberately NOT auto-ingestion. The app never starts pricing a market
without a human reviewing its real resolution rules ("never guess a number"),
and in practice the genuinely auto-priceable case -- an individual game/match
in a market type already modeled -- never even reaches this list: catalog_scan
skips per-game instances (_is_per_game) and the sport pollers already price
them. So what lands here is, by construction, structurally new. The classifier
just says WHY each one needs review (news/media -> LLM layer; stat-leader ->
no player-stat model; futures/bracket -> needs a futures model), and reserves
the one "an existing model could actually price this" verdict for the rare
head-to-head-outcome series that slips through, which is the only safe
auto-price candidate.

Keyword-based and conservative on purpose: a wrong "auto-priceable" call is the
costly one (it invites ingesting a market whose rules differ), so anything
ambiguous falls through to "review", never to auto-priceable.
"""
from __future__ import annotations

# (category, human note). Ordered by priority -- first matching bucket wins, so
# the more specific / higher-risk buckets are checked before the generic ones.
NEWS = "news"
STAT_LEADER = "stat_leader"
FUTURES = "futures"
MATCH_OUTCOME = "match_outcome"
REVIEW = "review"

_NOTES = {
    NEWS: "Non-statistical (news / media / transaction) market — this needs the "
          "planned LLM layer for non-stat markets, not an Elo model. Can't be auto-priced.",
    STAT_LEADER: "Player stat or award prop (leader / MVP / most-X). There's no "
                 "player-stat projection model behind this app, so it can't be auto-priced.",
    FUTURES: "Season / tournament futures or advancement (champion / qualify / "
             "stage / relegation). Needs a per-sport futures or bracket model to price.",
    MATCH_OUTCOME: "Looks like a head-to-head match outcome in a sport this app "
                   "models — an existing Elo model could price this once its resolution "
                   "rules are confirmed. The only auto-price candidate here.",
    REVIEW: "Unclassified — open it and review its resolution rules before doing anything.",
}

# Buckets from catalog_scan.py's catch-all -- by construction these belong to
# no sport this app models. A Dota 2 or CFL series reads as a clean head-to-
# head by keyword ("Dota 2 Map Winner" hits _MATCH_KW's "map winner"), so it
# would otherwise earn the one auto-priceable verdict while there is no Elo,
# no ratings, and no ingestion behind it at all. The keyword read is still
# worth showing -- it says what KIND of market this is -- but match_outcome
# has to be unreachable here, since that verdict means "an EXISTING model
# could price this" and no existing model covers these sports.
UNTRACKED_SPORTS = {"other"}

_UNTRACKED_MATCH_NOTE = (
    "Looks like a head-to-head match outcome, but it's in a sport this app doesn't "
    "model at all — there's no Elo or ingestion behind it, so it can't be auto-priced. "
    "Review it as a new-sport build, not a new market type in an existing one."
)

# News / media / roster-transaction language -- these are propositions about
# announcements, not game results.
_NEWS_KW = (
    "announce", "outlet", "report", "contract", "sign ", "signing", "trade",
    "traded", "buyout", "extension", "hire", "fired", "waive", "release",
    "next team", "to join", "rumor", "press conference",
)
# Player stat leaders / individual awards.
_STAT_KW = (
    "leader", "leading", "mvp", "most valuable", "rookie of the year", "dpoy",
    "cy young", "yards", "touchdown", "reception", "rushing", "passing",
    "assists", "rebound", "points leader", "home run", "strikeout", "kills leader",
    "top scorer", "golden boot", "award",
)
# Season/tournament-long outcomes and bracket advancement.
_FUTURES_KW = (
    "champion", "championship", "to win the", "title", "world series", "super bowl",
    "finals", "conference winner", "division winner", "qualify", "advance",
    "stage of elim", "stage 2", "round of", "group ", "relegation", "promotion",
    "make the playoff", "reach the", "worlds", "cup winner", "season ", "specials",
)
# Head-to-head match-outcome language (the auto-price candidate). Kept narrow.
_MATCH_KW = (
    "moneyline", "map winner", "game winner", "to beat", " vs ", " vs. ",
    "match winner", "series winner", "spread", "total maps", "over/under",
)


# Tokens that mean SOCCER specifically, used only to sharpen the human note on a
# catch-all entry -- never to change its sport or category.
#
# WHY THIS EXISTS. The catch-all is the exact COMPLEMENT of the wired sport
# matchers, so a league this app does not ingest lands in "other" correctly. But
# it then gets _UNTRACKED_MATCH_NOTE, which says "review it as a new-SPORT
# build" -- and that is wrong for a new LEAGUE in a sport we already model. The
# wrong framing has real cost: 45 KXNCAAF series were once bulk-dismissed as
# untracked sports, SIX of which this app now actively prices.
#
# Measured 2026-08-23: the catch-all held KXISRPLBTTS/SPREAD/TOTAL (Israeli
# Premier League), KXEGYPLBTTS/SPREAD/TOTAL (Egyptian), KXTACAPORTGAME/ADVANCE
# (Taca de Portugal) and KXGERSC1H* (German Supercup) -- complete, priceable
# market sets for leagues that are simply unwired, sitting under a note telling
# a reader to treat them as a different sport.
#
# Deliberately soccer-only and token-based: these strings are league and cup
# names that do not appear in the other sports this app tracks. It costs nothing
# to be wrong (the note is advisory) and the alternative -- inferring the sport
# itself -- would be a guess with consequences.
_SOCCER_HINT_KW = (
    "btts", "taca", "copa", "coppa", "pokal", "liga", "serie", "eredivisie",
    "bundesliga", "ligue", "epl", "efl", "futbol", "calcio", "allsvenskan",
    "superlig", "primeira", "eliteserien", "brasileiro", "concacaf", "conmebol",
)

# SOCCER SEASON FUTURES. _SOCCER_HINT_KW above is a list of LEAGUE names, which
# does not scale: the New Markets backlog on 2026-08-24 held 96+ soccer entries
# across 36+ leagues -- Chance Liga (Czechia), Bolivia LFPB, Besta deild karla,
# Morocco Botola Pro, Premium Liiga (Estonia) -- and every keyword sweep I wrote
# under-counted, missing Spanish "primera" while having Portuguese "primeira".
# Chasing the world's league names is not a strategy.
#
# The PROPOSITIONS are the reliable signal instead. "Teams relegated", "Team
# promoted to", "most clean sheets", "qualify for the UEFA Champions League" are
# soccer regardless of which country's league is in front of them.
#
# SPLIT BY WHETHER A MODEL EXISTS, because that is the only thing the reader
# actually needs to decide. Half of that backlog is blocked purely on a league
# not being wired; the other half has no model at any league.
_SOCCER_MODELLED_KW = (
    "relegat", "champions league", "europa league", "conference league",
)
_SOCCER_UNMODELLED_KW = (
    "clean sheet", "place finish", "relegation survivor", "promoted to", "promotion",
    "golden boot", "top scorer",
)

_SOCCER_FUTURES_MODELLED_NOTE = (
    "SOCCER season futures for a league this app may not ingest yet. The proposition "
    "itself IS modelled (relegation / league winner / UEFA qualification map onto the "
    "existing soccer futures pricing), so the only blockers are the league's series in "
    "kalshi_soccer_client or the Polymarket event slug, ratings history, and an ESPN "
    "settlement feed. Do NOT dismiss this as an untracked sport."
)

_SOCCER_FUTURES_UNMODELLED_NOTE = (
    "SOCCER season futures using a proposition this app has NO model for -- most clean "
    "sheets, Nth-place finish, and promotion are not derivable from the current season "
    "sim, and the promoted-club bridge was measured and REJECTED (an oracle rating was "
    "still 0.09 Brier worse than market). Wiring the league would not make this "
    "priceable. Dismiss unless the proposition itself gets built."
)

_UNWIRED_LEAGUE_NOTE = (
    "Looks like a match-level SOCCER market for a league this app does not ingest yet. "
    "This is a new LEAGUE in a sport already modelled -- not a new sport. The pricing "
    "already exists (moneyline/spread/total/btts); what is missing is the league's series "
    "in kalshi_soccer_client, plus ratings history and an ESPN settlement feed. Do NOT "
    "dismiss it as an untracked sport."
)


def _hay(identifier: str, title: str) -> str:
    return f"{identifier or ''} || {title or ''}".lower()


def classify(identifier: str, title: str, sport: str) -> tuple[str, str]:
    """Return (category, human_note). Conservative: news/stat/futures are
    checked BEFORE match_outcome so an award or futures market with an
    incidental team name never gets mislabeled auto-priceable."""
    hay = _hay(identifier, title)
    if any(k in hay for k in _NEWS_KW):
        return NEWS, _NOTES[NEWS]
    if any(k in hay for k in _STAT_KW):
        return STAT_LEADER, _NOTES[STAT_LEADER]
    if any(k in hay for k in _FUTURES_KW):
        # SOCCER FUTURES GET A SOCCER NOTE. This branch used to swallow them all:
        # the soccer hint below is unreachable for anything matching _FUTURES_KW,
        # which is the SAME short-circuit already documented further down for the
        # match-outcome branch -- written once, and then re-introduced here.
        # Measured 2026-08-24: 96+ of the 200 unclassified "other" entries were
        # soccer season futures reading as generic futures.
        if any(k in hay for k in _SOCCER_UNMODELLED_KW):
            return FUTURES, _SOCCER_FUTURES_UNMODELLED_NOTE
        if any(k in hay for k in _SOCCER_MODELLED_KW):
            return FUTURES, _SOCCER_FUTURES_MODELLED_NOTE
        return FUTURES, _NOTES[FUTURES]
    soccer_hint = sport in UNTRACKED_SPORTS and any(k in hay for k in _SOCCER_HINT_KW)
    if any(k in hay for k in _MATCH_KW):
        if sport in UNTRACKED_SPORTS:
            if soccer_hint:
                return REVIEW, _UNWIRED_LEAGUE_NOTE
            return REVIEW, _UNTRACKED_MATCH_NOTE
        return MATCH_OUTCOME, _NOTES[MATCH_OUTCOME]
    # ALSO on the plain-review fallback, not only inside the match-outcome
    # branch. The clearest cases never reach that branch: "BTTS" and "Taca de
    # Portugal Game" match no _MATCH_KW at all, so a first cut of this hint sat
    # in a branch those entries could not get to and changed nothing for exactly
    # the rows it was written for.
    if any(k in hay for k in _SOCCER_UNMODELLED_KW):
        return REVIEW, _SOCCER_FUTURES_UNMODELLED_NOTE
    if any(k in hay for k in _SOCCER_MODELLED_KW):
        return REVIEW, _SOCCER_FUTURES_MODELLED_NOTE
    if soccer_hint:
        return REVIEW, _UNWIRED_LEAGUE_NOTE
    return REVIEW, _NOTES[REVIEW]


def is_auto_priceable(category: str) -> bool:
    """Only a clean head-to-head match outcome is ever a safe auto-price
    candidate -- and even that stays a human-confirmed suggestion, never a
    silent ingest."""
    return category == MATCH_OUTCOME

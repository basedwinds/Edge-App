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
        return FUTURES, _NOTES[FUTURES]
    if any(k in hay for k in _MATCH_KW):
        return MATCH_OUTCOME, _NOTES[MATCH_OUTCOME]
    return REVIEW, _NOTES[REVIEW]


def is_auto_priceable(category: str) -> bool:
    """Only a clean head-to-head match outcome is ever a safe auto-price
    candidate -- and even that stays a human-confirmed suggestion, never a
    silent ingest."""
    return category == MATCH_OUTCOME

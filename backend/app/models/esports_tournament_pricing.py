"""Prices esports tournament_winner markets (CS2 / Valorant / LoL "Event:
Winner") off the Elo-seeded single-elim Monte Carlo in tournament_sim_esports.py.

The field for each tournament is read straight from the market inventory: every
tournament_winner market under the same group_label names one candidate team
(Market.team), so the set of those teams IS the field -- no separate bracket/
participant scrape needed. Each title's own elo_service supplies both the seed
rating (get_team_rating) and the pairwise series win prob (get_series_distribution).

Teams whose name doesn't resolve to a rating (naming mismatch between the
platform's label and this app's Elo keys, or a genuinely unrated team) are
dropped from the field and simply left unpriced -- the same "no baseline rather
than a guessed number" convention used everywhere else. Prices are normalized
across only the RATED field, so they still sum to ~1 among priceable teams.
"""
from collections import defaultdict

import logging
import re

from app.models.tournament_sim_esports import (
    DEFAULT_BEST_OF, DEFAULT_TRIALS, simulate_tournament_winner, simulate_with_group_stage,
)

log = logging.getLogger("esports_tournament_pricing")

# Words that carry no identity when matching a market's label to an event page.
_STOP = {"the", "a", "of", "to", "winner", "win", "wins", "2026", "2027"}
# Kalshi/Polymarket abbreviate regions that vlr.gg spells out.
_ALIAS = {"amer": "americas", "na": "north"}
# Real matches score 1.00 and unrelated labels score 0.00, so anything in the
# middle is ambiguous and not worth acting on.
_MIN_EVENT_MATCH = 0.5


def _tokens(text: str) -> set[str]:
    raw = {w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if w and w not in _STOP}
    return {_ALIAS.get(w, w) for w in raw}


def find_event_path(label: str, events: list[tuple[str, str]]) -> str | None:
    """The vlr.gg event page whose slug best matches this market's label."""
    want = _tokens(label)
    if not want:
        return None
    best_score, best_path = 0.0, None
    for _eid, path in events:
        have = _tokens(path.rsplit("/", 1)[-1])
        score = len(want & have) / max(1, len(want | have))
        if score > best_score:
            best_score, best_path = score, path
    return best_path if best_score >= _MIN_EVENT_MATCH else None

# A single-elim bracket sim only models a SINGLE real event. Two kinds of
# "tournament_winner" market are NOT that and must be skipped, or the sim
# produces nonsense (confirmed live: a 48-team "Qualify For Champs" field gave
# every team ~2% and a market that summed to 2.5x):
#   * Season-long AGGREGATE outcomes -- "win ANY international in 2026",
#     "qualify for Champs" -- which span many separate events, not one bracket.
#   * Anything with an implausibly large field for a single event.
# The keyword guard catches the aggregates by name; the size cap is a backstop.
_AGGREGATE_PHRASES = (
    "qualify", "win an international", "to win a", "champs 20", "champions 20",
    "any international", "make champs", "make it to",
    # Not aggregates but equally not a bracket, and each was being priced live:
    #   "LCK Legend Group Win Totals 2026 Rounds 3-4" -- asks how many GAMES a
    #     team wins, not who lifts the trophy; a bracket sim answers a different
    #     question and its numbers do not even sum the same way.
    #   "Worlds 2026 Winning Region" -- the candidates are REGIONS ("CBLOL
    #     (Brazil)"), not teams. It was priced at 12.3% off whatever the Elo
    #     lookup happened to fuzzy-match, which is worse than being unpriced.
    #   "Team to Make Grand Finals" -- reaching the final is top-2, not winning.
    "win total", "winning region", "grand final", "power ranking",
)
_MAX_SINGLE_EVENT_FIELD = 32


def _is_single_event(label: str, field_size: int) -> bool:
    low = (label or "").lower()
    if any(p in low for p in _AGGREGATE_PHRASES):
        return False
    return 2 <= field_size <= _MAX_SINGLE_EVENT_FIELD


# Group labels this model must NOT price, matched case-insensitively as
# substrings of the market's group_label.
#
# REAL BUG THIS FIXES, reported by a user 2026-08-09 who could not find any
# information online about a "VCT Partnership 2026 EMEA: Team Falcons" pick the
# app had recommended. They were right to be suspicious. The market is real, but
# it is Polymarket's "VCT Partnership 2027: EMEA" -- a question about which orgs
# RIOT WILL GRANT a franchise slot to, whose field includes FC Barcelona,
# Joblife, Stallions, Pcific and Fokus. It is a business decision, not a
# bracket, and no amount of match history predicts it.
#
# Because the row carried market_type "tournament_winner", this model priced it
# as though the field were playing a tournament: Team Falcons came out at 0.682
# against a market price of 0.25, a +43pp "edge" that was the single largest in
# the block and got staked. The model was not wrong about Falcons being a strong
# Valorant team; it was answering a different question entirely.
#
# The same misclassification covers several other Polymarket novelty markets --
# player pentakills, soloqueue challenges, roster-change news, shortest-game
# props and power-ranking publications -- roughly 350 rows across the three
# titles, all being scored by a bracket simulator.
#
# EXCLUSION FAILS SAFE, which is why a substring list is acceptable here when
# this project normally distrusts them: a false match leaves a market UNPRICED
# and therefore unstakeable, while a false miss prices a business decision as a
# bracket. The asymmetry runs the opposite way to team-name aliasing, where a
# wrong match silently stakes money on the wrong entity.
NON_COMPETITION_LABEL_MARKERS = (
    "partnership",      # franchise/slot allocation, decided by the publisher
    "roster change",    # transfer news
    "will leave",       # transfer news
    "power rankings",   # a published ranking, not a result
    "solo q",           # soloqueue ladder, not team play
    "soloq",
    "shortest",         # shortest-game novelty prop
    "to penta",         # individual player feat
    "player to",        # individual player feat
)


def is_competition_outcome(group_label: str | None) -> bool:
    """False for markets that are not a team competition result at all.

    Deliberately conservative in the safe direction -- see
    NON_COMPETITION_LABEL_MARKERS."""
    label = (group_label or "").lower()
    return not any(marker in label for marker in NON_COMPETITION_LABEL_MARKERS)


NON_COMPETITION_REASON = (
    "Not priced: this is not a team-competition result. Polymarket lists it under the same "
    "market type as a tournament winner, but the question is a franchise/partnership slot, a "
    "roster or transfer announcement, an individual player feat, a soloqueue ladder or a "
    "novelty stat -- none of which a match-history model can speak to. Left unpriced on "
    "purpose rather than scored by a bracket simulator that would answer a different question."
)


AGGREGATE_REASON = (
    "Not priced: this asks about a season-long outcome spanning many separate events "
    "(qualifying, or winning any one of several tournaments), not a single bracket. The "
    "bracket simulator models ONE event, and forcing it on a field this shape produced "
    "probabilities that summed to 2.5x when it was tried. Left unpriced rather than "
    "answered with a number that does not mean what it appears to."
)


def skip_reason(group_label: str | None, field_size: int = 0) -> str | None:
    """Why this futures group is not priced, or None if it should be.

    One place for both refusals, so the three esports routers cannot drift from
    the pricing function or from each other -- the same drift that left 93
    Valorant rows blank with no explanation while 95 others had one."""
    if not is_competition_outcome(group_label):
        return NON_COMPETITION_REASON
    if not _is_single_event(group_label or "", field_size):
        return AGGREGATE_REASON
    return None


def price_tournament_winners(markets, elo_service, best_of: int = DEFAULT_BEST_OF,
                             trials: int = DEFAULT_TRIALS, event_state_for=None) -> dict[int, float]:
    """Returns {market_id: model_prob} for the tournament_winner markets it can
    price. `elo_service` is a title's elo_service_* module (needs
    get_team_rating + get_series_distribution). Markets whose team is unrated,
    whose tournament is a season-long aggregate (not a single bracket), or in a
    <2-rated-team field, are simply absent from the result."""
    by_tournament: dict[str, list] = defaultdict(list)
    skipped_labels: set[str] = set()
    for m in markets:
        label = m.group_label or ""
        if not is_competition_outcome(label):
            skipped_labels.add(label)
            continue  # not a bracket -- see NON_COMPETITION_LABEL_MARKERS
        by_tournament[label].append(m)
    if skipped_labels:
        log.info("esports futures: not pricing %d non-competition group(s): %s",
                 len(skipped_labels), ", ".join(sorted(skipped_labels)[:6]))

    def win_prob_fn(a: str, b: str) -> float | None:
        dist = elo_service.get_series_distribution(a, b, best_of)
        return dist.prob_series_win_a() if dist is not None else None

    out: dict[int, float] = {}
    for _label, group in by_tournament.items():
        # Build the rated field, one entry per distinct team (dedupe defensively
        # in case a team somehow has two markets in one tournament).
        seen: set[str] = set()
        field: list[tuple[str, float]] = []
        for m in group:
            if not m.team or m.team in seen:
                continue
            rating = elo_service.get_team_rating(m.team)
            if rating is not None:
                seen.add(m.team)
                field.append((m.team, rating))

        if not _is_single_event(_label, len(group)):
            continue  # season-long aggregate / not a single bracket -- leave unpriced

        # If the event's group stage has already been played, USE IT. Pricing a
        # tournament purely off ratings ignores the most informative thing that
        # has happened in it: on VCT EMEA Stage 2 that put Karmine Corp, who WON
        # their group at 4-1, tenth of twelve at 0.9%, and gave FNATIC 9.6%
        # despite finishing fifth and missing the playoff entirely.
        #
        # Falls back to the rating-seeded bracket whenever the event can't be
        # identified, has no group stage, or the standings don't cover the whole
        # field -- a partially-read page must not silently eliminate teams.
        sim = None
        state = event_state_for(_label) if event_state_for else None
        if state:
            standings = state.get("standings") or {}
            slots = (state.get("format") or {}).get("slots") or 0
            sim = simulate_with_group_stage(field, standings, slots, win_prob_fn, trials=trials)
            if sim is not None:
                log.info("tournament %r priced from real group standings (%d slots)", _label, slots)
        if sim is None:
            sim = simulate_tournament_winner(field, win_prob_fn, trials=trials)
        if sim is None:
            continue
        for m in group:
            if m.team and m.team in sim:
                out[m.id] = round(sim[m.team], 4)
    return out

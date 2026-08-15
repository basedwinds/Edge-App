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


THIN_HISTORY_REASON = (
    "Not priced: this team is rated, but on too few games for the rating to be trusted here. "
    "The bracket simulator holds a field to the same minimum match count that gating a single "
    "match uses, because a team seeded off three or four games would carry that noise through "
    "every round of the simulation. Its rating is still shown; it is simply not staked on."
)


INCOMPLETE_FIELD_REASON = (
    "Not priced: too much of this field is unrated. The bracket simulator can only enter teams "
    "it has a rating for, and it answers \"who wins among the entrants\" -- so every dropped team's "
    "win probability is silently handed to the ones that remain. Here the market prices the "
    "missing teams high enough that pricing the remainder would overstate them by more than "
    "the staking gate, so the group is left unpriced rather than answered with an inflated number."
)


def _covered_mass(group, priced_ids, implied_by_market) -> float | None:
    """Share of the group's market probability sitting on teams the sim could rate.

    WHY THIS EXISTS. simulate_tournament_winner normalises over the field it is
    GIVEN, so its output always sums to 1.0 -- across the rated teams only. Every
    unrated team's win probability is therefore redistributed onto the survivors,
    and the resulting "edge" is an artifact of who happened to be rated. Measured
    live: LCK Challengers League had 3 of 10 teams rated holding 14.9% of the
    market's mass, and the sim gave those three 100% of the title -- a 6.7x
    inflation that put a +27pp edge and a real $2.50 stake on KT Rolster
    Challengers. Same defect class as the sim-leg coherence checks in
    integrity_checks.py: mutually exclusive legs must sum to a known constant.

    The market is used ONLY for this scalar -- how much probability the teams the
    model cannot rate collectively hold. The relative ordering among rated teams,
    which is where any edge comes from, stays entirely model-derived.

    Returns None when the distortion cannot be MEASURED -- i.e. some unrated leg
    has no price at all. An unpriced leg is not a zero-probability leg, and
    treating a missing snapshot as "nothing missing" would restore the exact bug
    this guards (presence is not sufficiency).
    """
    total = 0.0
    covered = 0.0
    for m in group:
        p = implied_by_market.get(m.id)
        if p is None:
            if m.id in priced_ids:
                continue  # a rated leg with no price only shrinks `covered`, safe
            return None   # an UNRATED leg with no price: the miss is unmeasurable
        total += p
        if m.id in priced_ids:
            covered += p
    if total <= 0:
        return None
    return covered / total


# Minimum share of the market's mass the rated teams must hold for the group to
# be priced at all. Below it the group is refused rather than rescaled.
# Rescaling is mathematically right at any coverage, but once the model speaks
# for a small minority of the field the SCALAR -- not the model -- is doing the
# work, and "who wins among these three" has drifted too far from the question
# the market is asking. 0.40 clears every legitimate field measured live (the
# worst was CBLOL/BLAST at ~87% covered, and Circuito Desafiante at 62%) and
# refuses only LCK Challengers League, where 3 rated teams held 14.9%.
MIN_FIELD_COVERAGE = 0.40


def price_tournament_winners(markets, elo_service, best_of: int = DEFAULT_BEST_OF,
                             trials: int = DEFAULT_TRIALS, event_state_for=None,
                             implied_by_market: dict[int, float | None] | None = None,
                             refusals: dict[str, str] | None = None,
                             unfielded: dict[int, str] | None = None,
                             progress_aware: set[str] | None = None) -> dict[int, float]:
    """Returns {market_id: model_prob} for the tournament_winner markets it can
    price. `elo_service` is a title's elo_service_* module (needs
    get_team_rating + get_series_distribution). Markets whose team is unrated,
    whose tournament is a season-long aggregate (not a single bracket), or in a
    <2-rated-team field, are simply absent from the result.

    `implied_by_market` maps market id -> market implied probability. Pass it
    whenever the caller has snapshots (all three routers do): it is what lets a
    partially-rated field be rescaled to the mass it actually covers instead of
    inheriting the dropped teams' probability. WITHOUT it a partial field is
    REFUSED rather than priced -- the conservative direction, so that a caller
    which forgets to pass prices cannot silently reintroduce the inflation.

    `refusals`, if passed, is FILLED IN with {group_label: reason} for every
    group the field-completeness gate turned away, so the routers can explain the
    blank rather than render an unexplained empty cell. A guard whose effect the
    user cannot see reads exactly like a guard that never ran.
    """
    implied_by_market = implied_by_market or {}
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
        # Prefer a FIELD-level rating lookup where the title has one. LoL keeps
        # two independently-trained pools and get_team_rating reads only the
        # clean Primary one, while win_prob_fn (get_series_distribution) falls
        # back to the expanded pool -- so the per-team lookup dropped teams the
        # pricer could price. get_field_ratings routes the whole field through
        # one pool, mirroring get_series_distribution's own pairwise rule.
        # Titles with a single pool (CS2, Valorant) have no such function and
        # keep the per-team path unchanged.
        _field_lookup = getattr(elo_service, "get_field_ratings", None)
        _ratings = (_field_lookup([m.team for m in group if m.team])
                    if _field_lookup else None)
        for m in group:
            if not m.team or m.team in seen:
                continue
            rating = (_ratings.get(m.team) if _ratings is not None
                      else elo_service.get_team_rating(m.team))
            if rating is not None:
                seen.add(m.team)
                field.append((m.team, rating))
            elif unfielded is not None:
                # WHY THE PRICER OWNS THIS TEXT. The routers used to infer the
                # blank themselves by re-asking get_team_rating, which is a
                # DIFFERENT question than the one the field builder asks -- a
                # team can hold a rating and still be too thin to field, and
                # that row then rendered a blank with no explanation at all.
                # Recording it here keeps the reason and the omission as one
                # decision instead of two that can drift.
                unfielded[m.id] = (THIN_HISTORY_REASON
                                   if elo_service.get_team_rating(m.team) is not None
                                   else "")

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
                # RECORDED, because the caller has to know. A group priced this
                # way has seen the results so far -- who is through, who is
                # already out. A group that falls through to the flat bracket
                # below has NOT: it re-simulates the whole event from ratings
                # every time, so a team eliminated yesterday keeps its full
                # pre-tournament win probability. Only the first kind is safe to
                # STAKE; see each router's `_progress_aware` gate.
                if progress_aware is not None:
                    progress_aware.add(_label)
        if sim is None:
            sim = simulate_tournament_winner(field, win_prob_fn, trials=trials)
        if sim is None:
            continue

        # FIELD COMPLETENESS. sim sums to 1.0 over the teams it was GIVEN, so if
        # any team was dropped for want of a rating the survivors have absorbed
        # its win probability. Rescale to the mass the rated teams actually hold
        # (or refuse the group outright when they hold too little); see
        # _covered_mass for the live 6.7x case this was found on.
        rated_names = {name for name, _ in field}
        rated_ids = {m.id for m in group if m.team in rated_names and m.team in sim}
        if len(rated_names) < len({m.team for m in group if m.team}):
            coverage = _covered_mass(group, rated_ids, implied_by_market)
            if coverage is None or coverage < MIN_FIELD_COVERAGE:
                log.info("tournament %r NOT priced: rated field covers %s of market mass "
                         "(need >= %.2f)", _label,
                         "an unmeasurable share" if coverage is None else f"{coverage:.1%}",
                         MIN_FIELD_COVERAGE)
                if refusals is not None:
                    refusals[_label] = INCOMPLETE_FIELD_REASON
                continue
            scale = coverage
        else:
            scale = 1.0

        for m in group:
            if m.team and m.team in sim:
                out[m.id] = round(sim[m.team] * scale, 4)
    return out

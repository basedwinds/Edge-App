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

from app.models.tournament_sim_esports import DEFAULT_BEST_OF, DEFAULT_TRIALS, simulate_tournament_winner

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


def price_tournament_winners(markets, elo_service, best_of: int = DEFAULT_BEST_OF,
                             trials: int = DEFAULT_TRIALS) -> dict[int, float]:
    """Returns {market_id: model_prob} for the tournament_winner markets it can
    price. `elo_service` is a title's elo_service_* module (needs
    get_team_rating + get_series_distribution). Markets whose team is unrated,
    whose tournament is a season-long aggregate (not a single bracket), or in a
    <2-rated-team field, are simply absent from the result."""
    by_tournament: dict[str, list] = defaultdict(list)
    for m in markets:
        by_tournament[m.group_label or ""].append(m)

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
        sim = simulate_tournament_winner(field, win_prob_fn, trials=trials)
        if sim is None:
            continue
        for m in group:
            if m.team and m.team in sim:
                out[m.id] = round(sim[m.team], 4)
    return out

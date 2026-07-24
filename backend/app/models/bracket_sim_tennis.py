"""Single-elimination bracket Monte Carlo for Tennis tournament-winner
futures (KXATP/KXWTA on Kalshi, "20XX {Slam} Winner" on Polymarket) --
parallel in spirit to season_sim.py's NFL/NBA/MLB season simulation, but
per-TOURNAMENT bracket rather than per-SEASON schedule, since Tennis has no
regular season to simulate the way team sports do.

Real, freely-scrapable bracket data (see tennisexplorer_client.py::
parse_draw_html, confirmed live 2026-07-19) gives the ACTUAL round-1 draw
plus, for any rounds already played, the REAL confirmed winners -- this
module starts the simulation from the DEEPEST fully-resolved round (already-
eliminated players simply don't appear there) rather than naively
re-simulating a tournament that's partway through, the same "don't predict
what's already known" discipline the rest of this app applies to in-progress
games/fights (see mlb_markets.py's `_game_already_started`).

Player identity is the one genuinely hard part: the draw only gives bare
surnames (plus seed/wildcard/qualifier tags, e.g. "Rublev [1]"), not the
"Surname I." key this app's offline-trained Elo ratings are keyed on. Same
category of collision risk already accepted elsewhere in this app (two
players sharing a surname within the same tour/gender) -- resolved here by
picking whichever of the surname's real rated keys has the HIGHER overall
Elo rating (checked against a real live collision, "Darderi" -> real ATP
player Luciano Darderi at 1993 vs an unrelated "darderi v." at 1374 -- a
huge, unambiguous gap, not a coin flip), since the more established player is
overwhelmingly more likely to be the one in a real tour-level draw. A surname
with NO rated match at all (a real, if rare, gap for a total tour debutant)
is dropped from the simulation entirely rather than guessed at -- the "no
baseline" convention this whole app already uses everywhere else.
"""
import random
import re

from app.ingestion.market_matcher_tennis import _fold
from app.models.baseline import elo_service_tennis

_SEED_TAG_RE = re.compile(r"\s*\[[^\]]*\]\s*$")

DEFAULT_TRIALS = 20000

Entrant = tuple[str, str] | None  # (display_name, elo_key), or None for a bye


def _strip_seed_tag(entry: str) -> str:
    return _SEED_TAG_RE.sub("", entry).strip()


def resolve_surname_to_key(surname: str) -> str | None:
    """Bare surname (e.g. "Van De Zandschulp") -> this app's "surname i."
    Elo key, or None if no rated player has that surname at all. See module
    docstring for the higher-rating tie-break on a real collision."""
    state = elo_service_tennis._cache.get("state")
    if state is None:
        return None
    normalized = _fold(surname)
    if not normalized:
        return None
    prefix = normalized + " "
    candidates = [
        key for key in state.overall_ratings
        if key.startswith(prefix) and re.match(r"^[a-z]\.$", key[len(prefix):])
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda k: state.overall_ratings[k])


def _deepest_resolved_round(rounds: list[list[str]]) -> list[str]:
    """The bracket's current REAL state -- deepest round whose entry count
    matches the expected size for its depth (draw_size >> depth). A round
    with FEWER real entries than expected means part of that round hasn't
    been played yet; falls back to the previous, fully-resolved round rather
    than guessing which specific match is still pending."""
    draw_size = len(rounds[0])
    resolved = rounds[0]
    for depth, round_entries in enumerate(rounds[1:], start=1):
        expected = draw_size >> depth
        if len(round_entries) != expected:
            break
        resolved = round_entries
    return resolved


def simulate_tournament(
    rounds: list[list[str]], surface: str | None = None, trials: int = DEFAULT_TRIALS
) -> dict[str, float] | None:
    """Returns {display_name: P(wins the tournament)} for every real
    (rating-resolved) entrant still alive as of the deepest fully-resolved
    round, or None if fewer than 2 such entrants remain (nothing left to
    simulate -- e.g. the final has already been played, or too few names
    resolved to a rating)."""
    current = _deepest_resolved_round(rounds)
    entrants: list[Entrant] = []
    for entry in current:
        if entry.strip().lower() == "bye":
            entrants.append(None)
            continue
        display_name = _strip_seed_tag(entry)
        key = resolve_surname_to_key(display_name)
        entrants.append((display_name, key) if key else None)

    real_entrants = [e for e in entrants if e is not None]
    if len(real_entrants) < 2:
        return None

    wins = {name: 0 for name, _ in real_entrants}
    for _ in range(trials):
        field = list(entrants)
        while len(field) > 1:
            next_round = []
            for i in range(0, len(field), 2):
                a = field[i]
                b = field[i + 1] if i + 1 < len(field) else None
                next_round.append(_simulate_match(a, b, surface))
            field = next_round
        winner = field[0]
        if winner is not None:
            wins[winner[0]] += 1

    return {name: wins[name] / trials for name, _ in real_entrants}


def _simulate_match(a: Entrant, b: Entrant, surface: str | None) -> Entrant:
    if a is None:
        return b
    if b is None:
        return a
    p_a = elo_service_tennis.get_match_win_prob(a[1], b[1], surface)
    if p_a is None:
        p_a = 0.5
    return a if random.random() < p_a else b

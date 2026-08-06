"""NASCAR Cup title model -- an ELIMINATION playoff, not a points race.

WHY THIS EXISTS SEPARATELY. racing_championship_sim projects a cumulative-points
title and is correct for F1 and IndyCar. NASCAR is excluded from it ON PURPOSE:
its championship is decided by a knockout playoff that ends in a winner-take-all
final race, so a points projection would not be merely unvalidated, it would be
answering a different question. Points decide who ENTERS the playoff; they do
not decide the title.

THE FORMAT ENCODED HERE (constants below, isolated so they are cheap to correct):
  * 16 drivers qualify. Race winners first, then points.
  * Four rounds of 3, 3, 3 and 1 races, cutting 16 -> 12 -> 8 -> 4.
  * The Championship 4 is a single race: whichever of the four finishes highest
    is champion, regardless of the other 39 cars.

HONESTLY LABELLED: unlike the WNBA bracket -- whose reseeding rule was RECOVERED
from the 2024/25 postseasons in our own data -- this structure is encoded from
domain knowledge, because nothing stored here records NASCAR playoff rounds. It
is stated as an assumption rather than presented as measured, and every number
that defines it sits in one block so a correction is a one-line change.

WHAT IS MEASURED: the per-race finishing model. Positions are drawn
Plackett-Luce from 10**(rating/400), the SAME weighting racing_sim already uses
for race_winner and top_n, so a driver's title odds cannot contradict the
per-race prices shown next to them.

model_validated: false. No NASCAR championship has been settled through this.
"""
import logging
import random

log = logging.getLogger("racing_playoff_sim")

# --- format constants (assumed, not measured -- see module docstring) --------
PLAYOFF_FIELD = 16
ROUND_RACES = (3, 3, 3, 1)          # races per round
ROUND_SURVIVORS = (12, 8, 4, 1)     # field size AFTER each round
CHAMPIONSHIP_ROUND_SIZE = 4
# ----------------------------------------------------------------------------


def _weights(ratings: dict[str, float]) -> dict[str, float]:
    """Same conversion racing_sim uses, so both price off one scale."""
    return {d: 10 ** (r / 400.0) for d, r in ratings.items()}


def _draw_order(field: list[str], w: dict[str, float], rng: random.Random) -> list[str]:
    """One Plackett-Luce finishing order over `field`, best first."""
    remaining = list(field)
    rem_w = {d: w[d] for d in field}
    order = []
    while remaining:
        total = sum(rem_w[d] for d in remaining)
        x = rng.random() * total
        acc = 0.0
        pick = remaining[-1]
        for d in remaining:
            acc += rem_w[d]
            if x <= acc:
                pick = d
                break
        order.append(pick)
        remaining.remove(pick)
        del rem_w[pick]
    return order


def simulate_nascar_title(
    ratings: dict[str, float],
    wins: dict[str, int],
    points: dict[str, float],
    regular_races_left: int,
    trials: int = 4000,
    seed: int | None = None,
) -> dict[str, float]:
    """{driver: P(wins the Cup)}.

    `regular_races_left` matters and is not cosmetic: while the regular season
    is still running the playoff FIELD is not set, so each trial simulates the
    remaining regular-season races first and lets their winners claim spots. A
    driver outside the current cut can win their way in, which a model that
    froze today's standings would price at zero.

    Qualification order per trial: race winners (existing + newly simulated),
    then championship points for the remaining spots -- the real rule's shape.
    Ties inside the points fill are broken by points then at random, so no
    driver benefits from sorting first.
    """
    field = [d for d in ratings if d in points]
    if len(field) < PLAYOFF_FIELD:
        return {}
    if seed is None:
        seed = hash(tuple(sorted(field))) & 0xFFFFFFFF
    rng = random.Random(seed)
    w = _weights(ratings)
    titles = {d: 0 for d in field}

    for _ in range(trials):
        sim_wins = {d: int(wins.get(d, 0)) for d in field}
        for _r in range(max(0, regular_races_left)):
            sim_wins[_draw_order(field, w, rng)[0]] += 1

        winners = [d for d in field if sim_wins[d] > 0]
        rng.shuffle(winners)  # no ordering advantage among equal win-getters
        winners.sort(key=lambda d: -sim_wins[d])
        qualified = winners[:PLAYOFF_FIELD]
        if len(qualified) < PLAYOFF_FIELD:
            rest = [d for d in field if d not in qualified]
            rest.sort(key=lambda d: (-points.get(d, 0.0), rng.random()))
            qualified += rest[: PLAYOFF_FIELD - len(qualified)]

        alive = qualified
        for races, survivors in zip(ROUND_RACES, ROUND_SURVIVORS):
            if survivors == 1:
                # Championship race: best finisher AMONG THE FOUR takes the
                # title. Everyone else on track is irrelevant to it, which is
                # exactly what makes this not a points contest.
                champ = next(d for d in _draw_order(field, w, rng) if d in alive)
                titles[champ] += 1
                break
            tally = {d: 0.0 for d in alive}
            for _r in range(races):
                order = [d for d in _draw_order(field, w, rng) if d in alive]
                for pos, d in enumerate(order):
                    tally[d] += len(order) - pos          # better finish = more
                if order:
                    tally[order[0]] += len(order)         # a win is decisive
            alive = sorted(alive, key=lambda d: (-tally[d], rng.random()))[:survivors]

    return {d: round(titles[d] / trials, 4) for d in field}

"""Season-championship Monte Carlo for F1 (cumulative-points title).

A championship is NOT the same question as a single race: it's who has the most
points after the remaining races, starting from the CURRENT standings. So a
driver 100 points back with strong pace is still a long shot -- exactly the
effect a single-race model misses (and why we don't price champion futures off
raw driver strength). This simulates the rest of the season:

  * start each simulated season from the real current championship points;
  * for each remaining race, sample a full finishing order from the drivers'
    strengths via a Plackett-Luce model (Gumbel-max trick -> argsort, fully
    vectorised so thousands of seasons run fast);
  * award F1 points (25-18-15-12-10-8-6-4-2-1 to the top 10) and accumulate;
  * after the last race, the driver with the most points is champion.

The share of simulated seasons a driver wins is their title probability.
Constructors' title = the same, summing each team's two drivers' points.

NASCAR is deliberately NOT modelled here -- its title is a playoff-elimination
format, not cumulative points, so this cumulative-points sim doesn't apply.
"""
import numpy as np

F1_POINTS = [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]  # P1..P10

# IndyCar pays far deeper than F1: P1..P25 below, and EVERY classified finisher
# past P25 still scores 5 (INDYCAR_TAIL_POINTS). That flat tail is why the table
# can't just be truncated like F1's -- with a 33-car field, ~8 drivers sit on the
# tail every race, and dropping it would understate the whole midfield's points.
# Bonus points (pole, laps led, most laps led -- 1/1/2) are NOT modelled: they're
# worth a couple of points against a 50-point win and we have no per-race
# laps-led model, so inventing them would add noise, not accuracy.
# The Indy 500's double points are likewise not special-cased -- it runs in May,
# so it is never among the REMAINING races this sim projects over.
INDYCAR_POINTS = [
    50, 40, 35, 32, 30, 28, 26, 24, 22, 20,     # P1..P10
    19, 18, 17, 16, 15, 14, 13, 12, 11, 10,     # P11..P20
    9, 8, 7, 6, 5,                              # P21..P25
]
INDYCAR_TAIL_POINTS = 5.0

SERIES_POINTS = {
    "f1": (F1_POINTS, 0.0),
    "irl": (INDYCAR_POINTS, INDYCAR_TAIL_POINTS),
}


def _position_points(d: int, points_table: list[float], tail: float) -> np.ndarray:
    """Points awarded for each finishing position in a d-car field: the table for
    as far as it goes, then `tail` for everyone behind it."""
    pos = np.full(d, float(tail))
    n = min(len(points_table), d)
    pos[:n] = points_table[:n]
    return pos


def simulate_driver_championship(
    driver_ids: list[str],
    current_points: dict[str, float],
    strengths: dict[str, float],
    remaining_races: int,
    trials: int = 4000,
    points_table: list[float] | None = None,
    tail_points: float = 0.0,
) -> dict[str, float]:
    """{driver_id: P(wins the drivers' championship)}. `strengths` are Elo-like
    ratings (higher = faster); converted to Plackett-Luce weights the same way
    the race sim / pole model do (10**(s/400))."""
    d = len(driver_ids)
    if d == 0 or remaining_races <= 0:
        # Season over (or no field): champion is simply whoever leads now.
        if not driver_ids:
            return {}
        leader = max(driver_ids, key=lambda x: current_points.get(x, 0.0))
        return {i: (1.0 if i == leader else 0.0) for i in driver_ids}

    logw = np.array([strengths[i] / 400.0 * np.log(10) for i in driver_ids])  # PL log-weights
    base = np.array([current_points.get(i, 0.0) for i in driver_ids])
    pos_points = _position_points(d, points_table or F1_POINTS, tail_points)

    champ_counts = np.zeros(d)
    done = 0
    chunk = 500  # bound memory: (chunk, races, drivers) float array
    rng = np.random.default_rng()
    while done < trials:
        n = min(chunk, trials - done)
        # Gumbel-max: argsort of (logw + Gumbel noise) descending == a PL order sample.
        noise = rng.gumbel(size=(n, remaining_races, d))
        order = np.argsort(-(logw[None, None, :] + noise), axis=2)  # (n, races, d) driver idx by finish pos
        # argsort of a permutation is its inverse: rank[...,i] = the position
        # driver i finished in. Indexing pos_points by that scores EVERY position
        # at once -- the old per-position loop only ran over the top 10, which is
        # right for F1 (nothing else scores) but would silently drop IndyCar's
        # P11-P25 table and its 5-point tail.
        rank = np.argsort(order, axis=2)
        race_points = pos_points[rank]
        season = base[None, :] + race_points.sum(axis=1)  # (n, d) final points per season
        champs = np.argmax(season, axis=1)
        for c in champs:
            champ_counts[c] += 1
        done += n
    return {driver_ids[i]: float(champ_counts[i] / trials) for i in range(d)}


def simulate_constructor_championship(
    constructors: dict[str, list[str]],  # constructor -> its driver_ids
    current_points: dict[str, float],
    strengths: dict[str, float],
    remaining_races: int,
    trials: int = 4000,
) -> dict[str, float]:
    """{constructor: P(wins the constructors' championship)} -- same season sim,
    but each simulated season sums both of a team's drivers' points."""
    driver_ids = [d for ds in constructors.values() for d in ds]
    d = len(driver_ids)
    teams = list(constructors.keys())
    if d == 0 or not teams:
        return {}
    if remaining_races <= 0:
        totals = {t: sum(current_points.get(x, 0.0) for x in constructors[t]) for t in teams}
        leader = max(teams, key=lambda t: totals[t])
        return {t: (1.0 if t == leader else 0.0) for t in teams}

    idx_by_driver = {x: i for i, x in enumerate(driver_ids)}
    # team membership matrix (teams x drivers) to sum driver points into team points
    membership = np.zeros((len(teams), d))
    for ti, t in enumerate(teams):
        for x in constructors[t]:
            membership[ti, idx_by_driver[x]] = 1.0

    logw = np.array([strengths.get(x, 0.0) / 400.0 * np.log(10) for x in driver_ids])
    base = np.array([current_points.get(x, 0.0) for x in driver_ids])
    pos_points = np.zeros(d)
    pos_points[: min(10, d)] = F1_POINTS[: min(10, d)]

    champ_counts = np.zeros(len(teams))
    done = 0
    chunk = 500
    rng = np.random.default_rng()
    while done < trials:
        n = min(chunk, trials - done)
        noise = rng.gumbel(size=(n, remaining_races, d))
        order = np.argsort(-(logw[None, None, :] + noise), axis=2)
        race_points = np.zeros((n, remaining_races, d))
        for p in range(min(10, d)):
            np.put_along_axis(race_points, order[:, :, p : p + 1], pos_points[p], axis=2)
        season = base[None, :] + race_points.sum(axis=1)  # (n, d)
        team_season = season @ membership.T  # (n, teams)
        champs = np.argmax(team_season, axis=1)
        for c in champs:
            champ_counts[c] += 1
        done += n
    return {teams[i]: float(champ_counts[i] / trials) for i in range(len(teams))}

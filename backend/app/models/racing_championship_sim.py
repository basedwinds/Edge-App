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


def simulate_driver_championship(
    driver_ids: list[str],
    current_points: dict[str, float],
    strengths: dict[str, float],
    remaining_races: int,
    trials: int = 4000,
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
    pos_points = np.zeros(d)
    pos_points[: min(10, d)] = F1_POINTS[: min(10, d)]

    champ_counts = np.zeros(d)
    done = 0
    chunk = 500  # bound memory: (chunk, races, drivers) float array
    rng = np.random.default_rng()
    while done < trials:
        n = min(chunk, trials - done)
        # Gumbel-max: argsort of (logw + Gumbel noise) descending == a PL order sample.
        noise = rng.gumbel(size=(n, remaining_races, d))
        order = np.argsort(-(logw[None, None, :] + noise), axis=2)  # (n, races, d) driver idx by finish pos
        race_points = np.zeros((n, remaining_races, d))
        for p in range(min(10, d)):  # only top-10 positions score
            np.put_along_axis(race_points, order[:, :, p : p + 1], pos_points[p], axis=2)
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

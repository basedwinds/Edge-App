"""Division/H2H/worst-to-first market signals, all derived from
season_sim.py's existing simulation output (win_count_pct histograms and
the Round-12 "_DIVISIONS" order/total-wins tallies) -- no new Monte Carlo
simulation needed for this batch, just new ways of reading the same
2,000-trial run already computed for every other futures market.

Kalshi division codes ("NFCWEST") <-> this app's DIVISIONS dict keys
("NFC West") are interconverted via a simple space-strip/upper-case
transform, confirmed reversible for all 8 real divisions.
"""
from app.data.divisions import DIVISIONS
from app.ingestion.market_matcher import to_kalshi_abbr


def division_code_to_key(division_code: str) -> str | None:
    """"NFCWEST" -> "NFC West" -- reverse of kalshi_client.py's own
    `key.replace(" ", "").upper()` transform, checked against all 8 real
    division keys rather than assumed reversible."""
    for key in DIVISIONS:
        if key.replace(" ", "").upper() == division_code.upper():
            return key
    return None


def division_wins_model_prob(division_code: str, line: float, sim_divisions: dict) -> float | None:
    """P(division combines for >= line total wins) -- direct tail-sum of the
    division's total_win_hist_pct, same "sum the histogram tail" pattern as
    win_total's per-team version."""
    key = division_code_to_key(division_code)
    if key is None or key not in sim_divisions:
        return None
    hist = sim_divisions[key]["total_win_hist_pct"]
    idx = int(line)
    if not (0 <= idx < len(hist)):
        return None
    return round(sum(hist[idx:]), 4)


def _match_order_blob(division_key: str, order_blob: str) -> tuple | None:
    """Kalshi's order_blob is a concatenation of the 4 teams' KALSHI codes
    in order, no separator (e.g. "ARISEALARSF"). Since the division's 4
    real teams are already known, this just tries all 24 permutations of
    them rather than parsing the blob blindly -- far more robust than a
    greedy prefix-split (this app's usual approach for 2-team blobs) would
    be for 4 teams, where ambiguous splits are much more likely."""
    import itertools

    teams = DIVISIONS.get(division_key)
    if not teams:
        return None
    for perm in itertools.permutations(teams):
        blob = "".join(to_kalshi_abbr(t) for t in perm)
        if blob == order_blob:
            return perm
    return None


def division_order_model_prob(division_code: str, order_blob: str, sim_divisions: dict) -> float | None:
    key = division_code_to_key(division_code)
    if key is None or key not in sim_divisions:
        return None
    matched_order = _match_order_blob(key, order_blob)
    if matched_order is None:
        return None
    return round(sim_divisions[key]["order_pct"].get(matched_order, 0.0), 4)


def division_extreme_model_probs(sim_divisions: dict, mode: str) -> dict[str, float]:
    """P(this division has the MOST or LEAST combined wins among all 8),
    for the div_least_wins/div_most_wins markets. Treats each division's
    total-win distribution as independent of the others for this
    cross-division comparison (a real simplification -- divisions ARE
    weakly correlated within the same simulated season via shared
    strength-of-schedule effects, but exact joint tracking across all 8
    divisions' Cartesian product would be a much bigger lift for a
    second-order effect) -- returns {division_key: probability}."""
    keys = list(sim_divisions.keys())
    hists = {k: sim_divisions[k]["total_win_hist_pct"] for k in keys}
    n_vals = len(next(iter(hists.values())))

    # CDF (P(X <= v)) per division, used to compute "all others are below/above v"
    cdf = {k: [] for k in keys}
    for k in keys:
        running = 0.0
        for p in hists[k]:
            running += p
            cdf[k].append(running)

    result: dict[str, float] = {}
    for target in keys:
        prob = 0.0
        for v, p_v in enumerate(hists[target]):
            if p_v == 0.0:
                continue
            if mode == "most":
                # all OTHER divisions strictly below v (using CDF at v-1), ties split evenly is a further
                # refinement not worth the complexity here -- strict "below" is a conservative, documented choice.
                others_below = 1.0
                for other in keys:
                    if other == target:
                        continue
                    others_below *= cdf[other][v - 1] if v > 0 else 0.0
                prob += p_v * others_below
            else:  # "least"
                others_above = 1.0
                for other in keys:
                    if other == target:
                        continue
                    others_above *= 1.0 - cdf[other][v]
                prob += p_v * others_above
        result[target] = round(prob, 4)
    return result


def worst_to_first_model_prob(last_season_worst_by_division: dict[str, str], sim_results: dict) -> float | None:
    """P(ANY of last season's division-worst teams wins their division this
    year) -- 1 - product(1 - division_pct) across those teams, treating
    each division race as independent of the others (reasonable here since
    they're literally different divisions, unlike division_extreme_model_probs's
    same-trial-correlation caveat above)."""
    if not last_season_worst_by_division:
        return None
    prob_none = 1.0
    for team in last_season_worst_by_division.values():
        team_sim = sim_results.get(team)
        if team_sim is None:
            continue
        prob_none *= 1.0 - team_sim.get("division_pct", 0.0)
    return round(1.0 - prob_none, 4)


def h2h_model_prob(team: str, opponent: str, sim_results: dict) -> float | None:
    """P(team's actual wins > opponent's actual wins), ties split 50/50 --
    direct convolution of the two teams' independent win_count_pct
    histograms (a real simplification when the two teams play each other,
    since that game's outcome correlates their win counts slightly -- not
    worth tracking the joint distribution for a single shared game's effect)."""
    team_sim = sim_results.get(team)
    opp_sim = sim_results.get(opponent)
    if team_sim is None or opp_sim is None:
        return None
    team_hist = team_sim.get("win_count_pct")
    opp_hist = opp_sim.get("win_count_pct")
    if not team_hist or not opp_hist:
        return None
    prob = 0.0
    for a, p_a in enumerate(team_hist):
        for b, p_b in enumerate(opp_hist):
            if a > b:
                prob += p_a * p_b
            elif a == b:
                prob += 0.5 * p_a * p_b
    return round(prob, 4)

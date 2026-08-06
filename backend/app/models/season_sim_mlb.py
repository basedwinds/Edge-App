"""MLB season Monte Carlo simulator -- parallel to season_sim.py (NFL)/
season_sim_nba.py, same "free reference estimate, not validated to beat the
market" honesty. Elo ratings held STATIC across all remaining games within a
single trial, same simplification as NFL/NBA's own simulators. Deliberately
TEAM-ELO ONLY, no starting-pitcher blend, for remaining games -- future
starters aren't knowable months out the way this app's day-of probable-
pitcher data is, so there's nothing real to blend in for games beyond the
next ~5 days; using team-Elo-only for the whole season is an honest, real
simplification, not a guess dressed up as more precise than it is.

Real 2022+ postseason format, confirmed live via web search 2026-07-17 (not
assumed from memory, given how much this shapes the whole simulator --
MLB.com/Bleacher Report/NBC, cross-checked against each other):
  - 6 teams per league (AL/NL, 15 teams each): 3 division winners (seeded
    1-3 by record) + 3 wild cards (best remaining records in the league
    regardless of division, seeded 4-6).
  - Wild Card Series: best-of-3, ALL games at the HIGHER seed's park (not
    split) -- (3) hosts (6), (4) hosts (5). Seeds 1-2 get a bye.
  - Division Series: best-of-5, games 1/2/5 at the higher seed, games 3/4 at
    the lower seed (2-2-1). NO RESEEDING -- (1) plays the winner of (4v5),
    (2) plays the winner of (3v6), a FIXED bracket regardless of which wild
    card team actually survives.
  - Championship Series / World Series: best-of-7, 2-3-2 (games 1/2/6/7 at
    the higher seed). World Series home field goes to whichever pennant
    winner has the better real regular-season win total (the 2017+ rule --
    no more fixed AL/NL rotation), determined here by directly comparing
    each trial's own simulated win counts, not a separate abstract "seed."
"""
import random

from app.data.mlb_divisions import DIVISIONS, TEAM_LEAGUE
from app.models.baseline.elo_mlb import HOME_FIELD_ADV, NEUTRAL_SITE_HOME_FIELD_ADV, win_prob

N_TRIALS = 2000
MAX_REG_WINS = 162  # real full MLB regular season length

WC_PATTERN = "HHH"          # best-of-3, all at higher seed
LDS_PATTERN = "HHAAH"       # best-of-5, 2-2-1
LCS_WS_PATTERN = "HHAAAHH"  # best-of-7, 2-3-2


def _simulate_game(ratings: dict, home: str, away: str, home_adv: float, rng: random.Random) -> str:
    p_home = win_prob(ratings.get(home, 1500.0), ratings.get(away, 1500.0), home_adv)
    return home if rng.random() < p_home else away


def _simulate_series(ratings: dict, team_a: str, team_b: str, wins: dict, pattern: str, rng: random.Random) -> str:
    """`higher`/`lower` determined by each trial's own real simulated win
    total (ties broken arbitrarily toward team_a, a rare edge case) -- this
    is what actually decides home-field priority for every real MLB
    postseason round, not an abstract seed number carried separately."""
    higher, lower = (team_a, team_b) if wins.get(team_a, 0) >= wins.get(team_b, 0) else (team_b, team_a)
    series_wins = {higher: 0, lower: 0}
    wins_needed = len(pattern) // 2 + 1
    for game_side in pattern:
        home, away = (higher, lower) if game_side == "H" else (lower, higher)
        winner = _simulate_game(ratings, home, away, HOME_FIELD_ADV, rng)
        series_wins[winner] += 1
        if series_wins[higher] == wins_needed or series_wins[lower] == wins_needed:
            break
    return higher if series_wins[higher] == wins_needed else lower


def _rank_teams(teams: list[str], wins: dict, head_to_head: dict, rng: random.Random) -> list[str]:
    """Best-to-worst. Simplified tiebreak (wins, then head-to-head if
    exactly 2 tied and they played in this trial, then random for 3+-way
    ties) -- same "rough, auditable" simplification as season_sim_nba.py,
    not MLB's real multi-step tiebreaker (head-to-head, intradivision
    record, ...)."""
    ordered = sorted(teams, key=lambda t: wins[t], reverse=True)
    result: list[str] = []
    i = 0
    while i < len(ordered):
        j = i
        while j < len(ordered) and wins[ordered[j]] == wins[ordered[i]]:
            j += 1
        tier = ordered[i:j]
        if len(tier) == 2 and (tier[0], tier[1]) in head_to_head:
            winner = head_to_head[(tier[0], tier[1])]
            tier = [winner, tier[0] if winner == tier[1] else tier[1]]
        elif len(tier) > 1:
            tier = list(tier)
            rng.shuffle(tier)
        result.extend(tier)
        i = j
    return result


def _compute_starting_records(played_games: list[dict]) -> dict[str, int]:
    wins: dict[str, int] = {}
    for g in played_games:
        if g.get("home_score") is None or g.get("away_score") is None or g["home_score"] == g["away_score"]:
            continue  # no real MLB regular-season ties to grade
        winner = g["home_team"] if g["home_score"] > g["away_score"] else g["away_team"]
        wins[winner] = wins.get(winner, 0) + 1
    return wins


def run_simulation(
    ratings: dict[str, float],
    all_reg_games: list[dict],
    n_trials: int = N_TRIALS,
    seed: int | None = None,
) -> dict[str, dict]:
    """all_reg_games: every REG-season game for the season being simulated,
    played or not. Returns {team: {"division_pct", "playoff_pct" (made the
    real 6-per-league bracket), "pennant_pct" (won the league), "championship_pct",
    "best_record_pct", "worst_record_pct", "win_count_pct"}}, plus a
    "_LEAGUE" entry ({"any_wins_ge_pct"}) mirroring NFL/NBA's win-total
    ladder markets."""
    rng = random.Random(seed)
    all_teams = list(TEAM_LEAGUE.keys())
    starting_wins = _compute_starting_records(all_reg_games)
    remaining = [g for g in all_reg_games if g.get("home_score") is None or g.get("away_score") is None]

    tallies = {
        t: {
            "division": 0, "playoff": 0, "pennant": 0, "championship": 0,
            "best_record": 0, "worst_record": 0, "win_hist": [0] * (MAX_REG_WINS + 1),
        }
        for t in all_teams
    }
    any_wins_ge_hist = [0] * (MAX_REG_WINS + 1)
    # JOINT (AL pennant, NL pennant) counts, for the World Series MATCHUP
    # markets (KXTEAMSINWS, 225 open). Multiplying the two pennant_pct values
    # would be wrong twice over: the pairing is not independent (both champions
    # come out of one simulated postseason) and pennant_pct alone cannot say
    # WHICH opponent. Tallied here from the same trial that already decides
    # both champions, so a matchup probability can never contradict either
    # team's own pennant number.
    matchup_tallies: dict[tuple[str, str], int] = {}

    for _ in range(n_trials):
        wins = {t: starting_wins.get(t, 0) for t in all_teams}
        head_to_head: dict[tuple, str] = {}

        for g in remaining:
            home, away = g["home_team"], g["away_team"]
            if home not in wins or away not in wins:
                continue
            adv = NEUTRAL_SITE_HOME_FIELD_ADV if g.get("location") == "Neutral" else HOME_FIELD_ADV
            winner = _simulate_game(ratings, home, away, adv, rng)
            loser = away if winner == home else home
            wins[winner] += 1
            head_to_head[(winner, loser)] = winner
            head_to_head[(loser, winner)] = winner

        max_wins = max(wins.values())
        min_wins = min(wins.values())
        for t in all_teams:
            if wins[t] == max_wins:
                tallies[t]["best_record"] += 1
            if wins[t] == min_wins:
                tallies[t]["worst_record"] += 1
            tallies[t]["win_hist"][min(wins[t], MAX_REG_WINS)] += 1
        for i in range(min(max_wins, MAX_REG_WINS) + 1):
            any_wins_ge_hist[i] += 1

        for div, teams in DIVISIONS.items():
            winner = _rank_teams(teams, wins, head_to_head, rng)[0]
            tallies[winner]["division"] += 1

        league_champs: dict[str, str] = {}
        for league in ("AL", "NL"):
            league_teams = [t for t in all_teams if TEAM_LEAGUE[t] == league]
            league_divisions = {d: teams for d, teams in DIVISIONS.items() if d.startswith(league)}

            div_winners = []
            remaining_teams = list(league_teams)
            for teams in league_divisions.values():
                w = _rank_teams(teams, wins, head_to_head, rng)[0]
                div_winners.append(w)
                remaining_teams.remove(w)

            seed1, seed2, seed3 = _rank_teams(div_winners, wins, head_to_head, rng)
            seed4, seed5, seed6 = _rank_teams(remaining_teams, wins, head_to_head, rng)[:3]

            for t in (seed1, seed2, seed3, seed4, seed5, seed6):
                tallies[t]["playoff"] += 1

            wc_45_winner = _simulate_series(ratings, seed4, seed5, wins, WC_PATTERN, rng)
            wc_36_winner = _simulate_series(ratings, seed3, seed6, wins, WC_PATTERN, rng)

            lds1_winner = _simulate_series(ratings, seed1, wc_45_winner, wins, LDS_PATTERN, rng)
            lds2_winner = _simulate_series(ratings, seed2, wc_36_winner, wins, LDS_PATTERN, rng)

            league_champ = _simulate_series(ratings, lds1_winner, lds2_winner, wins, LCS_WS_PATTERN, rng)
            league_champs[league] = league_champ
            tallies[league_champ]["pennant"] += 1

        pair = (league_champs["AL"], league_champs["NL"])
        matchup_tallies[pair] = matchup_tallies.get(pair, 0) + 1

        champion = _simulate_series(ratings, league_champs["AL"], league_champs["NL"], wins, LCS_WS_PATTERN, rng)
        tallies[champion]["championship"] += 1

    results = {
        t: {
            "division_pct": tallies[t]["division"] / n_trials,
            "playoff_pct": tallies[t]["playoff"] / n_trials,
            "pennant_pct": tallies[t]["pennant"] / n_trials,
            "championship_pct": tallies[t]["championship"] / n_trials,
            "best_record_pct": tallies[t]["best_record"] / n_trials,
            "worst_record_pct": tallies[t]["worst_record"] / n_trials,
            "win_count_pct": [c / n_trials for c in tallies[t]["win_hist"]],
        }
        for t in all_teams
    }
    results["_LEAGUE"] = {"any_wins_ge_pct": [c / n_trials for c in any_wins_ge_hist]}
    # Keyed by an UNORDERED frozenset, not the (AL, NL) tuple: Kalshi names a
    # matchup "Toronto vs Washington" with no indication which side is which
    # league, so a caller must be able to look it up either way round. This is
    # the same ordered-vs-unordered trap that swapped two LoL rosters -- there
    # the ORDERED value was stored under an UNORDERED key, which is the unsafe
    # direction. Here the value (a count) is symmetric, so folding the key is
    # lossless.
    results["_MATCHUPS"] = {
        frozenset(pair): c / n_trials for pair, c in matchup_tallies.items()
    }
    return results

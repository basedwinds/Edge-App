"""NBA season Monte Carlo simulator -- parallel to season_sim.py (NFL), same
"free reference estimate, not validated to beat the market" honesty.

REAL BLOCKER, not a design choice: ESPN has not published the 2026-27
schedule yet (confirmed live 2026-07-16 -- 0 events for the typical late-
October season-opener week; NBA schedules are usually released in mid-
August). This module is built and verified now against a COMPLETED real
season's actual schedule/results as a stand-in, and will activate
automatically the moment the real 2026-27 schedule appears (poller_nba.py
already pulls whatever ESPN has for the current season window on every
cycle) -- same "build the pipe, it lights up when data exists" pattern as
NBA regular-season moneyline/spread/total in this app (built in Phase 2/4
despite 0 open markets at the time).

NBA's playoff format is structurally quite different from the NFL's --
verified live via web search 2026-07-16, not assumed from memory, given how
much this shapes the whole simulator:
  - Top 6 seeds/conference qualify directly, by win% (NOT tied to division
    winners -- that guarantee was dropped in the 2015-16 season; division
    winner is scored here purely as "best record within division," a
    separate, independent futures market from playoff seeding).
  - Seeds 7-10 go to a play-in tournament: 7v8 game (winner = 7-seed), 9v10
    game (loser eliminated), loser of 7v8 vs. winner of 9v10 for the 8-seed.
  - The bracket is FIXED once seeded -- no reseeding in later rounds (unlike
    the NFL's divisional-round reseed-by-actual-seed rule).
  - Every round (first round through Finals) is a best-of-7 SERIES, not a
    single game -- simulated game-by-game in the real 2-2-1-1-1 home/away
    pattern (higher seed hosts games 1,2,5,7), stopping at 4 wins.

Elo ratings held STATIC across all remaining games within a single trial,
same simplification as the NFL version -- but each trial now draws its own
per-team strength offset (see TEAM_STRENGTH_SIGMA), so a team is the same team
all season while the SEASON-TO-SEASON uncertainty in its rating is modelled.
"""
import random

from app.data.nba_divisions import CONFERENCES, DIVISIONS, TEAM_CONFERENCE, TEAM_DIVISION
from app.models.baseline.elo_nba import effective_home_court_adv, win_prob

N_TRIALS = 2000
MAX_REG_WINS = 82  # NBA regular season length; win-count histograms use indices 0..82

# Per-season, per-team Elo uncertainty, in Elo points. Same fix as CFB
# (sigma=225) and NFL (sigma=100): treating each rating as EXACTLY known and
# every game as an independent coin makes an 82-game season ~binomial, which is
# far too narrow. Real pre-season uncertainty is about the TEAM (trades, a
# rookie leap, health) and PERSISTS across all 82 games, fattening the tails in
# a way independent flips cannot.
#
# 75 is measured, not assumed. Backtest over 10 seasons (2016-2026), each
# projected from PRIOR-SEASONS-ONLY Elo with zero games played -- exactly what
# a pre-season futures market prices -- 2,700 team-threshold predictions:
#
#   sigma=0    predicted 99.5% -> happened 92.4%; 88.7% -> 72.8%; 11.4% -> 28.2%
#              mean abs gap 8.70pp, Brier 0.1245
#   sigma=75   every bucket within 2.0pp
#              mean abs gap 1.30pp, Brier 0.1134
#
# Leave-one-season-out picked 75 in 9 of 10 folds and improved the held-out
# season in 9 of 10 (only 2017 got worse). AUC RISES 0.9181 -> 0.9201, so this
# widens the distribution (win-total sd 4.26 -> 8.57 wins) without flattening
# the ranking. Evidence is stronger than NFL's and comparable to CFB's.
TEAM_STRENGTH_SIGMA = 75.0

# Which games (1-indexed) the HIGHER seed hosts in a best-of-7 series --
# real NBA format since 2014 (previously 2-3-2), confirmed via the same
# search as the playoff-format facts above.
_HIGHER_SEED_HOME_GAMES = {1, 2, 5, 7}


def _simulate_game(ratings: dict, home: str, away: str, home_adv: float, rng: random.Random) -> str:
    p_home = win_prob(ratings.get(home, 1500.0), ratings.get(away, 1500.0), home_adv)
    return home if rng.random() < p_home else away


def _simulate_series(ratings: dict, higher_seed: str, lower_seed: str, rng: random.Random) -> str:
    """Best-of-7, real 2-2-1-1-1 home/away pattern. Elo ratings/home-court
    adv held static across the series (same per-trial-static simplification
    as the rest of this module)."""
    wins = {higher_seed: 0, lower_seed: 0}
    game_num = 0
    while wins[higher_seed] < 4 and wins[lower_seed] < 4:
        game_num += 1
        if game_num in _HIGHER_SEED_HOME_GAMES:
            home, away = higher_seed, lower_seed
        else:
            home, away = lower_seed, higher_seed
        home_adv = effective_home_court_adv(home, None, None, None)
        winner = _simulate_game(ratings, home, away, home_adv, rng)
        wins[winner] += 1
    return higher_seed if wins[higher_seed] == 4 else lower_seed


def _rank_teams(teams: list[str], wins: dict, head_to_head: dict, rng: random.Random) -> list[str]:
    """Best-to-worst. Simplified tiebreak (wins, then head-to-head if
    exactly 2 tied and they played in this trial, then random for 3+-way
    ties) -- same "rough, auditable" simplification as season_sim.py (NFL),
    not the NBA's real multi-step tiebreaker (head-to-head, division record,
    conference record, ...)."""
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
        if g.get("home_score") is None or g.get("away_score") is None:
            continue
        winner = g["home_team"] if g["home_score"] > g["away_score"] else g["away_team"]  # no ties in the NBA
        wins[winner] = wins.get(winner, 0) + 1
    return wins


def _compute_total_games(all_reg_games: list[dict]) -> dict[str, int]:
    total: dict[str, int] = {}
    for g in all_reg_games:
        total[g["home_team"]] = total.get(g["home_team"], 0) + 1
        total[g["away_team"]] = total.get(g["away_team"], 0) + 1
    return total


def run_simulation(
    ratings: dict[str, float],
    all_reg_games: list[dict],
    n_trials: int = N_TRIALS,
    seed: int | None = None,
) -> dict[str, dict]:
    """all_reg_games: every REG-season game for the season being simulated,
    played or not. Returns {team: {"division_pct", "playoff_pct" (made the
    real 16-team bracket after play-in resolves), "play_in_pct" (finished
    7th-10th, reached the play-in stage), "conf_champ_pct", "championship_pct",
    "best_record_pct", "worst_record_pct", "win_count_pct"}}, plus a
    "_LEAGUE" entry ({"any_wins_ge_pct"}) mirroring NFL's win-total ladder
    markets."""
    rng = random.Random(seed)
    all_teams = list(TEAM_CONFERENCE.keys())
    starting_wins = _compute_starting_records(all_reg_games)
    remaining = [g for g in all_reg_games if g.get("home_score") is None or g.get("away_score") is None]
    total_games = _compute_total_games(all_reg_games)

    tallies = {
        t: {
            "division": 0, "playoff": 0, "play_in": 0, "conf_champ": 0, "championship": 0,
            "best_record": 0, "worst_record": 0, "win_hist": [0] * (MAX_REG_WINS + 1),
        }
        for t in all_teams
    }
    any_wins_ge_hist = [0] * (MAX_REG_WINS + 1)

    for _ in range(n_trials):
        wins = {t: starting_wins.get(t, 0) for t in all_teams}
        head_to_head: dict[tuple, str] = {}

        # ONE team-strength draw per simulated season, applied by building the
        # trial's rating table up front rather than threading an `offsets`
        # argument through all 11 _simulate_game/_simulate_series call sites.
        # That is deliberate: the NFL version of this fix threaded a parameter
        # and silently missed the four playoff calls, which would have given a
        # team one strength in the regular season and another in the playoffs.
        # Substituting the table makes that class of miss impossible -- every
        # downstream call reads the same per-trial ratings by construction.
        if TEAM_STRENGTH_SIGMA > 0:
            ratings_t = {
                t: ratings.get(t, 1500.0) + rng.gauss(0.0, TEAM_STRENGTH_SIGMA)
                for t in all_teams
            }
        else:
            ratings_t = ratings

        for g in remaining:
            home, away = g["home_team"], g["away_team"]
            if home not in wins or away not in wins:
                continue
            adv = effective_home_court_adv(home, g.get("location"), g.get("home_rest"), g.get("away_rest"))
            winner = _simulate_game(ratings_t, home, away, adv, rng)
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

        conf_champs: dict[str, str] = {}
        for conf, conf_teams in CONFERENCES.items():
            seeds = _rank_teams(conf_teams, wins, head_to_head, rng)
            top6, seven, eight, nine, ten = seeds[:6], seeds[6], seeds[7], seeds[8], seeds[9]
            # Only the 4 teams that finish 7th-10th actually PLAY IN the
            # play-in tournament -- top-6 teams go straight to the playoffs
            # and never touch it (they get "playoff", tallied below, instead).
            for t in (seven, eight, nine, ten):
                tallies[t]["play_in"] += 1

            # Play-in: 7v8 (winner -> 7-seed bracket slot), 9v10 (loser eliminated)
            seven_v_eight_winner = _simulate_game(ratings_t, seven, eight, effective_home_court_adv(seven, None, None, None), rng)
            seven_v_eight_loser = eight if seven_v_eight_winner == seven else seven
            nine_v_ten_winner = _simulate_game(ratings_t, nine, ten, effective_home_court_adv(nine, None, None, None), rng)
            eighth_seed_winner = _simulate_game(
                ratings_t, seven_v_eight_loser, nine_v_ten_winner,
                effective_home_court_adv(seven_v_eight_loser, None, None, None), rng,
            )

            bracket = top6 + [seven_v_eight_winner, eighth_seed_winner]  # 8 real playoff teams, seeds 1-8 in order
            for t in bracket:
                tallies[t]["playoff"] += 1

            # First round: 1v8, 2v7, 3v6, 4v5 -- FIXED bracket, no reseeding later
            r1_winners = []
            for hi_idx, lo_idx in ((0, 7), (3, 4), (2, 5), (1, 6)):
                w = _simulate_series(ratings_t, bracket[hi_idx], bracket[lo_idx], rng)
                r1_winners.append(w)
            # Conf semis: winner(1v8) vs winner(4v5); winner(2v7) vs winner(3v6)
            semi1 = _simulate_series(ratings_t, r1_winners[0], r1_winners[1], rng)
            semi2 = _simulate_series(ratings_t, r1_winners[2], r1_winners[3], rng)
            conf_champ = _simulate_series(ratings_t, semi1, semi2, rng)
            conf_champs[conf] = conf_champ
            tallies[conf_champ]["conf_champ"] += 1

        conf_names = list(CONFERENCES.keys())
        champion = _simulate_series(ratings_t, conf_champs[conf_names[0]], conf_champs[conf_names[1]], rng)
        tallies[champion]["championship"] += 1

    results = {
        t: {
            "division_pct": tallies[t]["division"] / n_trials,
            "playoff_pct": tallies[t]["playoff"] / n_trials,
            "play_in_pct": tallies[t]["play_in"] / n_trials,
            "conf_champ_pct": tallies[t]["conf_champ"] / n_trials,
            "championship_pct": tallies[t]["championship"] / n_trials,
            "best_record_pct": tallies[t]["best_record"] / n_trials,
            "worst_record_pct": tallies[t]["worst_record"] / n_trials,
            "win_count_pct": [c / n_trials for c in tallies[t]["win_hist"]],
        }
        for t in all_teams
    }
    results["_LEAGUE"] = {"any_wins_ge_pct": [c / n_trials for c in any_wins_ge_hist]}
    return results

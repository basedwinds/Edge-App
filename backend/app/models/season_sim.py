"""Season Monte Carlo simulator, used to price the futures markets Kalshi/
Polymarket list (division winners, conference champions, Super Bowl
champion, 1-seed) against the app's existing Elo ratings -- the same
"free reference estimate, not validated to beat the market" honesty as
everything else in this app (see elo_service.py).

Deliberately simplified vs. real NFL tiebreaker rules (division record,
common games, strength of victory, strength of schedule, net points, ...):
uses simulated win total as the primary sort, head-to-head result within
the SAME simulated trial as a secondary tiebreak (only when exactly two
teams are tied and they played each other in that trial), and a random
coin flip as the final tiebreak for 3+-way ties. Same "rough, auditable,
not exactly official rules" spirit as this project's other simplifications
(e.g. the non-starter injury discount, or the playoff-clincher week gating).

Elo ratings are held STATIC across all remaining games within a single
trial (not updated game-by-game as that trial's season unfolds) --
computationally simpler; each trial independently resamples from the same
rating snapshot, so the aggregate distribution across many trials still
captures matchup uncertainty, just not the "team gets better/worse as the
hypothetical season unfolds" second-order effect. Worth revisiting if this
ever needs Phase 7-level rigor.

Playoff format (7 teams/conference, matches the current real NFL format):
4 division winners seeded 1-4 by record, 3 wildcards seeded 5-7. Wild card
round: 2v7, 3v6, 4v5 (1-seed byes), fixed matchups regardless of upsets.
Divisional round RESEEDS: the 1-seed hosts whichever surviving team has the
worst actual seed, the other two survivors play each other, host is always
whichever survivor has the better seed. Conference championship: same
reseeding-by-actual-seed logic. Super Bowl is a fixed neutral site --
home_field_adv=0 for that game specifically, unlike every other simulated
game (real, well-known fact, not a simplification).
"""
import random

from app.data.divisions import DIVISIONS, TEAM_CONFERENCE
from app.models.baseline.elo import effective_home_field_adv, win_prob

N_TRIALS = 2000
MAX_REG_WINS = 17  # current NFL regular season length; win-count histograms below use indices 0..17


# Pre-season uncertainty in a team's TRUE strength, in Elo points, drawn once per
# simulated season and held across that season's games.
#
# Without it every game is an independent coin off a rating treated as exactly
# known, so a 17-game season is close to a binomial -- far too narrow. Real
# pre-season uncertainty is about the TEAM (free agency, the draft, a new QB) and
# it persists all year, which fattens the tails in a way independent flips cannot.
#
# Measured the same way as the CFB version (see season_sim_cfb), projecting each
# season from prior-years-only Elo with zero games played -- exactly what these
# futures price. 1,760 team-threshold predictions over 2021-2025:
#
#   sigma=0    predicted 88.5% -> happened 76.3%   predicted 11.4% -> happened 18.9%
#   sigma=100  mean absolute calibration gap 6.94pp -> ~1.5pp
#
# Brier 0.1507 -> 0.1444.
#
# EVIDENCE IS WEAKER THAN CFB'S AND THE NUMBER REFLECTS THAT. Leave-one-season-out
# improved only 3 of 5 folds (2021 and 2023 got worse) and the fitted sigma ranged
# 100-150, where CFB improved all 4 folds at a tight 225-250. So this takes the
# LOW end of that range -- 100 was the modal fold pick -- rather than the 125 that
# minimises full-sample gap. The DIRECTION is not in doubt: the sim demonstrably
# does not represent pre-season roster turnover, and unmodelled uncertainty can
# only ever produce overconfidence. The magnitude is the uncertain part, so it is
# deliberately under-applied.
TEAM_STRENGTH_SIGMA = 100.0


def _simulate_game(ratings: dict, home: str, away: str, home_field_adv: float, rng: random.Random,
                   offsets: dict | None = None) -> str:
    ra = ratings.get(home, 1500.0)
    rb = ratings.get(away, 1500.0)
    if offsets is not None:
        ra += offsets.get(home, 0.0)
        rb += offsets.get(away, 0.0)
    p_home = win_prob(ra, rb, home_field_adv)
    return home if rng.random() < p_home else away


def _rank_teams(teams: list[str], wins: dict, head_to_head: dict, rng: random.Random) -> list[str]:
    """Best-to-worst. See module docstring for the tiebreak simplification."""
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
        if g["home_score"] == g["away_score"]:
            continue  # ties don't count toward either team's win total here -- a known simplification
        winner = g["home_team"] if g["home_score"] > g["away_score"] else g["away_team"]
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
    played or not (played ones seed the starting win totals, unplayed ones
    get simulated). Returns {team: {"division_pct", "playoff_pct",
    "one_seed_pct", "conf_champ_pct", "sb_champ_pct", "undefeated_pct",
    "best_record_pct", "win_count_pct"}}, plus a special "_LEAGUE" entry with
    {"any_undefeated_pct", "any_wins_ge_pct"}. "win_count_pct" is a length-18
    list (index i = P(exactly i wins), i in 0..17) -- the raw win-count
    distribution the new win-total markets derive from (Kalshi's
    KXNFLWINS-{team}/KXNFLEXACTWINS{team} series, added 2026-07-16).
    "any_wins_ge_pct" is the league-wide analogue (index i = P(at least one
    team finishes with >= i wins)), for Kalshi's team-less KXNFLWINS-ANY
    market. "any_undefeated_pct" (Polymarket's single league-wide undefeated
    market, not team-keyed -- see polymarket_client.py's
    pro-football-undefeated-regular-season) predates this histogram and is
    kept as its own field since that market doesn't take a threshold param.

    Also added 2026-07-16: "worst_record_pct" (mirror of best_record_pct)
    and a "_DIVISIONS" entry, {div: {"order_pct": {(t1,t2,t3,t4): pct},
    "total_win_hist_pct": length-69 list, index i = P(the division's 4 teams
    combine for exactly i wins)}} -- for the division-exact-order,
    division-total-wins, and division-most/least-wins markets.
    """
    rng = random.Random(seed)
    all_teams = list(TEAM_CONFERENCE.keys())
    starting_wins = _compute_starting_records(all_reg_games)
    remaining = [g for g in all_reg_games if g.get("home_score") is None or g.get("away_score") is None]
    total_games = _compute_total_games(all_reg_games)

    tallies = {
        t: {
            "division": 0, "playoff": 0, "one_seed": 0, "conf_champ": 0, "sb_champ": 0,
            "undefeated": 0, "best_record": 0, "worst_record": 0, "win_hist": [0] * (MAX_REG_WINS + 1),
            # Stage of elimination (Kalshi KXNFLSTAGEOFELIM): the ONE round each
            # team bows out in, per trial. Exhaustive + mutually exclusive, so
            # the six sum to n_trials for every team. reg = missed the playoffs.
            "stage_reg": 0, "stage_wc": 0, "stage_div": 0, "stage_conf": 0,
            "stage_sb_loss": 0, "stage_sb_win": 0,
        }
        for t in all_teams
    }
    any_undefeated_count = 0
    any_wins_ge_hist = [0] * (MAX_REG_WINS + 1)  # index i = trials where SOME team finished with >= i wins
    # Division exact order (24 permutations of 4 teams) and division-total-wins
    # (summed across the 4 teams IN THE SAME TRIAL, not a post-hoc convolution
    # of independent marginals -- correctly captures the real negative
    # correlation from common divisional games, e.g. if A beats B, A's total
    # goes up and B's doesn't, for free, since it's read off the same `wins`
    # dict every other division stat already uses).
    MAX_DIVISION_WINS = 4 * MAX_REG_WINS
    div_tallies = {div: {"order": {}, "total_win_hist": [0] * (MAX_DIVISION_WINS + 1)} for div in DIVISIONS}

    for _ in range(n_trials):
        wins = {t: starting_wins.get(t, 0) for t in all_teams}
        head_to_head: dict[tuple, str] = {}
        # ONE strength draw per simulated season, shared by the regular season and
        # the playoffs below -- a team is the same team all year, so redrawing per
        # game would just be extra coin-flip noise and would leave the win-total
        # distribution as narrow as before (see TEAM_STRENGTH_SIGMA).
        offsets = ({t: rng.gauss(0.0, TEAM_STRENGTH_SIGMA) for t in all_teams}
                   if TEAM_STRENGTH_SIGMA > 0 else None)

        for g in remaining:
            home, away = g["home_team"], g["away_team"]
            if home not in wins or away not in wins:
                continue
            hfa = effective_home_field_adv(home, g.get("location"))
            winner = _simulate_game(ratings, home, away, hfa, rng, offsets)
            loser = away if winner == home else home
            wins[winner] += 1
            head_to_head[(winner, loser)] = winner
            head_to_head[(loser, winner)] = winner

        # Undefeated / best-record are pure regular-season win-total facts,
        # independent of the playoff bracket simulated below.
        trial_has_undefeated = False
        max_wins = max(wins.values())
        min_wins = min(wins.values())
        for t in all_teams:
            if wins[t] == max_wins:
                tallies[t]["best_record"] += 1
            if wins[t] == min_wins:
                tallies[t]["worst_record"] += 1
            if total_games.get(t) and wins[t] == total_games[t]:
                tallies[t]["undefeated"] += 1
                trial_has_undefeated = True
            tallies[t]["win_hist"][min(wins[t], MAX_REG_WINS)] += 1
        if trial_has_undefeated:
            any_undefeated_count += 1
        for i in range(min(max_wins, MAX_REG_WINS) + 1):
            any_wins_ge_hist[i] += 1

        division_winners: dict[str, str] = {}
        for div, teams in DIVISIONS.items():
            order = _rank_teams(teams, wins, head_to_head, rng)
            winner = order[0]
            division_winners[div] = winner
            tallies[winner]["division"] += 1
            order_tuple = tuple(order)
            div_tallies[div]["order"][order_tuple] = div_tallies[div]["order"].get(order_tuple, 0) + 1
            div_tallies[div]["total_win_hist"][sum(wins[t] for t in teams)] += 1

        conf_seeds: dict[str, list[str]] = {}
        for conf in ("AFC", "NFC"):
            conf_div_winners = [division_winners[d] for d in DIVISIONS if d.startswith(conf)]
            seeded_div_winners = _rank_teams(conf_div_winners, wins, head_to_head, rng)
            conf_teams = [t for t in all_teams if TEAM_CONFERENCE[t] == conf]
            wildcards = _rank_teams([t for t in conf_teams if t not in conf_div_winners], wins, head_to_head, rng)[:3]
            seeds = seeded_div_winners + wildcards  # index 0 = seed 1 ... index 6 = seed 7
            conf_seeds[conf] = seeds
            for t in seeds:
                tallies[t]["playoff"] += 1
            tallies[seeds[0]]["one_seed"] += 1

        # stage of elimination: default everyone to "missed playoffs", then
        # overwrite each playoff team with the round it actually exits in.
        trial_stage = {t: "reg" for t in all_teams}

        conf_champs: dict[str, str] = {}
        for conf in ("AFC", "NFC"):
            seeds = conf_seeds[conf]
            seed_num = {team: i + 1 for i, team in enumerate(seeds)}

            def home_away(a: str, b: str) -> tuple[str, str]:
                return (a, b) if seed_num[a] < seed_num[b] else (b, a)

            wc_winners = [seeds[0]]  # 1-seed byes
            for hi, lo in ((1, 6), (2, 5), (3, 4)):  # seed2v7, seed3v6, seed4v5
                home, away = home_away(seeds[hi], seeds[lo])
                w = _simulate_game(ratings, home, away, effective_home_field_adv(home, None), rng, offsets)
                wc_winners.append(w)
                trial_stage[seeds[lo] if w == seeds[hi] else seeds[hi]] = "wc"  # WC loser

            survivors = sorted(wc_winners, key=lambda t: seed_num[t])
            one_seed_team, lowest_remaining = survivors[0], survivors[-1]
            middle_two = survivors[1:3]

            home, away = home_away(one_seed_team, lowest_remaining)
            dw1 = _simulate_game(ratings, home, away, effective_home_field_adv(home, None), rng, offsets)
            trial_stage[one_seed_team if dw1 == lowest_remaining else lowest_remaining] = "div"
            home, away = home_away(middle_two[0], middle_two[1])
            dw2 = _simulate_game(ratings, home, away, effective_home_field_adv(home, None), rng, offsets)
            trial_stage[middle_two[0] if dw2 == middle_two[1] else middle_two[1]] = "div"

            home, away = home_away(dw1, dw2)
            conf_champ = _simulate_game(ratings, home, away, effective_home_field_adv(home, None), rng, offsets)
            trial_stage[dw1 if conf_champ == dw2 else dw2] = "conf"  # conf-championship-game loser
            conf_champs[conf] = conf_champ
            tallies[conf_champ]["conf_champ"] += 1

        sb_champ = _simulate_game(ratings, conf_champs["AFC"], conf_champs["NFC"], 0.0, rng, offsets)  # neutral site
        tallies[sb_champ]["sb_champ"] += 1
        sb_loser = conf_champs["NFC"] if sb_champ == conf_champs["AFC"] else conf_champs["AFC"]
        trial_stage[sb_champ] = "sb_win"
        trial_stage[sb_loser] = "sb_loss"
        for t, stg in trial_stage.items():
            tallies[t]["stage_" + stg] += 1

    results = {
        t: {
            "division_pct": tallies[t]["division"] / n_trials,
            "playoff_pct": tallies[t]["playoff"] / n_trials,
            "one_seed_pct": tallies[t]["one_seed"] / n_trials,
            "conf_champ_pct": tallies[t]["conf_champ"] / n_trials,
            "sb_champ_pct": tallies[t]["sb_champ"] / n_trials,
            "undefeated_pct": tallies[t]["undefeated"] / n_trials,
            "best_record_pct": tallies[t]["best_record"] / n_trials,
            "worst_record_pct": tallies[t]["worst_record"] / n_trials,
            "win_count_pct": [c / n_trials for c in tallies[t]["win_hist"]],
            # Stage of elimination -- the six are exhaustive and sum to 1.0.
            "stage_exit_pct": {
                "reg": tallies[t]["stage_reg"] / n_trials,
                "wc": tallies[t]["stage_wc"] / n_trials,
                "div": tallies[t]["stage_div"] / n_trials,
                "conf": tallies[t]["stage_conf"] / n_trials,
                "sb_loss": tallies[t]["stage_sb_loss"] / n_trials,
                "sb_win": tallies[t]["stage_sb_win"] / n_trials,
            },
        }
        for t in all_teams
    }
    results["_LEAGUE"] = {
        "any_undefeated_pct": any_undefeated_count / n_trials,
        "any_wins_ge_pct": [c / n_trials for c in any_wins_ge_hist],
    }
    results["_DIVISIONS"] = {
        div: {
            "order_pct": {order: c / n_trials for order, c in d["order"].items()},
            "total_win_hist_pct": [c / n_trials for c in d["total_win_hist"]],
        }
        for div, d in div_tallies.items()
    }
    return results

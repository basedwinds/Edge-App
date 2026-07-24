"""Throwaway experiment (not wired into production): tests whether giving a
team's first few post-roster-change matches a BOOSTED K (their pre-change
rating partly reflects players no longer on the roster, so the first real
post-change results should count for more) improves walk-forward Brier vs.
the current, already-shipped flat-K per-series model. Same hypothesis shape
as test_valorant_patch_signal.py's patch-boost experiment, applied to a real
roster-change event instead of a real patch-version change.

Real transfer history from scripts/build_cs2_transfer_history_cache.py
(14,849 real events, Liquipedia's Player_Transfers/{year}/{month} archive,
2023-05 through 2026-07), matched to each real historical match by team name
and date. A team's OWN most recent transfer event (either a player joining
OR leaving -- either one changes the roster) resets its own
"games_since_roster_change" counter, same reset-on-change logic as the
already-shipped Valorant patch-boost."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.calibration import brier_score  # noqa: E402
from app.models.baseline.elo_cs2 import BASE_RATING, K as SHIPPED_K, map_win_prob, series_score_distribution  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "cs2_historical_match_cache.json"
TRANSFER_CACHE_PATH = DATA_DIR / "cs2_transfer_history_cache.json"
WARMUP = 800


def load_matches():
    rows = json.loads(MATCH_CACHE_PATH.read_text(encoding="utf-8"))
    rows = [r for r in rows if r.get("best_of") and r.get("winner")]
    rows.sort(key=lambda r: r.get("estimated_start_time") or r["match_date"])
    return rows


def load_transfers_by_team():
    events = json.loads(TRANSFER_CACHE_PATH.read_text(encoding="utf-8"))
    by_team: dict[str, list[str]] = {}
    for e in events:
        by_team.setdefault(e["team"], []).append(e["date"])
    for team in by_team:
        by_team[team].sort()
    n_teams = len(by_team)
    print(f"{len(events)} real transfer events across {n_teams} real teams")
    return by_team


def prob_series_win_a(a_r, b_r, best_of):
    map_p = map_win_prob(a_r, b_r)
    dist = series_score_distribution(map_p, best_of)
    return sum(p for (a, b), p in dist.items() if a > b)


def run_walkforward(matches, transfers_by_team, boost_multiplier, boost_games):
    """boost_multiplier=1.0/boost_games=0 reproduces the EXISTING shipped
    behavior exactly (no roster-tenure awareness) -- used as the real
    baseline."""
    ratings = {}
    games_since_change = {}
    last_change_seen = {}  # team -> the most recent real transfer date already accounted for
    preds, outcomes = [], []

    def effective_k(team):
        if boost_games <= 0:
            return SHIPPED_K
        if games_since_change.get(team, 999) < boost_games:
            return SHIPPED_K * boost_multiplier
        return SHIPPED_K

    for m in matches:
        team_a, team_b, best_of, winner = m["team_a"], m["team_b"], m["best_of"], m["winner"]
        date = m.get("estimated_start_time") or m.get("match_date") or ""

        for team in (team_a, team_b):
            team_transfers = transfers_by_team.get(team, [])
            # Most recent real transfer for this team strictly before this match.
            prior = [d for d in team_transfers if d < date]
            latest = prior[-1] if prior else None
            if latest is not None and latest != last_change_seen.get(team):
                games_since_change[team] = 0
                last_change_seen[team] = latest

        a_r = ratings.get(team_a, BASE_RATING)
        b_r = ratings.get(team_b, BASE_RATING)
        preds.append(prob_series_win_a(a_r, b_r, best_of))
        outcomes.append(1.0 if winner == "team_a" else 0.0)

        map_p = map_win_prob(a_r, b_r)
        actual_a = 1.0 if winner == "team_a" else 0.0
        k_a, k_b = effective_k(team_a), effective_k(team_b)
        delta_a = k_a * (actual_a - map_p)
        delta_b = k_b * (actual_a - map_p)
        ratings[team_a] = a_r + delta_a
        ratings[team_b] = b_r - delta_b
        # Only teams that have ALREADY had a real transfer detected get an
        # entry here -- a brand-new team with no tracked transfer must NOT
        # silently start accumulating a "games since change" count of its
        # own (that would boost K for every new team's first few games,
        # confounding "recently changed roster" with "cold start", a real
        # bug caught here before shipping).
        if team_a in games_since_change:
            games_since_change[team_a] += 1
        if team_b in games_since_change:
            games_since_change[team_b] += 1

    return preds, outcomes


def main():
    matches = load_matches()
    transfers_by_team = load_transfers_by_team()
    print(f"{len(matches)} real matches loaded")

    match_teams = set(m["team_a"] for m in matches) | set(m["team_b"] for m in matches)
    covered = sum(1 for t in match_teams if t in transfers_by_team)
    print(f"{covered}/{len(match_teams)} real match-cache teams have at least 1 tracked real transfer event")

    baseline_preds, baseline_outcomes = run_walkforward(matches, transfers_by_team, 1.0, 0)
    baseline_brier = brier_score(baseline_preds[WARMUP:], baseline_outcomes[WARMUP:])
    print(f"\nBaseline (shipped, no roster-tenure awareness), K={SHIPPED_K}: Brier = {baseline_brier:.5f}")

    print(f"\n{'boost x':>8}  {'boost games':>12}  {'Brier':>10}  {'vs baseline':>12}")
    for boost_mult in (1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8):
        for boost_games in (2, 3, 4):
            preds, outcomes = run_walkforward(matches, transfers_by_team, boost_mult, boost_games)
            b = brier_score(preds[WARMUP:], outcomes[WARMUP:])
            diff = b - baseline_brier
            print(f"{boost_mult:>8}  {boost_games:>12}  {b:>10.5f}  {diff:>+12.5f}")


if __name__ == "__main__":
    main()

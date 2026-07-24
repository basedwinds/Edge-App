"""Real-data check for candidate Tennis situational signals -- same
discipline as check_mma_situational_signals.py/check_mma_round2_signals.py:
compute each candidate feature walk-forward (no leakage) alongside the
already-shipped Elo model's own residual (actual outcome minus predicted
win probability), then report the real correlation BEFORE building
anything. Nothing here is wired into the live app -- this is the
investigation step, not the build.

Candidates checked, all computable from data already on hand (no new
source needed):
  1. Rest-days differential (days since each player's last match, any tier)
  2. Head-to-head record (prior meetings between these two exact players)
  3. Surface transition (tour-level only, surface known) -- didn't just play
     this same surface last time out
  4. Rank-vs-Elo divergence (tour-level only, real WRank exists) -- does the
     official ranking know something Elo's pure win/loss history doesn't
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import tennis_data  # noqa: E402
from app.models.baseline.elo_tennis import TennisEloState, predict_and_update  # noqa: E402


def ols_slope(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Simple OLS y = intercept + slope*x. Returns (slope, intercept)."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    slope = cov / vx if vx else 0.0
    intercept = my - slope * mx
    return slope, intercept


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return float("nan")
    return cov / (vx * vy) ** 0.5


def main():
    matches = tennis_data.load_matches()
    print(f"Total matches in merged cache: {len(matches)}")

    state = TennisEloState()
    last_match_date: dict[str, str] = {}  # player_key -> ISO date of their last match (any tier)
    last_surface: dict[str, str] = {}  # player_key -> surface of their last TOUR match with known surface
    h2h: dict[tuple[str, str], list[str]] = {}  # frozenset-ish sorted pair -> list of winner_keys, chronological

    residuals_rest, rest_diffs = [], []
    residuals_h2h, h2h_diffs, h2h_ns = [], [], []
    residuals_surf, surf_transitions = []  , []
    residuals_rank, rank_elo_gaps = [], []

    def days_between(d1: str, d2: str) -> int:
        import datetime as dt
        return (dt.date.fromisoformat(d2) - dt.date.fromisoformat(d1)).days

    for m in matches:
        a, b = m["player_a_key"], m["player_b_key"]
        surface = m.get("surface")
        date = m["match_date"]

        p_a = predict_and_update(state, m)

        has_result = m.get("winner_key") is not None and not m.get("is_retirement")
        if has_result and p_a is not None:
            actual_a = 1.0 if m["winner_key"] == a else 0.0
            residual = actual_a - p_a

            # 1. Rest days differential
            if a in last_match_date and b in last_match_date:
                rest_a = days_between(last_match_date[a], date)
                rest_b = days_between(last_match_date[b], date)
                rest_diff = rest_a - rest_b  # positive = A more rested
                if -60 <= rest_diff <= 60:  # exclude multi-month layoffs, a different phenomenon
                    residuals_rest.append(residual)
                    rest_diffs.append(rest_diff)

            # 2. Head-to-head (only when this exact pair has met before)
            pair_key = tuple(sorted((a, b)))
            prior = h2h.get(pair_key, [])
            if len(prior) >= 3:  # same "minimum 3 prior" threshold as MMA's method-mix check
                a_h2h_wins = sum(1 for w in prior if w == a)
                a_h2h_rate = a_h2h_wins / len(prior)
                residuals_h2h.append(residual)
                h2h_diffs.append(a_h2h_rate - 0.5)
                h2h_ns.append(len(prior))

            # 3. Surface transition (tour-level only, surface known both this match and last)
            if surface and a in last_surface and b in last_surface:
                a_transitioned = 1.0 if last_surface[a] != surface else 0.0
                b_transitioned = 1.0 if last_surface[b] != surface else 0.0
                transition_diff = b_transitioned - a_transitioned  # positive = B switched, A didn't (A should be favored)
                if transition_diff != 0:
                    residuals_surf.append(residual)
                    surf_transitions.append(transition_diff)

            # 4. Does the official ranking explain residual variance Elo's OWN
            # prediction already missed? Since residual = actual - Elo's
            # prediction, directly correlating the raw rank gap against that
            # residual is the right test (Elo's own explanatory power is
            # already netted out by construction) -- no need to separately
            # subtract an Elo term.
            rank_a, rank_b = m.get("player_a_rank"), m.get("player_b_rank")
            if rank_a and rank_b and m["tier"] == "tour":
                rank_gap = rank_b - rank_a  # positive = A ranked better (lower rank number)
                residuals_rank.append(residual)
                rank_elo_gaps.append(float(rank_gap))

        # update tracking state regardless of whether this match was scoreable
        if m.get("winner_key") is not None or m.get("is_retirement"):
            last_match_date[a] = date
            last_match_date[b] = date
        if surface and m.get("tier") == "tour" and (m.get("winner_key") is not None or m.get("is_retirement")):
            last_surface[a] = surface
            last_surface[b] = surface
        if m.get("winner_key") is not None and not m.get("is_retirement"):
            pair_key = tuple(sorted((a, b)))
            h2h.setdefault(pair_key, []).append(m["winner_key"])

    print()
    print(f"1. REST-DAYS DIFFERENTIAL: n={len(rest_diffs)}, r={pearson(rest_diffs, residuals_rest):.4f}")
    slope, intercept = ols_slope(h2h_diffs, residuals_h2h)
    print(f"2. HEAD-TO-HEAD (>=3 prior meetings): n={len(h2h_diffs)}, r={pearson(h2h_diffs, residuals_h2h):.4f}, "
          f"slope={slope:.4f}, intercept={intercept:.4f}, avg prior meetings={sum(h2h_ns)/len(h2h_ns) if h2h_ns else 0:.1f}")
    print(f"3. SURFACE TRANSITION (tour-level only): n={len(surf_transitions)}, r={pearson(surf_transitions, residuals_surf):.4f}")
    slope2, intercept2 = ols_slope(rank_elo_gaps, residuals_rank)
    print(f"4. RANK-vs-ELO DIVERGENCE (tour-level only): n={len(rank_elo_gaps)}, r={pearson(rank_elo_gaps, residuals_rank):.4f}, "
          f"slope={slope2:.6f}, intercept={intercept2:.4f}")


if __name__ == "__main__":
    main()

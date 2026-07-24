"""Calibrate the roster-change CAUTION WINDOW (roster_changes_cs2.py's
LOOKBACK_DAYS, shipped at a deliberately-uncalibrated 30). The informational
"Wait" badge assumes a team's rating is less trustworthy for some window after
a roster change; this measures how long the model's error actually stays
elevated after a real transfer, so the window is set from data instead of a
guess.

Method: walk the real historical CS2 matches forward with the SHIPPED team Elo
(same walk-forward as test_cs2_roster_tenure_signal.py's baseline arm), and for
every scored match record (a) the model's squared error (Brier contribution)
and (b) days since the most recent real roster change on EITHER team (the more
recently-changed side -- a match is "in flux" if either team just changed).
Bucketing Brier by that gap shows where error returns to the no-recent-change
baseline. That day is the justified window.

Why the team model, not the production player-blend: the player-level model
(K_PLAYER/PLAYER_BLEND_WEIGHT) already re-rates the actual lineup, so it
recovers from a roster change AT LEAST as fast as the team model. The team
model's recovery curve is therefore a conservative UPPER BOUND on how long the
caution flag needs to last -- if even it is back to baseline by day X, the flag
never needs to run longer than X.

Self-Brier (not vs market) is used deliberately: only ~78 historical matches
have Kalshi closing prices, far too few to slice into day-since-change buckets,
whereas self-Brier has the full sample. It measures "is the model less accurate
right after a change", which is exactly what the flag is hedging against.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.baseline.elo_cs2 import (  # noqa: E402
    BASE_RATING, K as SHIPPED_K, map_win_prob, series_score_distribution,
)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "cs2_historical_match_cache.json"
TRANSFER_CACHE_PATH = DATA_DIR / "cs2_transfer_history_cache.json"
WARMUP = 800  # same warmup the tenure experiment used -- skip cold-start ratings

# Upper edge (days) of each bucket; None = "no tracked change / very old".
BUCKETS = [7, 14, 21, 30, 45, 60, 90, 180]


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
    return by_team


def prob_series_win_a(a_r, b_r, best_of):
    dist = series_score_distribution(map_win_prob(a_r, b_r), best_of)
    return sum(p for (a, b), p in dist.items() if a > b)


def _days_between(iso_a: str, iso_b: str) -> int | None:
    """Whole days from date iso_b to date iso_a (both 'YYYY-MM-DD...' strings)."""
    import datetime as dt
    try:
        da = dt.date.fromisoformat(iso_a[:10])
        db = dt.date.fromisoformat(iso_b[:10])
    except ValueError:
        return None
    return (da - db).days


def most_recent_change_days(team, match_date, transfers_by_team) -> int | None:
    """Days since this team's most recent real transfer STRICTLY BEFORE the
    match, or None if it has no tracked prior transfer."""
    prior = [d for d in transfers_by_team.get(team, []) if d < match_date]
    if not prior:
        return None
    return _days_between(match_date, prior[-1])


def main():
    matches = load_matches()
    transfers_by_team = load_transfers_by_team()
    print(f"{len(matches)} real matches, {sum(len(v) for v in transfers_by_team.values())} "
          f"transfer events across {len(transfers_by_team)} teams\n")

    ratings: dict[str, float] = {}
    # Per-bucket accumulators: (sum_brier, count)
    bucket_labels = [f"<= {b}d" for b in BUCKETS] + ["no recent (>180d / none)"]
    sums = [0.0] * (len(BUCKETS) + 1)
    counts = [0] * (len(BUCKETS) + 1)

    for i, m in enumerate(matches):
        team_a, team_b, best_of, winner = m["team_a"], m["team_b"], m["best_of"], m["winner"]
        date = m.get("estimated_start_time") or m.get("match_date") or ""

        a_r = ratings.get(team_a, BASE_RATING)
        b_r = ratings.get(team_b, BASE_RATING)
        pred = prob_series_win_a(a_r, b_r, best_of)
        actual_a = 1.0 if winner == "team_a" else 0.0

        if i >= WARMUP and date:
            da = most_recent_change_days(team_a, date, transfers_by_team)
            db = most_recent_change_days(team_b, date, transfers_by_team)
            recent = [d for d in (da, db) if d is not None]
            gap = min(recent) if recent else None  # more recently-changed side
            if gap is None or gap > BUCKETS[-1]:
                idx = len(BUCKETS)  # baseline bucket
            else:
                idx = next(j for j, edge in enumerate(BUCKETS) if gap <= edge)
            sums[idx] += (pred - actual_a) ** 2
            counts[idx] += 1

        # Update ratings (shipped flat-K team model).
        map_p = map_win_prob(a_r, b_r)
        delta = SHIPPED_K * (actual_a - map_p)
        ratings[team_a] = a_r + delta
        ratings[team_b] = b_r - delta

    baseline_brier = sums[-1] / counts[-1] if counts[-1] else float("nan")
    print(f"{'days since change':<28}{'N':>8}{'Brier':>12}{'vs baseline':>14}")
    print("-" * 62)
    for label, s, c in zip(bucket_labels, sums, counts):
        if c == 0:
            print(f"{label:<28}{0:>8}{'--':>12}{'--':>14}")
            continue
        b = s / c
        diff = b - baseline_brier
        flag = ""
        if label != bucket_labels[-1]:
            flag = "  <-- elevated" if diff > 0.005 else ("  ~baseline" if abs(diff) <= 0.005 else "  better")
        print(f"{label:<28}{c:>8}{b:>12.5f}{diff:>+14.5f}{flag}")

    print("-" * 62)
    print(f"Baseline (no recent change) Brier: {baseline_brier:.5f}")
    print("\nReading: the window should end at the first bucket whose Brier has")
    print("returned to ~baseline (diff within +/-0.005) and stays there. Elevated")
    print("early buckets that decay back to baseline = a real, and calibratable,")
    print("caution window; a flat/noisy curve = 30 days is not data-supported and")
    print("the flag is purely precautionary.")


if __name__ == "__main__":
    main()

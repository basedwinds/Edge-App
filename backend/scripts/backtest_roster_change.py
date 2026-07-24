"""Walk-forward backtest for roster_change_rules.py (Round 6) -- same
ablation philosophy as backtest_totals.py: Elo alone vs. Elo + the
roster-change adjustment, both scored against real market moneylines.

Point-in-time correctness, unlike the LIVE app: qb_ratings.py/
skill_position_ratings.py compute career stats from ALL cached PBP
(2012-2025) since "career-to-date" naturally means everything before right
now in production. Reused directly here would leak future-season data into
early test seasons and invalidate the backtest -- so this script rebuilds
season-bounded versions of those same career-rate functions, using only PBP
data from seasons STRICTLY BEFORE the season being tested.

Test seasons: 2022-2025 (4 seasons) -- the earliest testable transition is
2022 (needs 2021's end-of-season depth chart, the earliest season depth-chart
data exists at all). Depth-chart files come in two nflverse schemas (old
per-week format through 2024, new continuous-timestamp format from 2025 on
-- see depth_chart_client.py's docstring); this script's own parser handles
both, same as the one-off check that ruled out a generic turnover-count
signal for O-line/D-line/secondary/LB in Round 6.

Small-sample caveat, stated up front rather than glossed over: the signal
only fires when a team had a real starter change at QB/RB/WR/TE AND both the
outgoing and incoming player have enough career volume to be rated -- expect
a small number of qualifying games out of 4 seasons, reported honestly below
rather than treated as a large-sample result.
"""
import glob
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pandas as pd

from app.ingestion import nfl_data
from app.models.baseline.elo import EloState, effective_home_field_adv, win_prob, update_ratings
from app.models.calibration import brier_score, log_loss, moneyline_to_implied_prob, devig_two_way
from app.models.combine import combine_probability
from app.models.news_adjustment.roster_change_rules import compute_roster_change_adjustment
from app.models.qb_ratings import MIN_CAREER_DROPBACKS_TO_RATE, _canonical_key as qb_key
from app.models.skill_position_ratings import MIN_CAREER_PLAYS_TO_RATE, _rate_by_player, _canonical_key as skill_key

DEPTH_CHART_URL = "https://github.com/nflverse/nflverse-data/releases/download/depth_charts/depth_charts_{season}.csv"
SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}
PBP_CACHE_DIR = str(Path(__file__).resolve().parents[2] / "data" / "pbp_cache")

TEST_SEASONS = [2022, 2023, 2024, 2025]


def fetch_depth_chart(season: int) -> list[dict]:
    r = httpx.get(DEPTH_CHART_URL.format(season=season), timeout=60, follow_redirects=True)
    r.raise_for_status()
    import csv, io
    return list(csv.DictReader(io.StringIO(r.text)))


def parse_positions(rows: list[dict], mode: str, season: int) -> dict[str, dict[str, str]]:
    """mode: "earliest" (that season's own Week-1-ish starters) or "latest"
    (that season's end-of-season starters). For the new (dt-based) schema,
    "latest" is capped at Feb 15 of the FOLLOWING year to avoid the next
    offseason's moves bleeding into "end of season" -- same cutoff convention
    as depth_chart_client.py's get_skill_position_starters, which hit this
    exact issue live (a "season" file's snapshots run well into the next
    calendar year)."""
    if not rows:
        return {}
    if "dt" in rows[0]:
        cutoff = f"{season + 1}-02-15"
        chosen: dict[str, str] = {}
        for row in rows:
            team, dt = row["team"], row["dt"]
            if mode == "latest" and dt[:10] > cutoff:
                continue
            if team not in chosen:
                chosen[team] = dt
            elif (mode == "earliest" and dt < chosen[team]) or (mode == "latest" and dt > chosen[team]):
                chosen[team] = dt
        out: dict[str, dict[str, str]] = {}
        for row in rows:
            if row["dt"] != chosen.get(row["team"]):
                continue
            if row.get("pos_abb") not in SKILL_POSITIONS or row["pos_rank"] != "1":
                continue
            out.setdefault(row["team"], {})[row["pos_abb"]] = row["player_name"]
        return out
    else:
        def week_key(r):
            gt_rank = {"REG": 0, "POST": 1}.get(r.get("game_type"), -1)
            try:
                wk = int(r.get("week") or 0)
            except ValueError:
                wk = 0
            return (gt_rank, wk)

        if mode == "earliest":
            target = [r for r in rows if r.get("game_type") == "REG" and r.get("week") == "1"]
        else:
            best: dict[str, tuple] = {}
            for r in rows:
                if r.get("game_type") not in ("REG", "POST"):
                    continue
                team, k = r.get("club_code"), week_key(r)
                if team not in best or k > best[team]:
                    best[team] = k
            target = [r for r in rows if r.get("game_type") in ("REG", "POST") and week_key(r) == best.get(r.get("club_code"))]

        out: dict[str, dict[str, str]] = {}
        for row in target:
            if row.get("depth_team") != "1":
                continue
            out.setdefault(row["club_code"], {})[row["position"]] = row["full_name"]
        return out


def compute_qb_stats_before(season_cutoff: int) -> dict[str, dict]:
    files = sorted(glob.glob(os.path.join(PBP_CACHE_DIR, "pbp_*.parquet")))
    files = [f for f in files if int(re.search(r"pbp_(\d+)", f).group(1)) < season_cutoff]
    if not files:
        return {}
    cols = ["passer_player_name", "qb_dropback", "qb_epa"]
    frames = [pd.read_parquet(f, columns=cols) for f in files]
    pbp = pd.concat(frames, ignore_index=True)
    pbp = pbp[(pbp["qb_dropback"] == 1) & pbp["passer_player_name"].notna() & pbp["qb_epa"].notna()]
    totals = pbp.groupby("passer_player_name").agg(total=("qb_epa", "size"), epa_sum=("qb_epa", "sum"))
    out = {}
    for name, row in totals.iterrows():
        if row["total"] < MIN_CAREER_DROPBACKS_TO_RATE:
            continue
        key = qb_key(name)
        if key:
            out[key] = {"epa_per_dropback": float(row["epa_sum"] / row["total"])}
    return out


def compute_skill_stats_before(season_cutoff: int) -> tuple[dict, dict]:
    files = sorted(glob.glob(os.path.join(PBP_CACHE_DIR, "pbp_*.parquet")))
    files = [f for f in files if int(re.search(r"pbp_(\d+)", f).group(1)) < season_cutoff]
    if not files:
        return {}, {}
    cols = ["rush_attempt", "rusher_player_name", "pass_attempt", "receiver_player_name", "epa"]
    frames = [pd.read_parquet(f, columns=cols) for f in files]
    pbp = pd.concat(frames, ignore_index=True)
    rush = _rate_by_player(pbp[pbp["rush_attempt"] == 1], "rusher_player_name")
    recv = _rate_by_player(pbp[pbp["pass_attempt"] == 1], "receiver_player_name")
    return rush, recv


def main():
    games = nfl_data.fetch_games()
    games = [g for g in games if g["game_type"] == "REG"]
    games.sort(key=lambda g: (g["season"], g["week"]))

    print("Fetching depth charts + computing point-in-time career stats per test season...")
    current_positions, previous_positions = {}, {}
    qb_stats_by_season, rush_stats_by_season, recv_stats_by_season = {}, {}, {}
    for season in TEST_SEASONS:
        current_positions[season] = parse_positions(fetch_depth_chart(season), "earliest", season)
        previous_positions[season] = parse_positions(fetch_depth_chart(season - 1), "latest", season - 1)
        qb_stats_by_season[season] = compute_qb_stats_before(season)
        rush_stats_by_season[season], recv_stats_by_season[season] = compute_skill_stats_before(season)
        print(f"  {season}: {len(current_positions[season])} teams current, {len(previous_positions[season])} teams prior")

    state = EloState()
    elo_preds, adj_preds, market_preds, outcomes = [], [], [], []
    fired_count = 0

    for g in games:
        season = g["season"]
        state.start_season_if_new(season)
        home_r, away_r = state.get(g["home_team"]), state.get(g["away_team"])
        hfa = effective_home_field_adv(g["home_team"], g.get("location"))
        p_home_elo = win_prob(home_r, away_r, hfa)

        has_result = g.get("home_score") is not None and g.get("away_score") is not None
        if has_result:
            update_ratings(state, g["home_team"], g["away_team"], g["home_score"], g["away_score"], hfa)

        if season not in TEST_SEASONS or not has_result or g["home_score"] == g["away_score"]:
            continue
        if g.get("home_moneyline") is None or g.get("away_moneyline") is None:
            continue

        home, away = g["home_team"], g["away_team"]
        adjustment = compute_roster_change_adjustment(
            home, away,
            current_positions[season].get(home, {}), current_positions[season].get(away, {}),
            previous_positions[season].get(home, {}), previous_positions[season].get(away, {}),
            qb_stats_by_season[season], rush_stats_by_season[season], recv_stats_by_season[season],
        )
        if adjustment is not None:
            fired_count += 1
        p_home_adj = combine_probability(p_home_elo, adjustment)

        raw_home = moneyline_to_implied_prob(g["home_moneyline"])
        raw_away = moneyline_to_implied_prob(g["away_moneyline"])
        p_home_market, _ = devig_two_way(raw_home, raw_away)

        actual_home_win = 1.0 if g["home_score"] > g["away_score"] else 0.0
        elo_preds.append(p_home_elo)
        adj_preds.append(p_home_adj)
        market_preds.append(p_home_market)
        outcomes.append(actual_home_win)

    n = len(outcomes)
    print(f"\nScored games ({'-'.join(map(str, [TEST_SEASONS[0], TEST_SEASONS[-1]]))}, REG, non-tie, has moneyline): {n}")
    print(f"Games where the roster-change signal actually fired (>=1 side had a ratable starter swap): {fired_count}")
    print()
    print(f"{'Model':<28}{'Brier':>10}{'LogLoss':>10}")
    print(f"{'Elo alone':<28}{brier_score(elo_preds, outcomes):>10.4f}{log_loss(elo_preds, outcomes):>10.4f}")
    print(f"{'Elo + roster-change':<28}{brier_score(adj_preds, outcomes):>10.4f}{log_loss(adj_preds, outcomes):>10.4f}")
    print(f"{'Market (de-vigged)':<28}{brier_score(market_preds, outcomes):>10.4f}{log_loss(market_preds, outcomes):>10.4f}")

    elo_b = brier_score(elo_preds, outcomes)
    adj_b = brier_score(adj_preds, outcomes)
    print()
    print("=" * 55)
    if adj_b < elo_b:
        print(f"Roster-change signal HELPS: {adj_b:.4f} (with) < {elo_b:.4f} (Elo alone)")
    elif adj_b > elo_b:
        print(f"Roster-change signal HURTS: {adj_b:.4f} (with) > {elo_b:.4f} (Elo alone)")
    else:
        print("Roster-change signal made no measurable difference (identical Brier)")
    print("=" * 55)
    print(f"\nNote: only {fired_count} of {n} games had the signal actually fire -- small-sample result, interpret accordingly.")


if __name__ == "__main__":
    main()

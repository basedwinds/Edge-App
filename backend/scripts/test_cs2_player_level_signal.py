"""Throwaway experiment (not wired into production): tests whether a
PLAYER-level rating (rate individuals, aggregate to the lineup that actually
played) predicts better than the shipped TEAM-name Elo.

The real structural flaw this targets: elo_cs2.py keys ratings on TEAM NAME,
so an org that swaps three players keeps its old rating outright. The
roster-tenure K-boost already shipped is a band-aid on exactly this -- it
reacts to a change having happened, but still can't tell a good new lineup
from a bad one. A player-level model addresses the cause instead.

Lineups come from data/cs2_event_rosters_cache.json (per-EVENT rosters, see
scripts/build_cs2_event_roster_cache.py -- rosters assumed stable within one
event, an honest approximation, not per-match ground truth). Only 29.7% of
historical matches have both lineups resolved, so the team model is trained
on the FULL history while the player model necessarily trains on the covered
subset -- and BOTH are scored on the SAME covered subset, so the comparison
stays apples-to-apples rather than crediting one model with a bigger sample.

Team-side training mirrors the shipped per-series + roster-boost rule
(elo_cs2.py) so the baseline here is the real current model, not a strawman.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.baseline.cs2_lineups import Cs2LineupResolver  # noqa: E402
from app.models.calibration import brier_score  # noqa: E402
from app.models.baseline.elo_cs2 import (  # noqa: E402
    BASE_RATING, K, RATING_CLAMP, ROSTER_BOOST_MULTIPLIER, ROSTER_BOOST_GAMES,
    map_win_prob, series_score_distribution,
)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "cs2_historical_match_cache.json"
ROSTER_CACHE_PATH = DATA_DIR / "cs2_event_rosters_cache.json"
TRANSFER_CACHE_PATH = DATA_DIR / "cs2_transfer_history_cache.json"
WARMUP = 800


def load_matches():
    rows = json.loads(MATCH_CACHE_PATH.read_text(encoding="utf-8"))
    rows = [r for r in rows if r.get("best_of") and r.get("winner")]
    rows.sort(key=lambda r: r.get("estimated_start_time") or r["match_date"])
    return rows


def load_transfers():
    if not TRANSFER_CACHE_PATH.exists():
        return {}
    by = {}
    for e in json.loads(TRANSFER_CACHE_PATH.read_text(encoding="utf-8")):
        by.setdefault(e["team"], []).append(e["date"])
    for t in by:
        by[t].sort()
    return by


def build_resolver(matches):
    """Uses the SHIPPED app/models/baseline/cs2_lineups.py resolver (event
    rosters + transfer-log reconstruction), so this grid search measures the
    exact lineup source production uses -- not a separate, more-optimistic
    copy. Anchor date per tournament = its earliest real match date, same
    derivation as elo_service_cs2.py::refresh_ratings."""
    tournament_dates = {}
    for m in matches:
        slug = m["source_match_id"].split(":")[0]
        d = m.get("match_date")
        if d and (slug not in tournament_dates or d < tournament_dates[slug]):
            tournament_dates[slug] = d
    return Cs2LineupResolver(tournament_dates=tournament_dates)


def series_prob(map_p, best_of):
    dist = series_score_distribution(map_p, best_of)
    return sum(p for (a, b), p in dist.items() if a > b)


def run(matches, resolver, transfers, k_player, blend_w):
    """blend_w=0.0 -> pure team model; 1.0 -> pure player model."""
    team_r, games_since_roster, last_td = {}, {}, {}
    player_r = {}
    team_preds, player_preds, blend_preds, outcomes = [], [], [], []

    for idx, m in enumerate(matches):
        ta, tb, bo, w = m["team_a"], m["team_b"], m["best_of"], m["winner"]
        md = m.get("match_date")
        slug = m["source_match_id"].split(":")[0]
        la = resolver.lineup(slug, ta, m.get("team_a_display"), md)
        lb = resolver.lineup(slug, tb, m.get("team_b_display"), md)
        ar, br = team_r.get(ta, BASE_RATING), team_r.get(tb, BASE_RATING)
        actual = 1.0 if w == "team_a" else 0.0

        if la and lb and idx >= WARMUP:
            t_p = series_prob(map_win_prob(ar, br), bo)
            a_str = sum(player_r.get(p, BASE_RATING) for p in la) / len(la)
            b_str = sum(player_r.get(p, BASE_RATING) for p in lb) / len(lb)
            p_p = series_prob(map_win_prob(a_str, b_str), bo)
            team_preds.append(t_p)
            player_preds.append(p_p)
            blend_preds.append((1 - blend_w) * t_p + blend_w * p_p)
            outcomes.append(actual)

        # --- team update: shipped per-series + roster boost ---
        for team in (ta, tb):
            dates = transfers.get(team)
            td = None
            if dates and md:
                prior = [d for d in dates if d < md]
                td = prior[-1] if prior else None
            if td is not None and last_td.get(team) != td:
                games_since_roster[team] = 0
                last_td[team] = td
        p_a = map_win_prob(ar, br)

        def eff_k(team):
            return K * ROSTER_BOOST_MULTIPLIER if games_since_roster.get(team, ROSTER_BOOST_GAMES) < ROSTER_BOOST_GAMES else K

        team_r[ta] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, ar + eff_k(ta) * (actual - p_a)))
        team_r[tb] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, br - eff_k(tb) * (actual - p_a)))
        for team in (ta, tb):
            if team in games_since_roster:
                games_since_roster[team] += 1

        # --- player update: only when the real lineup is known ---
        if la and lb:
            a_str = sum(player_r.get(p, BASE_RATING) for p in la) / len(la)
            b_str = sum(player_r.get(p, BASE_RATING) for p in lb) / len(lb)
            pp = map_win_prob(a_str, b_str)
            delta = k_player * (actual - pp)
            # Every player on a lineup shares the result equally -- team
            # strength is the MEAN, so moving all members by `delta` moves
            # that mean by exactly `delta`, keeping k_player directly
            # comparable to the team model's own K.
            for p in la:
                player_r[p] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, player_r.get(p, BASE_RATING) + delta))
            for p in lb:
                player_r[p] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, player_r.get(p, BASE_RATING) - delta))

    return team_preds, player_preds, blend_preds, outcomes


def main():
    matches = load_matches()
    resolver = build_resolver(matches)
    transfers = load_transfers()

    tp, pp, bp, out = run(matches, resolver, transfers, k_player=32.0, blend_w=0.5)
    print(f"{len(out)} evaluated matches (both lineups known, post-warmup)")
    print(f"  TEAM model   Brier: {brier_score(tp, out):.5f}")
    print()
    print(f"{'k_player':>9}  {'pure player':>12}  {'best blend w':>13}  {'blend Brier':>12}  {'vs team':>10}")
    team_b = brier_score(tp, out)
    for k_p in (8, 12, 16, 24, 32, 40, 48):
        tp2, pp2, _, out2 = run(matches, resolver, transfers, k_player=k_p, blend_w=0.0)
        pure = brier_score(pp2, out2)
        best_w, best_b = None, None
        for w in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
            blend = [(1 - w) * a + w * b for a, b in zip(tp2, pp2)]
            bb = brier_score(blend, out2)
            if best_b is None or bb < best_b:
                best_b, best_w = bb, w
        print(f"{k_p:>9}  {pure:>12.5f}  {best_w:>13}  {best_b:>12.5f}  {best_b - team_b:>+10.5f}")


if __name__ == "__main__":
    main()

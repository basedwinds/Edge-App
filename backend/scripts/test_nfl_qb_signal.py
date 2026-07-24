"""Feasibility test (task #38): does decomposing NFL team Elo into a
team-minus-QB rating + a per-starting-QB rating beat plain team Elo?

The NFL model currently keys everything on the TEAM and handles a starter
being out only as a flat -6pp injury flag -- it can't tell a great starter
(Mahomes) from a replacement, nor a good backup from a bad one. Every game
already carries home_qb_name/away_qb_name (nflverse, 100% coverage, 7,276
games 1999-2025), so a QB-aware rating is testable with data in hand.

Decomposed model (standard QB-adjusted Elo): each prediction uses
team_rating + QB_WEIGHT * (qb_rating - BASE); after the game, the SAME
result-vs-expectation delta updates the team rating fully and the starting
QB's rating at K_QB. A QB carries their rating across team changes, so when a
starter is injured/benched/traded the swap is continuous, not a flat flag.
QB_WEIGHT=0 exactly reproduces plain team Elo (the baseline).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.database import SessionLocal  # noqa: E402
from app.db.models import NflGame  # noqa: E402
from app.models.calibration import brier_score  # noqa: E402
from app.models.baseline.elo import (  # noqa: E402
    BASE_RATING, K, HOME_FIELD_ADV, SEASON_REGRESSION, win_prob, mov_multiplier,
)

WARMUP = 512  # ~2 early seasons to warm ratings before scoring


def load_games():
    s = SessionLocal()
    try:
        rows = [{
            "season": g.season, "week": g.week, "id": g.id,
            "home": g.home_team, "away": g.away_team,
            "home_score": g.home_score, "away_score": g.away_score,
            "home_qb": g.home_qb_name, "away_qb": g.away_qb_name,
            "roof": g.roof, "location": g.location,
        } for g in s.query(NflGame).filter(
            NflGame.home_score.isnot(None), NflGame.game_type.in_(("REG", "POST"))
        ).all()]
    finally:
        s.close()
    rows.sort(key=lambda g: (g["season"], g["week"], g["id"]))
    return rows


def run_split(games, qb_weight, qb_share):
    """CORRECT test of QB-IDENTITY value, isolated from learning rate: the
    total effective-rating movement per game is IDENTICAL to plain team Elo
    (delta = K*mult*(actual-p)); only its ATTRIBUTION changes -- a fraction
    qb_share persists with the starting QB (carrying across team changes),
    the rest stays with the team. qb_share=0 is exactly plain team Elo.
    Effective rating = team + qb_weight*(qb - BASE); to move it by `delta`
    while giving the QB `qb_share`, team gets (1-qb_share)*delta and the QB
    component gets qb_share*delta (its raw rating moves qb_share*delta/qb_weight
    so the weighted term contributes qb_share*delta)."""
    team, qb = {}, {}
    season_seen = set()
    preds, outs = [], []
    for i, g in enumerate(games):
        if g["season"] not in season_seen:
            season_seen.add(g["season"])
            for t in team:
                team[t] = BASE_RATING + (1 - SEASON_REGRESSION) * (team[t] - BASE_RATING)
            for q in qb:
                qb[q] = BASE_RATING + (1 - SEASON_REGRESSION) * (qb[q] - BASE_RATING)
        h, a, hq, aq = g["home"], g["away"], g["home_qb"], g["away_qb"]
        tr_h, tr_a = team.get(h, BASE_RATING), team.get(a, BASE_RATING)
        eff_h = tr_h + qb_weight * (qb.get(hq, BASE_RATING) - BASE_RATING)
        eff_a = tr_a + qb_weight * (qb.get(aq, BASE_RATING) - BASE_RATING)
        adv = 0.0 if g["location"] == "Neutral" else HOME_FIELD_ADV
        p = win_prob(eff_h, eff_a, adv)
        actual = 1.0 if g["home_score"] > g["away_score"] else (0.0 if g["home_score"] < g["away_score"] else 0.5)
        if i >= WARMUP and actual != 0.5:
            preds.append(p); outs.append(actual)
        pd = g["home_score"] - g["away_score"]
        edwp = (eff_h + adv - eff_a) if pd >= 0 else (eff_a - adv - eff_h)
        mult = mov_multiplier(pd if pd != 0 else 1, edwp)
        delta = K * mult * (actual - p)
        team[h] = tr_h + (1 - qb_share) * delta
        team[a] = tr_a - (1 - qb_share) * delta
        if qb_weight > 0 and qb_share > 0:
            dq = qb_share * delta / qb_weight
            qb[hq] = qb.get(hq, BASE_RATING) + dq
            qb[aq] = qb.get(aq, BASE_RATING) - dq
    return preds, outs


def run(games, qb_weight, k_qb):
    team, qb = {}, {}
    season_seen = set()
    preds, outs = [], []
    for i, g in enumerate(games):
        if g["season"] not in season_seen:
            season_seen.add(g["season"])
            for t in team:
                team[t] = BASE_RATING + (1 - SEASON_REGRESSION) * (team[t] - BASE_RATING)
            for q in qb:
                qb[q] = BASE_RATING + (1 - SEASON_REGRESSION) * (qb[q] - BASE_RATING)
        h, a = g["home"], g["away"]
        hq, aq = g["home_qb"], g["away_qb"]
        tr_h, tr_a = team.get(h, BASE_RATING), team.get(a, BASE_RATING)
        eff_h = tr_h + qb_weight * (qb.get(hq, BASE_RATING) - BASE_RATING)
        eff_a = tr_a + qb_weight * (qb.get(aq, BASE_RATING) - BASE_RATING)
        adv = 0.0 if (g["location"] == "Neutral") else HOME_FIELD_ADV
        p = win_prob(eff_h, eff_a, adv)
        actual = 1.0 if g["home_score"] > g["away_score"] else (0.0 if g["home_score"] < g["away_score"] else 0.5)
        if i >= WARMUP and actual != 0.5:
            preds.append(p)
            outs.append(actual)
        # update: team at K WITH the production margin-of-victory multiplier
        # (faithful to elo.py::update_ratings), QB at k_qb. Both update on the
        # SAME effective-prediction error.
        pd = g["home_score"] - g["away_score"]
        edwp = (eff_h + adv - eff_a) if pd >= 0 else (eff_a - adv - eff_h)
        mult = mov_multiplier(pd if pd != 0 else 1, edwp)
        delta = K * mult * (actual - p)
        team[h] = tr_h + delta
        team[a] = tr_a - delta
        if qb_weight > 0:
            dq = k_qb * mult * (actual - p)
            qb[hq] = qb.get(hq, BASE_RATING) + dq
            qb[aq] = qb.get(aq, BASE_RATING) - dq
    return preds, outs


def main():
    games = load_games()
    print(f"{len(games)} NFL games (REG+POST, 1999-2025)")
    tp, to = run(games, 0.0, 0.0)
    base = brier_score(tp, to)
    print(f"plain team Elo baseline Brier: {base:.5f} ({len(to)} scored)\n")
    print(f"{'qb_weight':>9} {'k_qb':>6} {'Brier':>10} {'vs base':>10}")
    for w in (0.3, 0.5, 0.7, 1.0):
        for kq in (10, 20, 40):
            p, o = run(games, w, kq)
            b = brier_score(p, o)
            print(f"{w:>9} {kq:>6} {b:>10.5f} {b-base:>+10.5f}")


if __name__ == "__main__":
    main()

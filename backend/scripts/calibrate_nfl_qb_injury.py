"""Calibrates NFL injury_rules.py's guessed QB-out weight (QB_MAX_ADJUSTMENT_PP
=6.0) against real outcomes -- no scraping needed, NflGame already carries the
actual starting QB per game (nflverse, 100% coverage 1999-2025).

Method (the NBA calibration recipe, adapted): walk-forward the production team
Elo; for each game flag whether a team is starting a BACKUP (its QB differs
from its established recent starter, i.e. the most-frequent starter across its
trailing 10 games, requiring that established starter to have held the job in a
clear majority). For teams starting a backup (opponent on its normal starter),
measure actual win rate vs Elo prediction. The deficit is the real QB-out
effect the flag should reproduce.
"""
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.database import SessionLocal  # noqa: E402
from app.db.models import NflGame  # noqa: E402
from app.models.baseline.elo import (  # noqa: E402
    BASE_RATING, K, HOME_FIELD_ADV, SEASON_REGRESSION, win_prob, mov_multiplier,
)

TRAIL = 10
MAJORITY = 6  # established starter must own >= this many of the trailing games


def load():
    s = SessionLocal()
    try:
        rows = [{
            "season": g.season, "week": g.week, "id": g.id,
            "home": g.home_team, "away": g.away_team,
            "hs": g.home_score, "as": g.away_score,
            "hqb": g.home_qb_name, "aqb": g.away_qb_name,
            "loc": g.location,
        } for g in s.query(NflGame).filter(NflGame.home_score.isnot(None), NflGame.game_type.in_(("REG", "POST"))).all()]
    finally:
        s.close()
    rows.sort(key=lambda g: (g["season"], g["week"], g["id"]))
    return rows


def main():
    games = load()
    team = {}
    season_seen = set()
    recent_qbs = defaultdict(lambda: deque(maxlen=TRAIL))  # team -> recent starting QBs

    def established(t):
        dq = recent_qbs[t]
        if len(dq) < MAJORITY:
            return None
        qb, n = Counter(dq).most_common(1)[0]
        return qb if n >= MAJORITY else None

    backup_resid, normal_resid = [], []
    for g in games:
        if g["season"] not in season_seen:
            season_seen.add(g["season"])
            for t in team:
                team[t] = BASE_RATING + (1 - SEASON_REGRESSION) * (team[t] - BASE_RATING)
        h, a = g["home"], g["away"]
        hr, ar = team.get(h, BASE_RATING), team.get(a, BASE_RATING)
        adv = 0.0 if g["loc"] == "Neutral" else HOME_FIELD_ADV
        p = win_prob(hr, ar, adv)
        actual = 1.0 if g["hs"] > g["as"] else (0.0 if g["hs"] < g["as"] else 0.5)

        eh, ea = established(h), established(a)
        h_backup = eh is not None and g["hqb"] != eh
        a_backup = ea is not None and g["aqb"] != ea
        if actual != 0.5:
            # home starting a backup, away on its normal starter -> home resid
            if h_backup and not a_backup and ea is not None:
                backup_resid.append(actual - p)
            elif a_backup and not h_backup and eh is not None:
                backup_resid.append((1 - actual) - (1 - p))  # away resid
            elif not h_backup and not a_backup and eh is not None and ea is not None:
                normal_resid.append(actual - p)

        # update (production MOV) + record starters
        pd = g["hs"] - g["as"]
        edwp = (hr + adv - ar) if pd >= 0 else (ar - adv - hr)
        mult = mov_multiplier(pd if pd != 0 else 1, edwp)
        delta = K * mult * (actual - p)
        team[h] = hr + delta
        team[a] = ar - delta
        recent_qbs[h].append(g["hqb"])
        recent_qbs[a].append(g["aqb"])

    import statistics
    print(f"backup-QB-starting games (opp on normal starter): n={len(backup_resid)}")
    print(f"  team starting a backup wins {statistics.mean(backup_resid)*100:+.1f}pp vs pure-team-Elo")
    print(f"  => real QB-out effect ~ {abs(statistics.mean(backup_resid))*100:.1f}pp  (injury_rules QB weight = 6.0pp)")
    print(f"\nsanity -- both teams normal starter: n={len(normal_resid)}, resid {statistics.mean(normal_resid)*100:+.2f}pp (should be ~0)")


if __name__ == "__main__":
    main()

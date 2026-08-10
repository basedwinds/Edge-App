"""Does the national-team (INTL) rating pool actually predict, or is it too thin?

WHY. INTL is the thinnest pool this app stakes on -- national teams play a
handful of competitive matches a year, against a median of 111 matches/team for
the next-thinnest league. Two real bets ($20) currently ride on it. Every other
thin pool in this app got a minimum-sample gate before it was trusted (MMA
fights, esports games); INTL never got one, because nobody had measured whether
it needs one.

METHOD -- PREQUENTIAL, so it is genuinely out-of-sample with no retraining.
elo_service_soccer.refresh_ratings() already walks matches in date order and
calls predict_and_update(). This does the same walk, but SCORES each match with
the state built only from EARLIER matches, before folding it in. A match is
therefore never used to predict itself.

Scored against the same deliberately-dumb baseline check_club_friendlies_signal
uses: one flat league-average scoreline for every match, encoding nothing about
who is playing. Beating it is the minimum bar for "the ratings are doing work".
Reported as mean Poisson deviance on goals AND 3-way Brier, because they can
disagree -- goals can be well fitted while the win/draw/away split is not.

WARMUP. The first matches of any pool are pure noise (both teams sit at the
default rating), and a thin pool spends proportionally longer there. Matches are
only SCORED once both teams have at least --warmup prior appearances, so the
comparison is between leagues at comparable rating maturity rather than one
being punished for its cold start.

The baseline mean is computed over the SCORED matches of that league, which
leaks the league's average scoreline into the baseline. That is deliberate: it
makes the baseline stronger, so the test is conservative for the model.

    python scripts/check_intl_signal.py
    python scripts/check_intl_signal.py --warmup 5 --leagues INTL,E0,SP1
"""
from __future__ import annotations

import argparse
import collections
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import soccer_data  # noqa: E402
from app.models.baseline.elo_soccer import (  # noqa: E402
    SoccerRatingState, home_advantage_for_league, predict_match, update_ratings,
)
from app.models.baseline.team_name_folding import fold as _fold  # noqa: E402,F401  (import kept optional)


def poisson_deviance(g: int, lam: float) -> float:
    lam = max(lam, 1e-9)
    return 2 * ((g * math.log(g / lam) if g > 0 else 0.0) - (g - lam))


def three_way(lh: float, la: float, cap: int = 12):
    ph = pd = pa = 0.0
    for h in range(cap):
        for a in range(cap):
            p = (math.exp(-lh) * lh ** h / math.factorial(h)) * \
                (math.exp(-la) * la ** a / math.factorial(a))
            if h > a:
                ph += p
            elif h == a:
                pd += p
            else:
                pa += p
    return ph, pd, pa


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--warmup", type=int, default=3,
                    help="prior appearances each team needs before a match is scored")
    ap.add_argument("--leagues", default="",
                    help="comma-separated subset; default is every league with enough scored matches")
    ap.add_argument("--min-scored", type=int, default=60)
    args = ap.parse_args()
    want = {s.strip() for s in args.leagues.split(",") if s.strip()}

    matches = soccer_data.load_matches()
    by_league: dict[str, list[dict]] = collections.defaultdict(list)
    for m in matches:
        lg = m.get("league")
        if lg and (not want or lg in want):
            by_league[lg].append(m)

    results = []
    for league, rows in by_league.items():
        rows = [r for r in rows if r.get("match_date")]
        rows.sort(key=lambda r: r["match_date"])
        state = SoccerRatingState(home_log=home_advantage_for_league(league))
        seen: collections.Counter = collections.Counter()
        scored = []  # (lam_h, lam_a, ph, pd, pa, gh, ga)
        for m in rows:
            h, a = m.get("home_team"), m.get("away_team")
            gh, ga = m.get("home_goals_ft"), m.get("away_goals_ft")
            if not h or not a or gh is None or ga is None:
                continue
            # PREDICT FIRST, from earlier matches only.
            if seen[h] >= args.warmup and seen[a] >= args.warmup:
                try:
                    dist = predict_match(state, h, a)
                except Exception:
                    dist = None
                if dist is not None:
                    scored.append((dist.expected_home_goals, dist.expected_away_goals,
                                   dist.prob_home_win(), dist.prob_draw(), dist.prob_away_win(),
                                   int(gh), int(ga)))
            update_ratings(state, h, a, int(gh), int(ga))
            seen[h] += 1
            seen[a] += 1
        if len(scored) < args.min_scored:
            continue
        mu_h = statistics.mean(r[5] for r in scored)
        mu_a = statistics.mean(r[6] for r in scored)
        base_p = three_way(mu_h, mu_a)
        m_dev, b_dev, m_bri, b_bri = [], [], [], []
        for lam_h, lam_a, ph, pd, pa, gh, ga in scored:
            m_dev.append(poisson_deviance(gh, lam_h) + poisson_deviance(ga, lam_a))
            b_dev.append(poisson_deviance(gh, mu_h) + poisson_deviance(ga, mu_a))
            actual = (1.0, 0.0, 0.0) if gh > ga else (0.0, 1.0, 0.0) if gh == ga else (0.0, 0.0, 1.0)
            m_bri.append(sum((p - x) ** 2 for p, x in zip((ph, pd, pa), actual)))
            b_bri.append(sum((p - x) ** 2 for p, x in zip(base_p, actual)))
        teams = len(seen)
        results.append({
            "league": league, "n": len(scored), "teams": teams,
            "median_apps": statistics.median(seen.values()) if seen else 0,
            "dev_gain": statistics.mean(b_dev) - statistics.mean(m_dev),
            "bri_gain": statistics.mean(b_bri) - statistics.mean(m_bri),
        })

    results.sort(key=lambda r: r["dev_gain"])
    print(f"{'league':14s} {'scored':>7s} {'teams':>6s} {'med apps':>9s} "
          f"{'deviance gain':>14s} {'brier gain':>11s}")
    for r in results:
        flag = "  <-- WORSE THAN KNOWING NOTHING" if r["dev_gain"] <= 0 else ""
        print(f"{r['league']:14s} {r['n']:7d} {r['teams']:6d} {r['median_apps']:9.0f} "
              f"{r['dev_gain']:14.4f} {r['bri_gain']:11.4f}{flag}")
    print()
    print("gain = baseline minus model; POSITIVE means the ratings beat a flat "
          "league-average scoreline. Negative means they are worse than knowing nothing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

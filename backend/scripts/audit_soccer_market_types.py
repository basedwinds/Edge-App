"""Calibration audit of EVERY soccer market type, not just the match winner.

WHY THIS EXISTS. audit_soccer_leagues.py checked one number: P(home win), per
league. It found a real problem and it closed a real task -- but soccer prices
37 market types and that audit touched the home leg of exactly one of them:

    correct_score  3926 markets      game_spread     1204
    game_total     1789              first_half_*    ~2000 across 4 types
    moneyline_3way 1554  <- audited  team_total      1262

Every one of those is generated from the same Poisson goal grid, so a flaw in
the grid's SHAPE -- as opposed to its mean, which the home-win test probes --
shows up in totals and correct scores while leaving the match winner looking
fine. Independent Poisson is known to understate draws and score ties, because
real matches have correlated scoring that the independence assumption throws
away. Nothing in this app had ever measured that.

WHAT IS MEASURED, walk-forward, per market type pooled across all leagues:
  * BIAS: mean(actual - predicted). Non-zero means every market of that type
    is mispriced in one direction, which is invisible on any single bet.
  * BRIER vs the BASE RATE of that same market type. Beating a base rate that
    already knows how often, say, BTTS lands is the bar -- otherwise the model
    is re-learning the average rather than separating matches.

Both halves are scored where half-time goals exist (181,503 of 242,745
matches). Second-half outcomes are derived as full-time minus half-time.

NOT SCORED, and this is itself a finding: `ftts` (first team to score, 597
live markets) has no outcome in this data source. football-data records the
score, not the scoring ORDER, so prob_first_to_score cannot be validated here
at all -- it is graded in production from ESPN scoring plays, which this audit
has no access to. It is priced on an unvalidated leg.

MIN_MATCHES matches the league audit so the two are directly comparable.

===========================================================================
RESULT, 2026-08-09. Run BOTH ways; the comparison is the finding.

FIRST: every one of the ~50 market legs BEATS ITS OWN BASE RATE on Brier, at
full time and in both halves. No market type this app prices is noise.

SECOND, and the reason the era split matters: on FULL history almost
everything looked "TILTED" (|z| up to 41). Restricting to 2019+ collapses most
of it, exactly as fit_soccer_home_advantage.py predicted it would.

  COLLAPSED -- was the declining-home-advantage era effect, not a model flaw
    win_home      +0.0198 z +19.9  ->  +0.0006 z  +0.3
    spread_-0.5   +0.0198 z +19.9  ->  +0.0006 z  +0.3
    tt_home_o1.5  +0.0117 z +11.6  ->  -0.0032 z  -1.7
    total_o2.5    -0.0063 z  -6.1  ->  -0.0023 z  -1.2
    total_o1.5    -0.0020 z  -2.2  ->  -0.0001 z  -0.1
    btts          already unbiased both ways (+0.0007 z +0.3)

  SURVIVED the restriction -- these are structural and worth acting on
    win_draw      +0.0176 z +19.2  ->  +0.0142 z  +8.1   draws UNDER-priced
    win_away      -0.0374 z -41.6  ->  -0.0148 z  -8.4   away wins OVER-priced
    total_o4.5    -0.0090 z -12.8  ->  -0.0117 z  -8.6   got WORSE
    total_o3.5    -0.0094 z -10.2  ->  -0.0122 z  -6.9   got WORSE
    tt_away_o2.5  -0.0168 z -26.2  ->  -0.0090 z  -7.0
    tt_away_o1.5  -0.0274 z -29.1  ->  -0.0116 z  -6.3

  CORRECT SCORE, 2019+, is the same finding seen directly. Every TIE is
  under-priced and every away scoreline is over-priced:
    1-1 +0.0081 z +6.2    2-2 +0.0033 z +3.7    0-0 +0.0031 z +3.0
    0-1 -0.0053 z -4.9    0-2 -0.0040 z -4.8

THE DIAGNOSIS. The draw understatement and the over-3.5/over-4.5 overstatement
are the SAME defect with two faces: independent Poisson spreads probability
mass too widely. Real matches have correlated scoring, so the true
distribution has more weight on level scorelines and less in the high-total
tail than two independent Poissons produce. That is precisely the effect the
Dixon-Coles low-score correction exists for, and this model has no such term.

Magnitude: 1.4pp on the draw leg of 1,554 live moneyline_3way markets, and
1.2pp on the over legs of 1,789 game_total markets.

DO NOT ship a correlation term off this run. Fit it and validate it on
held-out seasons first -- and check that Brier does not degrade while bias
improves, which is the trap that killed the per-league home fit.
===========================================================================
"""
from __future__ import annotations

import collections
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import soccer_data  # noqa: E402
from app.models.baseline import elo_service_soccer as E  # noqa: E402

MIN_MATCHES = 10
TOTAL_LINES = (0.5, 1.5, 2.5, 3.5, 4.5)
SPREAD_LINES = (-2.5, -1.5, -0.5, 0.5, 1.5)
TEAM_TOTAL_LINES = (0.5, 1.5, 2.5)
CORRECT_SCORES = ((0, 0), (1, 0), (0, 1), (1, 1), (2, 0), (0, 2), (2, 1), (1, 2), (2, 2))
MIN_N = 2000  # a market type needs this many scored observations to be reported
# Pass a year to score only seasons from then on. NOT optional in practice for
# anything on the home/away axis: fit_soccer_home_advantage.py established that
# home advantage has declined steadily, so a full-history bias on home, away or
# any handicap is mostly a measurement of the 1990s. Run BOTH and compare -- a
# tilt that survives the restriction is structural, one that vanishes was time.
MIN_SEASON_YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 0


def season_year(m) -> int:
    s = (m.get("season") or "")[:4]
    return int(s) if s.isdigit() else 0


def brier(p, y):
    return (p - y) ** 2


class Bucket:
    __slots__ = ("resid", "briers", "actuals", "preds")

    def __init__(self):
        self.resid, self.briers, self.actuals, self.preds = [], [], [], []

    def add(self, p, y):
        self.resid.append(y - p)
        self.briers.append(brier(p, y))
        self.actuals.append(y)
        self.preds.append(p)

    def report(self):
        n = len(self.resid)
        mean_res = statistics.mean(self.resid)
        se = statistics.stdev(self.resid) / math.sqrt(n) if n > 1 else 0.0
        base = statistics.mean(self.actuals)
        return {
            "n": n, "bias": mean_res, "z": mean_res / se if se else 0.0,
            "brier": statistics.mean(self.briers),
            "base_brier": statistics.mean(brier(base, y) for y in self.actuals),
            "rate": base, "pred": statistics.mean(self.preds),
        }


def score_grid(b: dict, prefix: str, dist, hg: int, ag: int) -> None:
    """Score every market type derivable from one goal distribution."""
    tot = hg + ag
    b[f"{prefix}_win_home"].add(dist.prob_home_win(), float(hg > ag))
    b[f"{prefix}_win_draw"].add(dist.prob_draw(), float(hg == ag))
    b[f"{prefix}_win_away"].add(dist.prob_away_win(), float(hg < ag))
    b[f"{prefix}_btts"].add(dist.prob_btts(), float(hg > 0 and ag > 0))
    for ln in TOTAL_LINES:
        b[f"{prefix}_total_o{ln}"].add(dist.prob_total_over(ln), float(tot > ln))
    for ln in SPREAD_LINES:
        b[f"{prefix}_spread_{ln:+}"].add(dist.prob_home_spread_cover(ln), float((hg - ag) > -ln))
    for ln in TEAM_TOTAL_LINES:
        b[f"{prefix}_tt_home_o{ln}"].add(dist.prob_team_total_over("home", ln), float(hg > ln))
        b[f"{prefix}_tt_away_o{ln}"].add(dist.prob_team_total_over("away", ln), float(ag > ln))


def main() -> None:
    matches = [m for m in soccer_data.load_matches()
               if m.get("home_goals_ft") is not None and m.get("away_goals_ft") is not None]
    by_league: dict[str, list[dict]] = collections.defaultdict(list)
    for m in matches:
        lg = m.get("league")
        if lg:
            by_league[lg].append(m)

    b: dict[str, Bucket] = collections.defaultdict(Bucket)
    cs: dict[tuple, Bucket] = collections.defaultdict(Bucket)
    n_ht = 0

    for lg, games in by_league.items():
        games.sort(key=lambda m: (m.get("match_date") or "", m.get("home_team") or ""))
        state = E.SoccerRatingState(home_log=E.home_advantage_for_league(lg))
        seen: collections.Counter = collections.Counter()
        for m in games:
            home, away = m["home_team"], m["away_team"]
            scoreable = seen[home] >= MIN_MATCHES and seen[away] >= MIN_MATCHES
            dist = E.predict_and_update(state, m)  # updates ratings; must run for every match
            seen[home] += 1
            seen[away] += 1
            if not scoreable or dist is None:
                continue
            if season_year(m) < MIN_SEASON_YEAR:
                continue  # still replayed above, just not scored

            hg, ag = m["home_goals_ft"], m["away_goals_ft"]
            score_grid(b, "ft", dist, hg, ag)
            for h, a in CORRECT_SCORES:
                cs[(h, a)].add(dist.prob_correct_score(h, a), float(hg == h and ag == a))

            hh, ha = m.get("home_goals_ht"), m.get("away_goals_ht")
            if hh is None or ha is None:
                continue
            n_ht += 1
            score_grid(b, "h1", E.predict_half(state, home, away, 1), hh, ha)
            score_grid(b, "h2", E.predict_half(state, home, away, 2), hg - hh, ag - ha)

    print(f"{len(matches)} settled matches, {len(by_league)} leagues; {n_ht} also had half-time scores\n")

    def dump(title, rows):
        print(title)
        print(f"  {'market':22s}{'n':>8s}{'bias':>9s}{'z':>8s}{'actual':>8s}{'pred':>8s}"
              f"{'Brier':>9s}{'base':>9s}  verdict")
        for name, r in rows:
            beats = r["brier"] < r["base_brier"]
            tilt = "TILTED" if abs(r["z"]) > 4 else ("lean" if abs(r["z"]) > 2.5 else "ok")
            print(f"  {name:22s}{r['n']:8d}{r['bias']:+9.4f}{r['z']:+8.1f}{r['rate']:8.3f}{r['pred']:8.3f}"
                  f"{r['brier']:9.4f}{r['base_brier']:9.4f}  "
                  f"{'beats base' if beats else 'NO SKILL vs base'} / {tilt}")
        print()

    for prefix, label in (("ft", "FULL TIME"), ("h1", "FIRST HALF"), ("h2", "SECOND HALF")):
        rows = [(k[len(prefix) + 1:], v.report()) for k, v in sorted(b.items())
                if k.startswith(prefix + "_") and len(v.resid) >= MIN_N]
        rows.sort(key=lambda kv: -abs(kv[1]["z"]))
        if rows:
            dump(label, rows)

    rows = [(f"{h}-{a}", v.report()) for (h, a), v in sorted(cs.items()) if len(v.resid) >= MIN_N]
    rows.sort(key=lambda kv: -abs(kv[1]["z"]))
    dump("CORRECT SCORE (the shape test -- independent Poisson understates ties)", rows)

    print("bias = mean(actual - predicted); positive means the model prices the outcome TOO LOW.")
    print("|z| > 4 is a real tilt at these sample sizes, not sampling noise.")
    print("NOT COVERED: ftts (597 live markets) -- this source records the score, not the")
    print("scoring ORDER, so prob_first_to_score cannot be validated here at all.")


if __name__ == "__main__":
    main()

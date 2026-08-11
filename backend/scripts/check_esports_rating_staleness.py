"""Does a STALE Elo rating predict worse? Measured per title, walk-forward.

WHY THIS EXISTS. Live LoL futures showed FEARX sitting in the LCK 2026 Season
Winner field with a rating whose last real game was 2024-03-24 -- 870 days old,
from a roster that has since rebranded to BNK FEARX. A confidently wrong rating
is worse than no rating, so the question is whether to refuse a team whose last
game is far enough back, the way elo_service_soccer already expires a club
rating after 730 days.

The threshold was NOT going to be guessed. MIN_GAMES was fitted by bucketing
Brier on the historical crawl (see elo_service_lol.get_series_distribution's own
docstring) and this uses the same method: replay each title's crawl in date
order, and for every scored match bucket it by how long the STALER of the two
sides had been idle.

VERDICT 2026-08-11: REJECTED, do not gate on staleness.

    bucket        LoL n / acc        CS2 n / acc        Valorant n / acc
    overall       4996 / 0.6859      6519 / 0.6191      13025 / 0.6539
    <30d          4663 / 0.6867      5866 / 0.6190      11759 / 0.6537
    30-90d         222 / 0.6892       438 / 0.6187        852 / 0.6631
    90-180d         87 / 0.7011       141 / 0.6028        238 / 0.6261
    180-365d        11 / 0.3636        57 / 0.6316        142 / 0.7042
    >=365d          13 / 0.5385        17 / 0.7647         34 / 0.4706

LoL alone looks damning: 11 of 24 correct beyond 180 days, against a 0.686
baseline, which is p~0.008 on a binomial test. Taken by itself that would
justify a 180-day gate.

It does not survive the other two titles, and this app never assumes a result
transfers between them (patch signal: rejected for LoL; k-core: rejected for
Valorant; per-map updates: rejected for CS2 -- each measured separately).

  * CS2 shows NO degradation anywhere. Its two stale buckets are BETTER than its
    own baseline, including >=365d at 0.7647.
  * Valorant's 180-365d bucket is n=142 -- the LARGEST stale sample in the whole
    study, six times LoL's -- and it is BETTER than baseline at 0.7042. That
    directly contradicts LoL's 180-365d being the worst bucket in the study, and
    the big sample is the one to believe.
  * Only Valorant >=365d (n=34) corroborates LoL, and it conflicts with
    Valorant's own adjacent bucket.

Fifteen buckets were examined. Two or three looking bad at n=11-34 is what noise
produces. Gating on that would refuse real, priceable markets on a finding that
does not replicate -- so nothing is gated, and the measurement is checked in so
it can be re-run cheaply once the stale buckets carry real weight.

WHAT WOULD CHANGE THE ANSWER: LoL's >=180d buckets reaching n in the hundreds and
still showing sub-baseline accuracy, with at least one other title agreeing.
Re-run this script; it needs no arguments.

A HARNESS NOTE THAT COST THREE WRONG ANSWERS. `winner` in these crawl rows is the
literal string "team_a"/"team_b", NOT the team's name. Comparing it to the name
makes y always 0, which yields Brier ~0.31 and accuracy ~0.486 -- a plausible
looking table that is pure artifact. The OVERALL line is printed against each
title's documented figure for exactly this reason: if it does not reproduce, the
buckets are meaningless. Check the baseline before reading the result.
"""
from __future__ import annotations

import collections
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.baseline import elo_service_cs2, elo_service_lol, elo_service_valorant  # noqa: E402
from app.models.baseline.elo_cs2 import Cs2EloState, predict_and_update as pu_cs2  # noqa: E402
from app.models.baseline.elo_lol import LolEloState, predict_and_update as pu_lol  # noqa: E402
from app.models.baseline.elo_valorant import (  # noqa: E402
    ValorantEloState, predict_and_update as pu_val,
)
from app.models.baseline.lol_lineups import LolLineupResolver  # noqa: E402

BUCKETS = ("<30d", "30-90d", "90-180d", "180-365d", ">=365d")
# Each title's documented walk-forward figures, so a broken harness is visible
# rather than silently producing a table. See the docstring's harness note.
DOCUMENTED = {"lol": 0.679, "cs2": 0.6075, "valorant": 0.65}


def _bucket(age_days: int) -> str:
    if age_days < 30:
        return "<30d"
    if age_days < 90:
        return "30-90d"
    if age_days < 180:
        return "90-180d"
    if age_days < 365:
        return "180-365d"
    return ">=365d"


def _run(name, svc, state_cls, predict_and_update, use_lineups=False) -> None:
    hist = [m for m in svc._load_historical_matches() if m.get("match_date")]
    hist.sort(key=lambda m: m["sort_key"])
    state, last = state_cls(), {}
    resolver = LolLineupResolver() if use_lineups else None
    buckets = collections.defaultdict(lambda: [0, 0.0, 0])
    overall = [0, 0.0, 0]

    for m in hist:
        a, b = m["team_a"], m["team_b"]
        try:
            md = datetime.date.fromisoformat(str(m["match_date"])[:10])
        except Exception:
            continue
        if resolver is not None:
            la, lb = resolver.for_match(m.get("match_date"), a, b)
            m["lineup_a"], m["lineup_b"] = la, lb
        # Games played BEFORE this match -- the same MIN_GAMES gate the live
        # pricing path applies, read pre-update so there is no leakage.
        ga, gb = state.games_played(a), state.games_played(b)
        dist = predict_and_update(state, m)
        winner = m.get("winner")

        if winner in ("team_a", "team_b") and dist is not None \
                and ga >= svc.MIN_GAMES and gb >= svc.MIN_GAMES:
            p = dist.prob_series_win_a()
            y = 1.0 if winner == "team_a" else 0.0
            hit = (p >= 0.5) == (y == 1.0)
            overall[0] += 1
            overall[1] += (p - y) ** 2
            overall[2] += hit
            if a in last and b in last:
                # The STALER side governs: a match is only as current as its
                # least-recently-seen team.
                k = _bucket(max((md - last[a]).days, (md - last[b]).days))
                buckets[k][0] += 1
                buckets[k][1] += (p - y) ** 2
                buckets[k][2] += hit

        if winner in ("team_a", "team_b"):
            last[a] = last[b] = md
            if resolver is not None:
                resolver.note_played(a, b, m["lineup_a"], m["lineup_b"])

    if not overall[0]:
        print(f"\n=== {name}: no scorable matches ===")
        return
    acc = overall[2] / overall[0]
    doc = DOCUMENTED.get(name)
    flag = ""
    if doc and abs(acc - doc) > 0.05:
        flag = f"  <-- DOES NOT MATCH the documented {doc:.4f}; harness is broken, ignore the buckets"
    print(f"\n=== {name}: {len(hist)} crawl matches ===")
    print(f"OVERALL n={overall[0]} Brier={overall[1] / overall[0]:.5f} acc={acc:.4f}{flag}")
    print(f"{'age bucket':12s} {'n':>6s} {'Brier':>8s} {'acc':>7s}")
    for k in BUCKETS:
        n, bs, c = buckets[k]
        if n:
            print(f"{k:12s} {n:6d} {bs / n:8.5f} {c / n:7.4f}")


def main() -> None:
    _run("lol", elo_service_lol, LolEloState, pu_lol, use_lineups=True)
    _run("cs2", elo_service_cs2, Cs2EloState, pu_cs2)
    _run("valorant", elo_service_valorant, ValorantEloState, pu_val)
    print("\nSee this module's docstring for the 2026-08-11 verdict (REJECTED) "
          "and for what would change it.")


if __name__ == "__main__":
    main()

"""Does the tier-1/tier-2 bridge actually hold for DOMESTIC CUP ties? (Task #101.)

THE CONCERN THIS TESTS. season_sim_soccer carries a measured cross-division
bridge -- PROMOTED_TEAM_ATTACK_LOG_DISCOUNT = -0.2558 and
PROMOTED_TEAM_CONCEDE_LOG_DISCOUNT = +0.2444 -- derived from 476 real promotion
events. Pointing it at cup fixtures would let this app price Coppa Italia and
DFB Pokal, which Kalshi lists live. But those 476 events are PROMOTED teams:
the top two or three of each second-tier season. A cup tie pairs a top-flight
club with whatever second-tier club it drew, which is usually mid-table or
worse. Reusing the promoted-team offset for them assumes the discount is
CONSTANT across the whole second-tier quality range, and nobody has checked
that. If the true gap is wider for weaker second-tier clubs, the shipped
constants would systematically overrate cup underdogs -- and since the app
stakes where model > market, it would systematically buy them.

This is the same shape as the two calibration failures this project already
found (racing top_n, MLB season sim): a parameter fitted on one question, then
asked a different one that nobody measured.

METHOD. Pull real completed cup results from ESPN for the four countries where
both tiers are rated (Italy, Germany, Spain, England), resolve club names with
the fixture-verified alias map, keep only ties where BOTH clubs are rated and
they sit in different tiers, then sweep the attack discount and score each
value's Brier against actual outcomes. If the shipped -0.2558 sits at or near
the optimum, the bridge transfers and cup pricing can be built on it. If the
optimum is markedly steeper, it does not, and the constant would need refitting
for cup use.

EXTRA TIME IS EXCLUDED. A Poisson goals model predicts 90 minutes; a cup tie
that goes to extra time or penalties reports a final score that includes goals
the model never predicted, and would score a real 90-minute draw as a win.
Matches whose status shows more than two periods are dropped rather than
silently mis-graded.

BASELINE MATTERS AS ALWAYS. "no bridge" (discount 0.0, i.e. treating a second-
tier rating as if it were a top-flight one) is reported alongside, so the
question is not just "is the bridge optimal" but "does it beat doing nothing".

===========================================================================
RESULT, 2026-08-08. 115 cross-tier cup ties (2 seasons, 4 countries, extra time
excluded). VERDICT: the bridge TRANSFERS -- keep the shipped constant, and flag
cross-tier cup ties rather than refit.

    attack discount   3-way Brier   +/- SE
             0.0000       0.25888   0.01062   <- no bridge at all
            -0.2558       0.20890   0.01464   <- SHIPPED constant
            -0.4500       0.19514   0.02090   <- in-sample optimum

THE BRIDGE IS REAL AND IT IS BIG. Applying it cuts Brier from 0.25888 to
0.20890, roughly 3-4 standard errors. And this is genuine out-of-sample
validation, not a fit: the constant was derived from 476 promotion events and
had never touched a cup fixture. A parameter estimated on one dataset improving
predictions on a different one by 0.05 Brier is about as clean as this gets.

THE ORIGINAL CONCERN WAS ALSO CORRECT, PARTLY. The in-sample optimum is -0.45,
markedly steeper than the shipped -0.2558, exactly as predicted: a cup draw
hands a top-flight club a mid-table second-tier opponent, not the promotion
contenders the constant was fitted on. All four leave-one-cup-out folds
independently chose a steeper value (-0.45, -0.60, -0.60, -0.45). So the
DIRECTION is consistent -- the true gap is wider than the shipped constant.

BUT THE REFIT DOES NOT SURVIVE HOLD-OUT, so it is not shipped:

    held-out cup        n   fitted on rest   refit    shipped
    ita.coppa_italia   28          -0.4500   0.13613  0.16773   refit better
    ger.dfb_pokal      30          -0.6000   0.21189  0.19317   refit WORSE
    esp.copa_del_rey   28          -0.6000   0.25579  0.25840   ~tie
    eng.fa             29          -0.4500   0.19551  0.21714   refit better
    POOLED                                   0.20000  0.20890   +0.00890

A pooled gain of 0.0089 against a standard error of 0.015-0.021 is under one SE,
the sign flips on one of four cups, and the fitted value itself is unstable
across folds (-0.45 vs -0.60). Refitting on 115 in-sample ties would be the same
mistake as the racing attrition fit. Keep -0.2558.

WHAT THIS MEANS FOR SHIPPING CUP PRICING. Because the true discount is probably
steeper than the one in use, the model will tend to OVERRATE the second-tier side
of a cross-tier cup tie. The app stakes where model > market, so the residual
bias points specifically at buying cup underdogs. That is a known, directional,
unquantified risk on a market this app has never priced -- so cross-tier cup
ties should carry a caution flag (as MMA disagreement and the bracket
"approximate" badge already do) and be fed to the forward observation logger,
rather than be treated as a settled model. Same-tier cup ties (two top-flight
clubs) are unaffected and need no bridge at all.
===========================================================================
"""
from __future__ import annotations

import collections
import copy
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.base import get_json  # noqa: E402
from app.ingestion.market_matcher_soccer import canonical_team_key  # noqa: E402
from app.models.baseline import elo_service_soccer  # noqa: E402
from app.models.baseline.elo_soccer import predict_match  # noqa: E402
from app.models.season_sim_soccer import (  # noqa: E402
    PROMOTED_TEAM_ATTACK_LOG_DISCOUNT, PROMOTED_TEAM_CONCEDE_LOG_DISCOUNT,
)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
ALIAS_PATH = DATA_DIR / "soccer_espn_aliases.json"
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard?dates={a}-{b}&limit=500"

# cup slug -> (top flight, second tier)
CUPS = {
    "ita.coppa_italia": ("I1", "I2"),
    "ger.dfb_pokal": ("D1", "D2"),
    "esp.copa_del_rey": ("SP1", "SP2"),
    "eng.fa": ("E0", "E1"),
}
WINDOWS = [(datetime.date(2024, 7, 1), datetime.date(2025, 6, 30)),
           (datetime.date(2025, 7, 1), datetime.date(2026, 6, 30))]
# Sweep around the shipped value. 0.0 is "no bridge at all".
SWEEP = [0.0, -0.10, -0.1779, -0.2558, -0.35, -0.45, -0.60, -0.80]
CONCEDE_RATIO = PROMOTED_TEAM_CONCEDE_LOG_DISCOUNT / -PROMOTED_TEAM_ATTACK_LOG_DISCOUNT


def month_chunks(start: datetime.date, end: datetime.date):
    d = start
    while d <= end:
        nxt = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        yield d, min(nxt - datetime.timedelta(days=1), end)
        d = nxt


def fetch_cup(slug: str):
    out, seen = [], set()
    for window in WINDOWS:
        for a, b in month_chunks(*window):
            try:
                data = get_json(SCOREBOARD.format(slug=slug, a=a.strftime("%Y%m%d"), b=b.strftime("%Y%m%d")))
            except Exception:
                continue
            for ev in data.get("events", []):
                if ev.get("id") in seen:
                    continue
                seen.add(ev.get("id"))
                try:
                    comp = ev["competitions"][0]
                    st = comp.get("status") or ev.get("status") or {}
                    if not st.get("type", {}).get("completed"):
                        continue
                    # EXTRA TIME / PENALTIES -- the model predicts 90 minutes only.
                    if (st.get("period") or 0) > 2:
                        continue
                    home = away = None
                    for c in comp["competitors"]:
                        side = (c["team"]["displayName"], int(c["score"]))
                        if c["homeAway"] == "home":
                            home = side
                        else:
                            away = side
                    if not home or not away:
                        continue
                except (KeyError, IndexError, ValueError, TypeError):
                    continue
                out.append((home[0], away[0], home[1], away[1]))
    return out


def bridged_state(top_state, second_state, second_teams, attack_disc):
    """Top-flight state with each second-tier club injected at a bridged rating."""
    s = copy.deepcopy(top_state)
    for t in second_teams:
        s.attack_log[t] = second_state.get_attack(t) + attack_disc
        s.concede_log[t] = second_state.get_concede(t) + (-attack_disc) * CONCEDE_RATIO
        s.match_counts[t] = second_state.get_count(t)
    return s


def brier3(dist, outcome: str) -> float:
    """3-way Brier: home / draw / away, the market this would actually price."""
    ph = dist.prob_home_win() if hasattr(dist, "prob_home_win") else None
    if ph is None:
        grid = dist.grid
        ph = sum(p for (h, a), p in grid.items() if h > a)
        pd = sum(p for (h, a), p in grid.items() if h == a)
        pa = sum(p for (h, a), p in grid.items() if h < a)
    else:
        pd, pa = dist.prob_draw(), dist.prob_away_win()
    tgt = {"H": (1, 0, 0), "D": (0, 1, 0), "A": (0, 0, 1)}[outcome]
    return sum((p - t) ** 2 for p, t in zip((ph, pd, pa), tgt)) / 2.0


def main() -> None:
    elo_service_soccer.refresh_ratings()
    states = elo_service_soccer._cache["states_by_league"]
    aliases = json.loads(ALIAS_PATH.read_text(encoding="utf-8")) if ALIAS_PATH.exists() else {}
    if not aliases:
        print("NO ALIASES -- run build_soccer_espn_aliases.py first"); sys.exit(1)
    print(f"{len(aliases)} aliases loaded\n")

    def resolve(name: str):
        entry = aliases.get(name)
        if entry:
            return canonical_team_key(entry["team"]), entry["league"]
        k = canonical_team_key(name)
        for lg, st in states.items():
            if st.get_count(k) > 0:
                return k, lg
        return None, None

    ties = []  # (top_lg, second_lg, home_key, away_key, second_key, outcome)
    per_cup = collections.Counter()
    for slug, (top, second) in CUPS.items():
        for hname, aname, hg, ag in fetch_cup(slug):
            hk, hlg = resolve(hname)
            ak, alg = resolve(aname)
            if hk is None or ak is None:
                continue
            if {hlg, alg} != {top, second}:
                continue  # need exactly one top-flight and one second-tier side
            outcome = "H" if hg > ag else ("A" if ag > hg else "D")
            second_key = hk if hlg == second else ak
            ties.append((top, second, hk, ak, second_key, outcome, slug))
            per_cup[slug] += 1

    print(f"{len(ties)} cross-tier cup ties with both clubs rated (extra time excluded)")
    for slug, n in per_cup.most_common():
        print(f"   {slug:22s} {n}")
    if len(ties) < 30:
        print("\nTOO FEW TIES TO CONCLUDE -- reporting anyway, do not ship on this")
    print()

    by_pair = collections.defaultdict(list)
    for t in ties:
        by_pair[(t[0], t[1])].append(t)

    def score(rows_all, disc, only_slug=None):
        """Per-match Brier terms for a discount. Ratings always come from the
        FULL rating states -- only the scored fixture set is restricted, so a
        hold-out tests the parameter, not the ratings."""
        terms = []
        for (top, second), rows in rows_all.items():
            second_teams = {r[4] for r in rows}
            st = bridged_state(states[top], states[second], second_teams, disc)
            for r in rows:
                if only_slug is not None and r[6] != only_slug:
                    continue
                terms.append(brier3(predict_match(st, r[2], r[3]), r[5]))
        return terms

    import math

    print(f"{'attack discount':>16} {'3-way Brier':>12} {'+/- SE':>9} {'note':>26}")
    results = []
    for disc in SWEEP:
        terms = score(by_pair, disc)
        n = len(terms)
        b = sum(terms) / n if n else float("nan")
        var = sum((t - b) ** 2 for t in terms) / (n - 1) if n > 1 else 0.0
        se = math.sqrt(var / n) if n else float("nan")
        note = ""
        if disc == 0.0:
            note = "<- NO BRIDGE (baseline)"
        elif abs(disc - PROMOTED_TEAM_ATTACK_LOG_DISCOUNT) < 1e-9:
            note = "<- SHIPPED constant"
        results.append((b, disc))
        print(f"{disc:>16.4f} {b:>12.5f} {se:>9.5f} {note:>26}")

    best_b, best_d = min(results)
    shipped_b = next(b for b, d in results if abs(d - PROMOTED_TEAM_ATTACK_LOG_DISCOUNT) < 1e-9)
    none_b = next(b for b, d in results if d == 0.0)
    print(f"\noptimum {best_d:+.4f} (Brier {best_b:.5f})")
    print(f"shipped {PROMOTED_TEAM_ATTACK_LOG_DISCOUNT:+.4f} (Brier {shipped_b:.5f}), "
          f"gap to optimum {shipped_b - best_b:+.5f}")
    print(f"no bridge at all      (Brier {none_b:.5f}), "
          f"bridge is worth {none_b - shipped_b:+.5f}")

    # ---- HOLD-OUT. The optimum above is fitted on the same 115 ties it is
    # scored on. Leave-one-cup-out asks whether a refit generalizes to a cup it
    # never saw, which is the only version of this that could justify shipping a
    # new constant. Refitting on in-sample Brier is exactly how the racing
    # attrition fit went wrong.
    print("\n--- LEAVE-ONE-CUP-OUT: does a refit generalize? ---")
    print(f"{'held-out cup':>22} {'n':>4} {'fitted on rest':>15} {'refit Brier':>12} {'shipped Brier':>14}")
    refit_tot, ship_tot = [], []
    for slug in CUPS:
        held = [t for rows in by_pair.values() for t in rows if t[6] == slug]
        if not held:
            continue
        train_best, train_disc = None, None
        for disc in SWEEP:
            terms = [t for (tp, sd), rows in by_pair.items()
                     for t in score({(tp, sd): [r for r in rows if r[6] != slug]}, disc)]
            if not terms:
                continue
            b = sum(terms) / len(terms)
            if train_best is None or b < train_best:
                train_best, train_disc = b, disc
        rt = score(by_pair, train_disc, only_slug=slug)
        sh = score(by_pair, PROMOTED_TEAM_ATTACK_LOG_DISCOUNT, only_slug=slug)
        refit_tot += rt
        ship_tot += sh
        print(f"{slug:>22} {len(rt):>4} {train_disc:>15.4f} "
              f"{sum(rt)/len(rt):>12.5f} {sum(sh)/len(sh):>14.5f}")
    if refit_tot:
        r = sum(refit_tot) / len(refit_tot)
        s_ = sum(ship_tot) / len(ship_tot)
        print(f"\n  POOLED HELD-OUT: refit {r:.5f} vs shipped {s_:.5f} -> "
              f"refit is {s_ - r:+.5f} better")
        print("  (positive = refitting genuinely helps on unseen cups; "
              "near zero = keep the shipped constant)")


if __name__ == "__main__":
    main()

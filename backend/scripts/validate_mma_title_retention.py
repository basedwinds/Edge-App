"""Walk-forward validation of the UFC belt-retention model. (Task #111.)

WHY. fit_mma_title_retention.py fitted the parameters and then named its own
gate: "NO WALK-FORWARD VALIDATION. Matching 2 of 4 champions by eye is not
validation, it is four data points. The archive supports scoring this against
real past year-ends, and that is the gate." This is that gate.

THE TEST. Stand at 11 August of each past year -- the same vantage the live
market is priced from, ~4.7 months out from 31 December -- identify each
division's champion from fights BEFORE that date, predict P(still champion on
31 Dec), and check what actually happened.

NO LEAKAGE. Hazard and retention for year Y are fitted only on title fights
before 1 January of Y. A model that saw Y's own fights would score itself.

THE OUTCOME IS DERIVED, NOT ASSUMED. "Held the belt" means no decisive
non-interim title fight in that division between the vantage and 31 December was
won by someone other than the champion. Vacations, retirements and interim
elevations are invisible in fight results -- the same limitation that made
FIX C fail -- so a division whose belt changed hands administratively scores as
"held" here. That biases the measured accuracy UP, and is stated rather than
hidden.

THE STALENESS GUARD IS PART OF WHAT IS BEING TESTED. fit_ recommended refusing a
division whose last title fight is over ~12 months old, because "most recent
title-fight winner" cannot see a vacated belt (it returns Jon Jones for a
Heavyweight belt Tom Aspinall holds). This applies that guard and reports how
many division-years it excludes, so the cost of the guard is visible.

BASELINE. Scored against the historical base rate of champions holding, because
a model that cannot beat "champions usually hold" is not a model. Same
discipline as every other validation in this app: never against a coin flip.
"""
from __future__ import annotations

import collections
import datetime
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import ufc_data  # noqa: E402
from app.models.baseline.elo_mma import MmaEloState, predict_and_update, win_prob  # noqa: E402
from fit_mma_title_retention import load_title_fights  # noqa: E402

VANTAGE_MONTH, VANTAGE_DAY = 8, 11
STALE_DAYS = 365
FIRST_YEAR = 2013          # modern era; the 1990s tournament rows are excluded upstream
MIN_PRIOR_FIGHTS = 6       # per division, before it can be fitted at all
HAZARD = "flat"            # set by main(); "flat" = first-pass Poisson, "gap" = FIX A
RETENTION = "division"     # "division" = pooled average, "elo" = FIX B, champion-specific
FIELD_SIZE = 6             # how many recent title-fight participants make up "the field"

_ALL_FIGHTS_CACHE: list | None = None
_ELO_BY_YEAR: dict[int, MmaEloState] = {}


def _elo_state_before(year: int) -> MmaEloState:
    """Ratings replayed over every UFC fight BEFORE 1 Jan of `year`.

    Walk-forward by construction: elo_service_mma.refresh_ratings() replays the
    whole cache, so the only change needed for a no-leakage state is to stop the
    replay at the cutoff. Cached per year -- 13 replays, not 13 x divisions.
    """
    global _ALL_FIGHTS_CACHE
    if year in _ELO_BY_YEAR:
        return _ELO_BY_YEAR[year]
    if _ALL_FIGHTS_CACHE is None:
        rows = ufc_data.load_fights()
        for r in rows:
            r["_d"] = r.get("event_date") or r.get("date")
        _ALL_FIGHTS_CACHE = sorted(rows, key=lambda r: str(r.get("_d") or ""))
    cutoff = f"{year:04d}-01-01"
    state = MmaEloState()
    for f in _ALL_FIGHTS_CACHE:
        d = str(f.get("_d") or "")
        if not d or d >= cutoff:
            continue
        predict_and_update(state, f)
    _ELO_BY_YEAR[year] = state
    return state


def _division(fight) -> str:
    return (fight["weight_class"] or "").replace("Interim ", "").strip()


def _run(fights, years) -> None:
    rows, skipped_stale, skipped_thin = [], 0, 0

    for y in years:
        vantage = datetime.date(y, VANTAGE_MONTH, VANTAGE_DAY)
        year_end = datetime.date(y, 12, 31)
        window_years = (year_end - vantage).days / 365.25
        prior = [f for f in fights if f["date"] < datetime.date(y, 1, 1)]
        by_div_prior = collections.defaultdict(list)
        for f in prior:
            by_div_prior[_division(f)].append(f)

        for div, hist in by_div_prior.items():
            if len(hist) < MIN_PRIOR_FIGHTS:
                skipped_thin += 1
                continue
            # Champion as of the vantage: winner of the most recent decisive
            # non-interim title fight in this division before that date.
            before = [f for f in fights if _division(f) == div and f["date"] < vantage]
            if not before:
                continue
            last = before[-1]
            if (vantage - last["date"]).days > STALE_DAYS:
                skipped_stale += 1
                continue
            champ = last["winner_name"]

            # HAZARD. Two variants, because which one is being tested matters:
            #
            #   flat  -- title fights per year, Poisson. This is the FIRST-PASS
            #            hazard, and fit_mma_title_retention's FAILURE 2 is
            #            exactly that it "ignores the schedule".
            #   gap   -- FIX A from the second pass: the empirical distribution
            #            of spacings between consecutive title fights in this
            #            division, asked CONDITIONALLY -- given the belt has
            #            already been idle `elapsed` days, what share of
            #            historical gaps would have completed within the
            #            remaining window? That is what makes it schedule-aware
            #            without an announcement feed.
            span_years = max((hist[-1]["date"] - hist[0]["date"]).days / 365.25, 0.5)
            rate = len(hist) / span_years
            p_fight_flat = 1.0 - math.exp(-rate * window_years)

            gaps = [(hist[i]["date"] - hist[i - 1]["date"]).days
                    for i in range(1, len(hist))]
            elapsed = (vantage - last["date"]).days
            window_days = (year_end - vantage).days
            at_risk = [g for g in gaps if g > elapsed]
            if at_risk:
                completing = [g for g in at_risk if g <= elapsed + window_days]
                p_fight_gap = len(completing) / len(at_risk)
            else:
                # Every historical gap is already shorter than the current idle
                # spell -- the belt is overdue, so a fight is likelier than the
                # base rate, not less. Fall back rather than assert 0.
                p_fight_gap = p_fight_flat
            p_fight = p_fight_gap if HAZARD == "gap" else p_fight_flat

            # RETENTION. Two variants, same reason as the hazard:
            #
            #   division -- how often the defending champion won, pooled over the
            #               division. FAILURE 1 in fit_: every champion is then
            #               equally likely to lose.
            #   elo      -- FIX B: this champion's Elo win probability against the
            #               division's recent title-fight participants. Ratings are
            #               replayed only up to 1 Jan of Y, so no leakage.
            defended = held = 0
            chain = None
            for f in hist:
                if chain is not None:
                    defended += 1
                    if f["winner_name"] == chain:
                        held += 1
                chain = f["winner_name"]
            retention_div = (held / defended) if defended >= 3 else 0.5

            if RETENTION == "elo":
                state = _elo_state_before(y)
                champ_id = last["winner_id"]
                field: list[str] = []
                for f in reversed(before):
                    for fid in (f.get("winner_id"), f.get("loser_id")):
                        if fid and fid != champ_id and fid not in field:
                            field.append(fid)
                    if len(field) >= FIELD_SIZE:
                        break
                if champ_id and field:
                    cr = state.get(champ_id)
                    probs = [win_prob(cr, state.get(o)) for o in field[:FIELD_SIZE]]
                    retention = sum(probs) / len(probs)
                else:
                    retention = retention_div
            else:
                retention = retention_div

            p_hold = 1.0 - p_fight * (1.0 - retention)

            # ACTUAL: did anyone else win this belt before year end?
            after = [f for f in fights
                     if _division(f) == div and vantage <= f["date"] <= year_end]
            lost = any(f["winner_name"] != champ for f in after)
            rows.append({"year": y, "div": div, "champ": champ, "p": p_hold,
                         "held": 0 if lost else 1, "n_after": len(after)})

    if not rows:
        print("no scorable division-years")
        return rows

    n = len(rows)
    base = sum(r["held"] for r in rows) / n
    brier = sum((r["p"] - r["held"]) ** 2 for r in rows) / n
    brier_base = sum((base - r["held"]) ** 2 for r in rows) / n
    mean_p = sum(r["p"] for r in rows) / n

    print(f"\nscored division-years: {n}  ({len(years)} years, "
          f"{skipped_stale} skipped by the 12-month staleness guard, "
          f"{skipped_thin} skipped as too thin)")
    print(f"  actual hold rate      {base:.4f}")
    print(f"  mean predicted        {mean_p:.4f}   bias {mean_p - base:+.4f}")
    print(f"  model Brier           {brier:.5f}")
    print(f"  base-rate Brier       {brier_base:.5f}   <- the bar to beat")
    print(f"  -> {'MODEL BEATS base rate' if brier < brier_base else 'MODEL FAILS to beat base rate'}"
          f" ({100 * (brier_base - brier) / brier_base:+.1f}%)")

    print("\ncalibration by predicted band:")
    buckets = [(0.0, 0.5), (0.5, 0.65), (0.65, 0.8), (0.8, 0.9), (0.9, 1.01)]
    for lo, hi in buckets:
        sel = [r for r in rows if lo <= r["p"] < hi]
        if len(sel) < 5:
            continue
        pm = sum(r["p"] for r in sel) / len(sel)
        am = sum(r["held"] for r in sel) / len(sel)
        print(f"  {lo:.2f}-{hi:<4.2f} n={len(sel):3d}  pred {pm:.3f}  actual {am:.3f}  "
              f"gap {pm - am:+.3f}")

    print("\nper-year:")
    by_year = collections.defaultdict(list)
    for r in rows:
        by_year[r["year"]].append(r)
    for y in sorted(by_year):
        sel = by_year[y]
        print(f"  {y}: n={len(sel):2d}  actual {sum(s['held'] for s in sel) / len(sel):.2f}  "
              f"pred {sum(s['p'] for s in sel) / len(sel):.2f}")
    return rows


def main() -> dict:
    global HAZARD, RETENTION
    fights = [f for f in load_title_fights() if not f["interim"] and f["decisive"]]
    print(f"decisive non-interim title fights in archive: {len(fights)}")
    if not fights:
        return
    print(f"date range: {fights[0]['date']} .. {fights[-1]['date']}")
    years = sorted({f["date"].year for f in fights})
    years = [y for y in years if y >= FIRST_YEAR and y < max(years)]
    # flat+division is the FIRST-PASS model; gap+elo is FIX A + FIX B, i.e. the
    # variant the live code would actually price off. Both middles are run so the
    # contribution of each fix is separable rather than inferred.
    out = {}
    for h, r in (("flat", "division"), ("gap", "division"),
                 ("flat", "elo"), ("gap", "elo")):
        HAZARD, RETENTION = h, r
        bar = "=" * 62
        print(f"\n{bar}\nHAZARD = {h}   RETENTION = {r}\n{bar}")
        out[(h, r)] = _run(fights, years)
    return out


if __name__ == "__main__":
    main()

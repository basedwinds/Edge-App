"""Does TEAM OFFENCE move MLB game totals, measured on top of the pitcher term?

THE LAST STRUCTURAL GAP (#201). After #194 (negative binomial) and #199 (starting
pitchers), expected_total is:

    LEAGUE_AVG_TOTAL + PARK + TEMP + OUT_WIND + PITCHER_KBB

There is still no BATTING-side term at all.

THIS IS NOT THE REJECTED EXPERIMENT. game_lines_mlb's own docstring records that a
trailing team-scoring blend was tested and REJECTED at every rolling window from 5
to 50 games -- each was WORSE than the flat league mean. That tested CURRENT-season
trailing RUNS. This tests PRIOR-season team OPS: a different time base and a
different unit. The precedent for the distinction is #199 itself -- pitchers had
also been "rejected for totals", on ERA, and K-BB% found a real effect the noisier
metric had hidden. Runs scored is to OPS what ERA is to K-BB%: the outcome, not
the rate that generates it.

NO LOOKAHEAD BY CONSTRUCTION, same as #199: prior-season OPS is fully known before
the predicted season starts.

MEASURED ON TOP OF THE PITCHER TERM, NOT IN ISOLATION. Both arms include
PITCHER_KBB exactly as production computes it; the only difference is the offence
term. Fitting offence against a pitcher-free baseline would credit it with
variance the shipped model already explains, and would overstate it.

THE BAR (which has now worked twice): fit on 2023-2025, hold out 2026, choose any
shrink by CV INSIDE train, and judge on PER-MATCHUP error at the volume-carrying
lines -- NOT averaged P(over). In #199 the aggregate endorsed a slope that
overshot by 1.98x, because flat under-prices strong matchups and over-prices weak
ones and those cancel.

Run: backend/.venv/Scripts/python.exe scripts/fit_mlb_total_offence_term.py
"""
from __future__ import annotations

import json
import random
import statistics
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.game_lines_mlb import (  # noqa: E402
    LEAGUE_AVG_COMBINED_KBB, PARK_FACTOR, PITCHER_KBB_SLOPE,
    TOTAL_NB_DISPERSION, _nb_sf,
)
from scripts.fit_mlb_total_pitcher_term import pitcher_kbb, _get  # noqa: E402

TRAIN = (2023, 2024, 2025)
TEST = 2026
LINES = [6.5, 7.5, 8.5, 9.5, 10.5, 11.5]
SEED = 20260815


# JOIN ON TEAM ID, NOT ABBREVIATION. Neither the schedule nor the team-stats
# response carries an `abbreviation` field at all -- both expose only id/name --
# so an abbreviation join silently produced ZERO matched games rather than an
# error. Ids overlap 15/15 on a spot check.
def team_abbr(season: int) -> dict[int, str]:
    """{team_id: abbreviation} -- needed only to look up PARK_FACTOR, which is
    keyed by abbreviation.

    OAK -> ATH: the Athletics relocated, so PARK_FACTOR carries the current key
    while 2023-24 rows come back as OAK. Mapped rather than dropped, but note the
    park factor for those seasons is the CURRENT park's, not the Oakland
    Coliseum's -- one team of thirty, and it enters both arms identically, so it
    cannot bias the offence slope being fitted."""
    d = _get(f"https://statsapi.mlb.com/api/v1/teams?sportId=1&season={season}")
    out = {}
    for t in d.get("teams", []):
        ab = t.get("abbreviation")
        if t.get("id") and ab:
            out[t["id"]] = "ATH" if ab == "OAK" else ab
    return out


def team_ops(season: int) -> dict[int, float]:
    """{team_id: OPS} for a season, one request."""
    d = _get("https://statsapi.mlb.com/api/v1/teams/stats?stats=season&group=hitting"
             f"&season={season}&sportIds=1&gameType=R")
    out = {}
    for s in (d.get("stats") or [{}])[0].get("splits") or []:
        tid = (s.get("team") or {}).get("id")
        ops = (s.get("stat") or {}).get("ops")
        if not tid or ops in (None, ""):
            continue
        try:
            out[tid] = float(ops)
        except ValueError:
            continue
    return out


def games(season: int):
    """Finals with BOTH team abbreviations, both probable pitchers, and a score."""
    d = _get(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&season={season}"
             f"&startDate={season}-03-01&endDate={season}-11-15&gameType=R"
             f"&hydrate=probablePitcher")
    out = []
    for day in d.get("dates", []):
        for g in day.get("games", []):
            if (g.get("status") or {}).get("detailedState") != "Final":
                continue
            t = g.get("teams") or {}
            h, a = t.get("home") or {}, t.get("away") or {}
            hs, as_ = h.get("score"), a.get("score")
            if hs is None or as_ is None:
                continue
            ht = (h.get("team") or {}).get("id")
            at = (a.get("team") or {}).get("id")
            out.append({
                "total": hs + as_, "home": ht, "away": at,
                "hp": (h.get("probablePitcher") or {}).get("id"),
                "ap": (a.get("probablePitcher") or {}).get("id"),
            })
    return out


def build(season: int, kbb: dict, ops: dict, abbr: dict):
    """(combined_ops, combined_kbb, total, home_abbr) -- BOTH terms required, so
    the two arms compare on an identical population."""
    rows = []
    for g in games(season):
        ka, kb = kbb.get(g["hp"]), kbb.get(g["ap"])
        oa, ob = ops.get(g["home"]), ops.get(g["away"])
        ha = abbr.get(g["home"])
        if ka is None or kb is None or oa is None or ob is None or ha is None:
            continue
        rows.append(((oa + ob) / 2.0, (ka + kb) / 2.0, float(g["total"]), ha))
    return rows


def ols(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return slope, mx


def boot(xs, ys, n=3000):
    rnd = random.Random(SEED)
    k = len(xs)
    out = []
    for _ in range(n):
        idx = [rnd.randrange(k) for _ in range(k)]
        out.append(ols([xs[i] for i in idx], [ys[i] for i in idx])[0])
    out.sort()
    return out[int(0.025 * n)], out[int(0.975 * n)]


def main() -> None:
    print("Pulling prior-season pitcher rates and team OPS (season Y uses Y-1)...")
    kbb = {y: pitcher_kbb(y - 1) for y in TRAIN + (TEST,)}
    ops = {y: team_ops(y - 1) for y in TRAIN + (TEST,)}
    abbr = {y: team_abbr(y) for y in TRAIN + (TEST,)}
    tr = []
    for y in TRAIN:
        r = build(y, kbb[y], ops[y], abbr[y])
        tr += r
        print(f"  train {y}: {len(r)} games")
    te = build(TEST, kbb[TEST], ops[TEST], abbr[TEST])
    print(f"  test  {TEST}: {len(te)} games (never used to fit)")

    base_mu = sum(r[2] for r in tr) / len(tr)
    mean_kbb = LEAGUE_AVG_COMBINED_KBB

    # RESIDUAL AFTER the shipped model -- offence must explain what park+pitchers
    # do not. Fitting on raw totals would hand it variance already accounted for.
    resid = [r[2] - (base_mu + PARK_FACTOR.get(r[3], 0.0)
                     + PITCHER_KBB_SLOPE * (r[1] - mean_kbb)) for r in tr]
    xs = [r[0] for r in tr]
    slope, mean_ops = ols(xs, resid)
    lo, hi = boot(xs, resid)
    sd = statistics.pstdev(xs)

    print(f"\n{'='*76}\nSTEP 1 -- does OFFENCE explain the RESIDUAL after park+pitchers?\n{'='*76}")
    print(f"  combined prior-season OPS  mean {mean_ops:.4f}  sd {sd:.4f}")
    print(f"  slope {slope:+.3f} runs per 1.000 OPS   95% CI [{lo:+.3f}, {hi:+.3f}]")
    print(f"  => a 1-sd better offensive matchup is worth {slope*sd:+.3f} runs")
    if lo <= 0.0 <= hi:
        print("\n  CI SPANS ZERO -- no measurable effect beyond park and pitchers.")
        print("  STOP. This is the same verdict the trailing-runs blend got, reached")
        print("  on a different metric, which makes the original rejection stronger")
        print("  rather than merely repeated.")
        return
    if slope < 0:
        print("\n  SLOPE NEGATIVE -- better offences would mean FEWER runs. Backwards;")
        print("  that is a data bug, not a finding. STOP.")
        return
    print("  Sign POSITIVE as expected: better offences -> more runs.")

    # --- CV shrink inside train (2023-24 fit -> 2025 validate) ---
    tr2 = [r for y in (2023, 2024) for r in build(y, kbb[y], ops[y], abbr[y])]
    va = build(2025, kbb[2025], ops[2025], abbr[2025])
    b2 = sum(r[2] for r in tr2) / len(tr2)
    res2 = [r[2] - (b2 + PARK_FACTOR.get(r[3], 0.0)
                    + PITCHER_KBB_SLOPE * (r[1] - mean_kbb)) for r in tr2]
    s2, m2 = ols([r[0] for r in tr2], res2)
    srt = sorted(va, key=lambda r: r[0])
    third = len(srt) // 3
    groups = [srt[:third], srt[third:2 * third], srt[2 * third:]]
    print(f"\nCV shrink (fit 2023-24 slope {s2:+.3f}, validate 2025):")
    best, best_e = None, None
    for sh in [round(0.1 * i, 1) for i in range(0, 13)]:
        tot = 0.0
        for g in groups:
            act = sum(r[2] for r in g) / len(g)
            pred = (b2 + sum(PARK_FACTOR.get(r[3], 0.0) for r in g) / len(g)
                    + PITCHER_KBB_SLOPE * (sum(r[1] for r in g) / len(g) - mean_kbb)
                    + s2 * sh * (sum(r[0] for r in g) / len(g) - m2))
            tot += abs(pred - act)
        e = tot / len(groups)
        if best_e is None or e < best_e:
            best, best_e = sh, e
        print(f"    shrink {sh:.1f}  eff slope {s2*sh:+7.3f}   mean |tercile err| {e:.4f}")
    print(f"  CV picks shrink {best:.1f} -> effective slope {slope*best:+.3f}")

    # --- held-out, per-matchup ---
    eff = slope * best
    print(f"\n{'='*76}\nSTEP 2 -- HELD-OUT {TEST}, per-matchup (the metric that decides)\n{'='*76}")
    srt = sorted(te, key=lambda r: r[0])
    third = len(srt) // 3
    print(f"{'offence tercile':18}{'n':>6}{'actual':>9}{'no-offence':>13}{'+offence':>12}")
    e0 = e1 = 0.0
    for lbl, g in (("weakest", srt[:third]), ("middle", srt[third:2*third]), ("strongest", srt[2*third:])):
        act = sum(r[2] for r in g) / len(g)
        pk = (base_mu + sum(PARK_FACTOR.get(r[3], 0.0) for r in g) / len(g)
              + PITCHER_KBB_SLOPE * (sum(r[1] for r in g) / len(g) - mean_kbb))
        po = pk + eff * (sum(r[0] for r in g) / len(g) - mean_ops)
        e0 += abs(pk - act)
        e1 += abs(po - act)
        print(f"{lbl:18}{len(g):>6}{act:>9.3f}{pk-act:>+13.3f}{po-act:>+12.3f}")
    print(f"{'mean |err|':18}{'':>6}{'':>9}{e0/3:>13.4f}{e1/3:>12.4f}")

    n = len(te)
    print(f"\nheld-out P(over) at the volume lines (reported, NOT the decider):")
    print(f"{'line':>7}{'actual':>9}{'no-offence':>13}{'+offence':>12}")
    for line in LINES:
        act = sum(1 for r in te if r[2] > line) / n
        pa = sum(_nb_sf(line, base_mu + PARK_FACTOR.get(r[3], 0.0)
                        + PITCHER_KBB_SLOPE * (r[1] - mean_kbb), TOTAL_NB_DISPERSION) for r in te) / n
        pb = sum(_nb_sf(line, base_mu + PARK_FACTOR.get(r[3], 0.0)
                        + PITCHER_KBB_SLOPE * (r[1] - mean_kbb)
                        + eff * (r[0] - mean_ops), TOTAL_NB_DISPERSION) for r in te) / n
        print(f"{line:>7.1f}{act:>9.3f}{pa-act:>+13.3f}{pb-act:>+12.3f}")

    print()
    if e1 < e0:
        print(f"  SHIP: per-matchup error {e0/3:.4f} -> {e1/3:.4f}")
        print(f"  OFFENCE_OPS_SLOPE = {eff:.4f}   LEAGUE_AVG_COMBINED_OPS = {mean_ops:.4f}")
    else:
        print(f"  DO NOT SHIP: per-matchup error {e0/3:.4f} -> {e1/3:.4f}, no improvement")
        print(f"  where the term actually fires.")


if __name__ == "__main__":
    main()

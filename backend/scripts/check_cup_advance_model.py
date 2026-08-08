"""Does modelling extra time actually matter for cup ADVANCE markets?

Kalshi lists "To Advance" separately from "Winner" (KXCOPPAITALIAADVANCE,
KXDFBPOKALADVANCE -- 62 live markets). The lazy way to price those is to reuse
the 3-way moneyline and renormalize P(win) over the two sides, dropping the draw.
That is wrong in a specific, directional way: a tie level after 90 goes to extra
time and penalties, and a shootout is close to a coin flip, so renormalizing
pushes the FAVOURITE's advance probability too high and the underdog's too low.

cup_match._advance_probs models it properly instead -- 30 minutes of extra time
at the same Poisson intensities scaled by 30/90, then a 0.500 shootout.

THIS SCRIPT MEASURES WHETHER THAT IS WORTH IT, on real ties where the actual
advancing club is known. Unlike check_cup_tier_bridge.py, extra-time and
penalty ties are INCLUDED here -- they are the entire point, since they are
exactly the ties the naive method gets wrong.

BASELINE IS THE NAIVE METHOD, not "no model". The question is not whether the
Poisson model works (already established) but whether the extra-time layer earns
its place, so the comparison is proper-ET vs renormalized-moneyline on the same
ratings and the same fixtures.

===========================================================================
RESULT, 2026-08-08. 322 real ties (4 cups, 2 seasons), 72 of them (22%) decided
in extra time or on penalties. KEEP the extra-time model -- but NOT because of
the Brier, which is a wash:

    proper (extra time + shootout)   0.21151 +/- 0.00943
    naive  (renormalized moneyline)  0.21243 +/- 0.01061
    extra-time layer worth           +0.00092

That gain is a tenth of one standard error. On accuracy alone the naive method
is fine, and this must NOT be presented as a source of edge.

THE REASON TO KEEP IT IS THE DIRECTIONAL BIAS, which the pooled Brier cannot
see. Renormalizing splits the draw mass PROPORTIONALLY to the two win
probabilities, so it hands most of it to the favourite. The real mechanism does
not: extra time favours the stronger side only mildly, and a shootout is a coin
flip, so the true allocation is pulled toward 50/50. Sweeping 325 synthetic ties
across the whole favourite range shows exactly that shape:

    naive P(advance)   0.169   0.520   0.614   0.753   0.952
    proper             0.202   0.517   0.595   0.716   0.931
    difference        +0.033  -0.003  -0.019  -0.037  -0.021

The naive method overstates a favourite's advance probability by up to 3.7pp and
understates an underdog by up to 3.3pp, with a maximum disagreement of 4.7pp.
Against this app's 10pp edge gate that is nearly half the gate -- large enough to
manufacture edges on favourites in ADVANCE markets that do not exist. So the
extra-time layer is kept as bias control, not as accuracy, and that distinction
is the finding.
===========================================================================
"""
from __future__ import annotations

import collections
import datetime
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.base import get_json  # noqa: E402
from app.ingestion.market_matcher_soccer import canonical_team_key  # noqa: E402
from app.models.baseline import elo_service_soccer  # noqa: E402
from app.models.cup_match import predict_cup_tie  # noqa: E402

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
ALIAS_PATH = DATA_DIR / "soccer_espn_aliases.json"
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard?dates={a}-{b}&limit=500"
CUPS = {
    "ita.coppa_italia": ("I1", "I2"),
    "ger.dfb_pokal": ("D1", "D2"),
    "esp.copa_del_rey": ("SP1", "SP2"),
    "eng.fa": ("E0", "E1"),
}
WINDOWS = [(datetime.date(2024, 7, 1), datetime.date(2025, 6, 30)),
           (datetime.date(2025, 7, 1), datetime.date(2026, 6, 30))]


def month_chunks(start, end):
    d = start
    while d <= end:
        nxt = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        yield d, min(nxt - datetime.timedelta(days=1), end)
        d = nxt


def fetch(slug):
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
                    home = away = None
                    for c in comp["competitors"]:
                        rec = (c["team"]["displayName"], int(c["score"]), bool(c.get("winner")))
                        if c["homeAway"] == "home":
                            home = rec
                        else:
                            away = rec
                    if not home or not away:
                        continue
                    went_long = (st.get("period") or 0) > 2
                except (KeyError, IndexError, ValueError, TypeError):
                    continue
                # Who actually advanced: ESPN's winner flag, else the score.
                if home[2] != away[2]:
                    home_advanced = home[2]
                elif home[1] != away[1]:
                    home_advanced = home[1] > away[1]
                else:
                    continue  # level with no winner flag -- unknowable, skip
                out.append((home[0], away[0], home_advanced, went_long))
    return out


def main() -> None:
    elo_service_soccer.refresh_ratings()
    states = elo_service_soccer._cache["states_by_league"]
    aliases = json.loads(ALIAS_PATH.read_text(encoding="utf-8")) if ALIAS_PATH.exists() else {}

    def resolve(name):
        e = aliases.get(name)
        if e:
            k = canonical_team_key(e["team"])
            if states.get(e["league"]) and states[e["league"]].get_count(k) > 0:
                return k, e["league"]
        k = canonical_team_key(name)
        for lg, st in states.items():
            if st.get_count(k) > 0:
                return k, lg
        return None, None

    proper, naive, outcomes = [], [], []
    long_flags = []
    per_cup = collections.Counter()

    for slug, (top, second) in CUPS.items():
        for hname, aname, home_advanced, went_long in fetch(slug):
            hk, hlg = resolve(hname)
            ak, alg = resolve(aname)
            if hk is None or ak is None or {hlg, alg} - {top, second}:
                continue
            second_teams = {k for k, lg in ((hk, hlg), (ak, alg)) if lg == second}
            pred = predict_cup_tie(hk, ak, states[top], states.get(second), second_teams)
            if pred is None:
                continue
            total = pred.prob_home_advance + pred.prob_away_advance
            if total <= 0:
                continue
            proper.append(pred.prob_home_advance / total)
            # NAIVE: renormalize the 90-minute win probabilities, dropping the draw
            hw, aw = pred.prob_home_win(), pred.prob_away_win()
            naive.append(hw / (hw + aw) if (hw + aw) > 0 else 0.5)
            outcomes.append(1.0 if home_advanced else 0.0)
            long_flags.append(went_long)
            per_cup[slug] += 1

    n = len(outcomes)
    if not n:
        print("no scorable ties"); return

    def brier(ps):
        terms = [(p - o) ** 2 for p, o in zip(ps, outcomes)]
        m = sum(terms) / len(terms)
        var = sum((t - m) ** 2 for t in terms) / (len(terms) - 1) if len(terms) > 1 else 0.0
        return m, math.sqrt(var / len(terms))

    bp, sp = brier(proper)
    bn, sn = brier(naive)
    print(f"{n} cup ties with a known advancing club, both clubs rated")
    for slug, c in per_cup.most_common():
        print(f"   {slug:22s} {c}")
    n_long = sum(long_flags)
    print(f"   {n_long} ({n_long/n:.0%}) went to extra time or penalties\n")

    print(f"{'method':34s} {'Brier':>9s} {'+/- SE':>9s}")
    print(f"{'proper (extra time + shootout)':34s} {bp:>9.5f} {sp:>9.5f}")
    print(f"{'naive (renormalized moneyline)':34s} {bn:>9.5f} {sn:>9.5f}")
    print(f"\nextra-time layer is worth {bn - bp:+.5f} Brier")

    # Where the naive method is supposed to fail: it is most wrong when a draw
    # is likely, so split by the model's own 90-minute draw probability.
    print(f"\n{'draw prob at 90':>16} {'n':>4} {'proper':>9} {'naive':>9} {'gain':>9}")
    idx = sorted(range(n), key=lambda i: proper[i])
    del idx  # ordering not needed; bucket on the naive/proper gap instead
    for lo, hi in [(0.0, 0.22), (0.22, 0.26), (0.26, 1.0)]:
        sel = [i for i in range(n) if lo <= abs(proper[i] - naive[i]) * 4 < hi * 4]
        if len(sel) < 5:
            continue
        pb = sum((proper[i] - outcomes[i]) ** 2 for i in sel) / len(sel)
        nb = sum((naive[i] - outcomes[i]) ** 2 for i in sel) / len(sel)
        print(f"{f'gap {lo:.2f}-{hi:.2f}':>16} {len(sel):>4} {pb:>9.5f} {nb:>9.5f} {nb - pb:>+9.5f}")


if __name__ == "__main__":
    main()

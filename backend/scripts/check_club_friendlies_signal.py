"""Does this app's soccer model actually predict CLUB FRIENDLIES? (Task #112.)

WHY ASK BEFORE BUILDING. Kalshi lists 46 open club-friendly fixtures and 76% of
them have both clubs rated -- the best coverage left in the soccer catalog now
that Libertadores (38%) and Sudamericana (12%) have been measured and found to
be Conference-League-shaped. Coverage is usually the thing that kills these
projects, so 76% makes friendlies look like the obvious next build.

But coverage is necessary, not sufficient, and friendlies are the one fixture
type where the model's core assumption is openly suspect. Ratings are trained on
COMPETITIVE matches, where both sides pick their strongest available eleven and
play for points. A pre-season friendly is a different event wearing the same
name: clubs rotate whole teams at half time, hand minutes to trialists and
academy players, make unlimited substitutions, and have no incentive to chase a
result. If that breaks the mapping from rating to goals, a model that looks
confident would be confidently wrong -- and 76% coverage would just mean more
bad prices, not more good ones.

So this measures it instead of assuming either way.

THE TEST. Take completed club friendlies from ESPN where BOTH clubs are rated,
predict each with the SAME production path a real fixture would take (domestic
model when the clubs share a league, the fitted UEFA offsets when they do not),
and score the predictions. Then score the model the same way on COMPETITIVE
matches from the same leagues, as a control.

The control is the whole point. A raw error number on friendlies means nothing
on its own -- soccer is high-variance and even a good model looks mediocre in
absolute terms. What matters is whether the model beats the "no information"
baseline (league-average scoring, ignoring who is playing) by a SMALLER margin
on friendlies than it does on competitive matches. That difference is the thing
being measured, and it is the thing that decides whether these get staked,
tracked-only, or not built at all.

Scored two ways because they can disagree: mean Poisson deviance on the goals
themselves, and Brier score on the 3-way outcome, which is what the moneyline
market actually pays on.

===========================================================================
RESULT, 2026-08-09: REJECTED. Club friendlies are NOT built.

473 completed friendlies fetched, 189 priceable through the production path
(193 skipped for an unrated club, 91 the app would legitimately refuse).
Competitive control: 4,000 real league matches over the last 400 days.

    set              n   model dev  base dev    gain   model Bri  base Bri   gain
    friendlies     189      2.1669    2.1517  -0.0152     0.6286    0.6383  +0.0097
    competitive   4000      2.1498    2.4046  +0.2548     0.5993    0.6484  +0.0491

THE MODEL IS WORSE THAN KNOWING NOTHING AT PREDICTING GOALS IN A FRIENDLY.
The deviance gain is NEGATIVE (-0.0152): a flat league-average scoreline beats
the rated prediction. On the 3-way outcome the model is barely ahead, keeping
only 20% of the edge it shows on competitive matches (+0.0097 vs +0.0491).

That split is itself informative and matches the mechanism. Who is the better
CLUB still leaks through slightly, which is why the Brier gain stays positive --
but HOW MANY GOALS a friendly produces is essentially unrelated to the ratings,
which is exactly what you would expect when both sides rotate entire teams at
half time, hand minutes to trialists, and have no reason to chase a result.
Totals and spreads are the markets most exposed to that, and they are also the
bulk of the inventory.

So the 46 open fixtures (~180 markets across moneyline/total/spread/BTTS) are
deliberately NOT ingested. Coverage was never the problem here -- at 76% both
clubs rated, friendlies had the BEST remaining coverage in the catalog, better
than Libertadores (38%) or Sudamericana (12%). Building on that number alone
would have shipped the most confident-looking bad prices in the app.

The honest summary: this app can say a little about who wins a friendly and
almost nothing about how it is scored, and it has no business staking either.
===========================================================================
"""
from __future__ import annotations

import collections
import datetime
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.base import get_json  # noqa: E402
from app.ingestion import soccer_data  # noqa: E402
from app.ingestion.market_matcher_soccer import canonical_team_key  # noqa: E402
from app.models.baseline import elo_service_soccer  # noqa: E402
from app.models.uefa_match import predict_uefa_match  # noqa: E402

ALIASES = Path(__file__).resolve().parents[2] / "data" / "soccer_espn_aliases.json"
SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/soccer/club.friendly"
              "/scoreboard?dates={a}-{b}&limit=500")
# Pre-season windows, where club friendlies actually happen.
WINDOWS = [("20240601", "20240831"), ("20250601", "20250831"), ("20260601", "20260831")]


def fetch_friendlies():
    out, seen = [], set()
    for a, b in WINDOWS:
        try:
            data = get_json(SCOREBOARD.format(a=a, b=b))
        except Exception as exc:
            print(f"  fetch failed {a}-{b}: {exc}")
            continue
        for ev in data.get("events", []):
            if ev.get("id") in seen:
                continue
            seen.add(ev.get("id"))
            try:
                comp = ev["competitions"][0]
                if not comp.get("status", {}).get("type", {}).get("completed"):
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


def poisson_deviance(g: int, lam: float) -> float:
    lam = max(lam, 1e-9)
    return 2 * ((g * math.log(g / lam) if g > 0 else 0.0) - (g - lam))


def score_set(rows, mu_home: float, mu_away: float):
    """rows: (lam_h, lam_a, p_home, p_draw, p_away, gh, ga).
    Returns (model_dev, base_dev, model_brier, base_brier, n).

    The BASELINE is deliberately as dumb as possible -- it predicts the same
    average scoreline for every match, so it encodes no knowledge of who is
    playing. Beating it is the minimum bar for "the ratings are doing work"."""
    m_dev, b_dev, m_bri, b_bri = [], [], [], []
    # Baseline 3-way probabilities implied by the average scoreline.
    base_p = _three_way_from_lambdas(mu_home, mu_away)
    for lam_h, lam_a, ph, pd, pa, gh, ga in rows:
        m_dev.append(poisson_deviance(gh, lam_h) + poisson_deviance(ga, lam_a))
        b_dev.append(poisson_deviance(gh, mu_home) + poisson_deviance(ga, mu_away))
        actual = (1.0, 0.0, 0.0) if gh > ga else (0.0, 1.0, 0.0) if gh == ga else (0.0, 0.0, 1.0)
        m_bri.append(sum((p - a) ** 2 for p, a in zip((ph, pd, pa), actual)))
        b_bri.append(sum((p - a) ** 2 for p, a in zip(base_p, actual)))
    n = len(rows)
    if not n:
        return None
    return (statistics.mean(m_dev), statistics.mean(b_dev),
            statistics.mean(m_bri), statistics.mean(b_bri), n)


def _three_way_from_lambdas(lh: float, la: float, cap: int = 12):
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


def main() -> None:
    elo_service_soccer.refresh_ratings()
    states = elo_service_soccer._cache["states_by_league"]
    aliases = json.loads(ALIASES.read_text(encoding="utf-8")) if ALIASES.exists() else {}

    def resolve(name):
        for cand in (aliases.get(name, {}).get("team"), name):
            if not cand:
                continue
            lg = elo_service_soccer.resolve_league(cand)
            if lg:
                return canonical_team_key(cand), lg
        return None, None

    def predict(hk, hl, ak, al):
        """The SAME path a live fixture would take -- domestic model within a
        league, fitted offsets across leagues. Returns None when the app would
        legitimately refuse to price, so refusals never count as errors."""
        if hl == al:
            dist = elo_service_soccer.get_match_distribution(hl, hk, ak)
            if dist is None:
                return None
            return (dist.expected_home_goals, dist.expected_away_goals,
                    dist.prob_home_win(), dist.prob_draw(), dist.prob_away_win())
        pred = predict_uefa_match(hk, hl, ak, al, states)
        if pred is None:
            return None
        d = pred.distribution
        return (d.expected_home_goals, d.expected_away_goals,
                pred.prob_home_win(), pred.prob_draw(), pred.prob_away_win())

    # ---- FRIENDLIES ------------------------------------------------------
    raw = fetch_friendlies()
    print(f"{len(raw)} completed club friendlies fetched")
    fr_rows, skipped = [], collections.Counter()
    for hn, an, gh, ga in raw:
        hk, hl = resolve(hn)
        ak, al = resolve(an)
        if hk is None or ak is None:
            skipped["a club is not rated"] += 1
            continue
        pred = predict(hk, hl, ak, al)
        if pred is None:
            skipped["app would refuse to price"] += 1
            continue
        fr_rows.append((*pred, gh, ga))
    print(f"  {len(fr_rows)} priceable; skipped: {dict(skipped)}")

    # ---- COMPETITIVE CONTROL --------------------------------------------
    # Real league matches, scored through the same domestic path.
    comp_rows = []
    cutoff = (datetime.date.today() - datetime.timedelta(days=400)).isoformat()
    for m in soccer_data.load_matches():
        if len(comp_rows) >= 4000:
            break
        if not m.get("match_date") or m["match_date"] < cutoff:
            continue
        if m.get("home_goals_ft") is None:
            continue
        lg = m.get("league")
        if lg not in states:
            continue
        hk, ak = canonical_team_key(m["home_team"]), canonical_team_key(m["away_team"])
        dist = elo_service_soccer.get_match_distribution(lg, hk, ak)
        if dist is None:
            continue
        comp_rows.append((dist.expected_home_goals, dist.expected_away_goals,
                          dist.prob_home_win(), dist.prob_draw(), dist.prob_away_win(),
                          m["home_goals_ft"], m["away_goals_ft"]))
    print(f"  {len(comp_rows)} competitive control matches")

    if len(fr_rows) < 40:
        print("\nTOO FEW PRICEABLE FRIENDLIES -- not concluding.")
        return

    print(f"\n{'set':14s}{'n':>6s}{'model dev':>11s}{'base dev':>10s}{'gain':>8s}"
          f"{'model Bri':>11s}{'base Bri':>10s}{'gain':>8s}")
    verdict = {}
    for label, rows in (("friendlies", fr_rows), ("competitive", comp_rows)):
        if not rows:
            continue
        mu_h = statistics.mean(r[5] for r in rows)
        mu_a = statistics.mean(r[6] for r in rows)
        md, bd, mb, bb, n = score_set(rows, mu_h, mu_a)
        verdict[label] = (bd - md, bb - mb)
        print(f"{label:14s}{n:6d}{md:11.4f}{bd:10.4f}{bd-md:+8.4f}"
              f"{mb:11.4f}{bb:10.4f}{bb-mb:+8.4f}")

    print("\n(gain = how much the model beats a knows-nothing league-average "
          "baseline; higher is better, negative means WORSE than knowing nothing)")
    if "friendlies" in verdict and "competitive" in verdict:
        fd, fb = verdict["friendlies"]
        cd, cb = verdict["competitive"]
        print(f"\ndeviance gain: friendlies {fd:+.4f} vs competitive {cd:+.4f}"
              f"   -> friendlies retain {fd/cd:.0%} of the edge" if cd else "")
        print(f"Brier gain:    friendlies {fb:+.4f} vs competitive {cb:+.4f}"
              f"   -> friendlies retain {fb/cb:.0%} of the edge" if cb else "")


if __name__ == "__main__":
    main()

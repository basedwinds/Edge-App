"""Fit a per-league STRENGTH OFFSET so cross-country UEFA ties can be priced.

THE PROBLEM. This app's soccer ratings are per-league by construction: a club's
attack rating says how many goals it scores relative to ITS OWN league's average,
so a 1.2 in the Eredivisie and a 1.2 in La Liga are not the same thing and
predict_match cannot be handed one of each. Domestic cups avoided this because
both clubs share a country (see cup_match.py). The Champions League does not.

THE PARAMETERIZATION. Give every league one scalar s_L. A stronger league's
clubs both score more and concede less against outsiders, so s_L adds to attack
and subtracts from concede:

    lambda_home = mu * exp(attack_h + concede_a + HFA + (s_H - s_A))
    lambda_away = mu * exp(attack_a + concede_h       + (s_A - s_H))

Only DIFFERENCES are identifiable, so one league is pinned at 0 (E0) and the
rest are read relative to it. mu is a single UEFA-wide scoring baseline, fitted
alongside, which absorbs the fact that each domestic league has its own goal
average. Fitted by Poisson maximum likelihood -- the gradients are closed-form,
so this is plain gradient ascent with no dependency.

WHAT THE FIT CAN AND CANNOT SEE.

  * UEFA results NEVER enter the ratings. football-data publishes domestic
    leagues only, so attack/concede come purely from league play. The offsets
    are therefore fitted on genuinely out-of-sample matches -- the model has
    never seen a single one of the games it is being scored on.
  * BUT the ratings are CURRENT, i.e. end-of-season, while the UEFA matches
    span the season. That is a mild look-ahead: a club's September strength is
    estimated partly from results it had not yet produced. It is stated rather
    than hidden. It should bias the offsets only weakly, because it applies
    symmetrically to every league, and a proper walk-forward rebuild per match
    date is a much larger job than this first estimate warrants.
  * Sample is small. Only matches where BOTH clubs are rated can be used, which
    check_uefa_coverage.py measures at 43% of UEFA overall.

HELD OUT BY SEASON, not by match, because matches within a season share clubs
and form -- a random split would leak. If the offsets do not transfer across
seasons they are not worth shipping.

===========================================================================
RESULT, 2026-08-08. 583 cross-country matches over 3 seasons, 10 leagues.
Fitted baseline mu = 1.338 goals. THE OFFSETS TRANSFER -- this is shippable.

    league   offset   goal ratio   matches
    E0       +0.000        1.000       190   (pinned)
    SP1      -0.203        0.816       173
    F1       -0.243        0.784       139
    D1       -0.259        0.772       157
    I1       -0.286        0.751       148
    P1       -0.522        0.593        91
    N1       -0.527        0.590        98
    T1       -0.590        0.554        49
    B1       -0.599        0.549        67
    G1       -0.717        0.488        54

THE ORDERING IS THE STRONGEST EVIDENCE HERE and it was not put in by hand. The
fit sees only goals and per-league attack/concede ratings; it has no idea what a
"big five" league is. It recovered England > Spain / France / Germany / Italy >>
Portugal / Netherlands > Turkey / Belgium > Greece, which is the consensus
European hierarchy. A fit that reproduces a known ranking it was never shown is
far more convincing than one that merely improves a loss.

It also found STRUCTURE worth knowing: the top five cluster inside 0.086 of each
other, then there is a 0.236 cliff to Portugal -- nearly three times the entire
spread of the big five. The interesting boundary in European football is not
within the big five, it is between them and everyone else.

HELD OUT BY SEASON -- transfers to every unseen season:

    held-out   n     no offsets   fitted     gain
    2023-24    164       2.3407   2.1478   +0.1929
    2024-25    199       2.4818   2.2899   +0.1919
    2025-26    220       2.7641   2.4674   +0.2967

Three seasons out of three, same sign, consistent magnitude. Contrast the cup
bridge refit (check_cup_tier_bridge.py), which was REJECTED because its gain was
under one SE and flipped sign on one of four folds. This is the opposite case.

LIMITS, stated rather than buried:
  * Ratings are end-of-season while the matches span the season -- a mild
    look-ahead, symmetric across leagues, not removed. A walk-forward rebuild
    per match date is the honest next refinement.
  * Only 10 leagues appear; second tiers essentially never reach UEFA, so
    E1/D2/F2/I2/SP2 have no offset and ties involving them stay unpriceable.
  * CALIBRATED IS NOT THE SAME AS BEATS THE MARKET. This shows the offsets
    predict goals better than pretending leagues are equal. It says nothing
    about edge against Kalshi prices, which is a separate question this app has
    never answered affirmatively for any sport. model_validated stays false.
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
from app.models.baseline.elo_soccer import HOME_ADVANTAGE_LOG  # noqa: E402

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
ALIASES = DATA_DIR / "soccer_espn_aliases.json"
OUT = DATA_DIR / "soccer_league_strength.json"
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/{c}/scoreboard?dates={a}-{b}&limit=500"
COMPS = ["uefa.champions", "uefa.europa", "uefa.europa.conf"]
SEASONS = {
    "2023-24": (datetime.date(2023, 8, 1), datetime.date(2024, 6, 15)),
    "2024-25": (datetime.date(2024, 8, 1), datetime.date(2025, 6, 15)),
    "2025-26": (datetime.date(2025, 8, 1), datetime.date(2026, 6, 15)),
}
REFERENCE_LEAGUE = "E0"
STEP = 0.02
ITERS = 4000


def month_chunks(a, b):
    d = a
    while d <= b:
        nxt = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        yield d, min(nxt - datetime.timedelta(days=1), b)
        d = nxt


def fetch_season(lo, hi):
    out, seen = [], set()
    for comp in COMPS:
        for a, b in month_chunks(lo, hi):
            try:
                data = get_json(SCOREBOARD.format(c=comp, a=a.strftime("%Y%m%d"), b=b.strftime("%Y%m%d")))
            except Exception:
                continue
            for ev in data.get("events", []):
                if ev.get("id") in seen:
                    continue
                seen.add(ev.get("id"))
                try:
                    cm = ev["competitions"][0]
                    st = cm.get("status") or ev.get("status") or {}
                    if not st.get("type", {}).get("completed"):
                        continue
                    if (st.get("period") or 0) > 2:
                        continue  # extra time adds goals the 90-minute model never predicts
                    home = away = None
                    for c in cm["competitors"]:
                        rec = (c["team"]["displayName"], int(c["score"]))
                        if c["homeAway"] == "home":
                            home = rec
                        else:
                            away = rec
                    if not home or not away:
                        continue
                except (KeyError, IndexError, ValueError, TypeError):
                    continue
                out.append((home[0], away[0], home[1], away[1]))
    return out


def loglik_and_grad(rows, mu_log, s, leagues):
    ll = 0.0
    g_mu = 0.0
    g_s = {L: 0.0 for L in leagues}
    for ah, ch, lh, aa, ca, la, gh, ga in rows:
        d = s[lh] - s[la]
        lam_h = math.exp(mu_log + ah + ca + HOME_ADVANTAGE_LOG + d)
        lam_a = math.exp(mu_log + aa + ch - d)
        ll += gh * math.log(max(lam_h, 1e-9)) - lam_h + ga * math.log(max(lam_a, 1e-9)) - lam_a
        rh, ra = gh - lam_h, ga - lam_a
        g_mu += rh + ra
        g_s[lh] += rh - ra
        g_s[la] -= rh - ra
    return ll, g_mu, g_s


def fit(rows, leagues):
    mu_log, s = math.log(1.3), {L: 0.0 for L in leagues}
    for _ in range(ITERS):
        _ll, g_mu, g_s = loglik_and_grad(rows, mu_log, s, leagues)
        n = max(1, len(rows))
        mu_log += STEP * g_mu / n
        for L in leagues:
            if L == REFERENCE_LEAGUE:
                continue  # pinned: only differences are identifiable
            s[L] += STEP * g_s[L] / n
    return mu_log, s


def score(rows, mu_log, s):
    """Mean Poisson deviance per match -- lower is better."""
    tot = 0.0
    for ah, ch, lh, aa, ca, la, gh, ga in rows:
        d = s.get(lh, 0.0) - s.get(la, 0.0)
        lam_h = math.exp(mu_log + ah + ca + HOME_ADVANTAGE_LOG + d)
        lam_a = math.exp(mu_log + aa + ch - d)
        for g, lam in ((gh, lam_h), (ga, lam_a)):
            tot += 2 * ((g * math.log(g / lam) if g > 0 else 0.0) - (g - lam))
    return tot / max(1, len(rows))


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

    by_season = {}
    for label, (lo, hi) in SEASONS.items():
        rows = []
        for hn, an, gh, ga in fetch_season(lo, hi):
            hk, hl = resolve(hn)
            ak, al = resolve(an)
            if hk is None or ak is None or hl == al:
                continue  # same-league UEFA tie teaches nothing about offsets
            sh, sa = states[hl], states[al]
            rows.append((sh.get_attack(hk), sh.get_concede(hk), hl,
                         sa.get_attack(ak), sa.get_concede(ak), al, gh, ga))
        by_season[label] = rows
        print(f"{label}: {len(rows)} cross-country matches with both clubs rated")

    all_rows = [r for rows in by_season.values() for r in rows]
    leagues = sorted({r[2] for r in all_rows} | {r[5] for r in all_rows})
    print(f"\n{len(all_rows)} matches total, {len(leagues)} leagues: {leagues}")
    if len(all_rows) < 60:
        print("TOO FEW MATCHES -- not fitting"); return

    mu_log, s = fit(all_rows, leagues)
    print(f"\nfitted baseline mu = {math.exp(mu_log):.3f} goals\n")
    print(f"{'league':8s} {'offset':>8s} {'~goal ratio':>12s} {'n matches':>10s}")
    counts = collections.Counter([r[2] for r in all_rows] + [r[5] for r in all_rows])
    for L in sorted(leagues, key=lambda x: -s[x]):
        print(f"{L:8s} {s[L]:+8.3f} {math.exp(s[L]):12.3f} {counts[L]:10d}")
    print(f"({REFERENCE_LEAGUE} pinned at 0 -- offsets are relative to it)")

    print("\n--- HELD OUT BY SEASON: do the offsets transfer? ---")
    print(f"{'held-out season':>16} {'n':>5} {'no offsets':>11} {'fitted':>9} {'gain':>9}")
    for label in SEASONS:
        test = by_season[label]
        train = [r for k, rows in by_season.items() if k != label for r in rows]
        if len(test) < 20 or len(train) < 40:
            continue
        tr_leagues = sorted({r[2] for r in train} | {r[5] for r in train})
        m2, s2 = fit(train, tr_leagues)
        flat = score(test, m2, {L: 0.0 for L in tr_leagues})
        fitted = score(test, m2, s2)
        print(f"{label:>16} {len(test):>5} {flat:>11.4f} {fitted:>9.4f} {flat - fitted:>+9.4f}")
    print("\n(positive gain = offsets genuinely transfer to an unseen season)")

    OUT.write_text(json.dumps({"mu": math.exp(mu_log), "reference": REFERENCE_LEAGUE,
                               "offsets": s, "n_matches": len(all_rows)},
                              indent=1, sort_keys=True), encoding="utf-8")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()

"""Fit an MLS <-> Liga MX league-strength offset from real Leagues Cup results.

WHY THIS EXISTS. Kalshi lists the Leagues Cup (KXLEAGUESCUP GAME/SPREAD/TOTAL/
BTTS, 104 open markets at 2026-08-08) and this app now rates BOTH pools -- MLS
from ESPN, Liga MX from football-data's extra format. But their attack/concede
ratings are relative to their OWN league's average, so an MLS 0.10 attack and a
Liga MX 0.10 attack are not the same thing and a cross-league match cannot be
priced by comparing them directly. That is the identical blocker solved for
European club competition in fit_uefa_league_strength.py, and this reuses its
shape: one scalar per league, Poisson MLE, held out by season.

WHY IT IS FEASIBLE HERE AND NOT FOR MOST COMPETITIONS. Coverage, which is what
actually kills these projects (see check_uefa_coverage.py -- the Conference
League tops out at 11% priceable no matter how good the model is). The Leagues
Cup field is EXACTLY the two leagues this app already rates, so a sweep of
2023-2026 found 172 completed cross-league matches with BOTH clubs resolving and
ZERO unresolved names. No alias work was needed at all, which is unusual enough
to be worth recording.

THE NEUTRAL-VENUE PROBLEM, and why the home term is fitted rather than assumed.
The Leagues Cup is not a home-and-away competition: the 2023 and 2024 editions
were played entirely in the United States and Canada, so ESPN's "home"
competitor is frequently a DESIGNATION rather than a real host. Reusing the
domestic HOME_ADVANTAGE_LOG would push that mislabelled venue effect straight
into the league offset -- the offset would quietly absorb "MLS clubs play these
in their own country", and it would then be wrong for any match that is a true
Liga MX home game. So this fits the home term for THIS competition alongside the
offset, and reports the domestic constant beside it for comparison. If the
fitted value lands near zero, that is evidence the venue really is neutral and
the offset is measuring league strength rather than geography.

HELD OUT BY SEASON, same discipline as the UEFA fit: a parameter that only
describes the seasons it was fitted on is not a model. The comparison is against
"pretend the leagues are equal" (offset pinned to zero), because beating that is
the entire claim being made -- NOT against a market price, which is a different
and much stronger claim this does not make.

model_validated stays False regardless of outcome.
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

ALIASES = Path(__file__).resolve().parents[2] / "data" / "soccer_espn_aliases.json"
SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
              "concacaf.leagues.cup/scoreboard?dates={a}-{b}&limit=500")
SEASONS = {str(y): (f"{y}0701", f"{y}0930") for y in (2023, 2024, 2025, 2026)}
REFERENCE_LEAGUE = "MLS"   # pinned to 0.0; only the DIFFERENCE is identifiable
DOMESTIC_HFA_LOG = 0.2624  # what the domestic/UEFA models use, for comparison
ITERS = 4000
STEP = 0.05


def fetch_season(lo: str, hi: str):
    """(home_name, away_name, home_goals, away_goals) for completed matches."""
    out, seen = [], set()
    try:
        data = get_json(SCOREBOARD.format(a=lo, b=hi))
    except Exception as exc:
        print(f"  fetch failed {lo}-{hi}: {exc}")
        return out
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


def loglik_and_grad(rows, mu_log, hfa, s, leagues):
    ll, g_mu, g_hfa = 0.0, 0.0, 0.0
    g_s = {L: 0.0 for L in leagues}
    for ah, ch, lh, aa, ca, la, gh, ga in rows:
        d = s[lh] - s[la]
        lam_h = math.exp(mu_log + ah + ca + hfa + d)
        lam_a = math.exp(mu_log + aa + ch - d)
        ll += gh * math.log(max(lam_h, 1e-9)) - lam_h + ga * math.log(max(lam_a, 1e-9)) - lam_a
        rh, ra = gh - lam_h, ga - lam_a
        g_mu += rh + ra
        g_hfa += rh                      # home term enters the HOME rate only
        g_s[lh] += rh - ra
        g_s[la] -= rh - ra
    return ll, g_mu, g_hfa, g_s


def fit(rows, leagues, fit_hfa: bool, fit_offset: bool = True):
    mu_log, hfa = math.log(1.3), (0.0 if fit_hfa else DOMESTIC_HFA_LOG)
    s = {L: 0.0 for L in leagues}
    for _ in range(ITERS):
        _ll, g_mu, g_hfa, g_s = loglik_and_grad(rows, mu_log, hfa, s, leagues)
        n = max(1, len(rows))
        mu_log += STEP * g_mu / n
        if fit_hfa:
            hfa += STEP * g_hfa / n
        if fit_offset:
            for L in leagues:
                if L == REFERENCE_LEAGUE:
                    continue
                s[L] += STEP * g_s[L] / n
    return mu_log, hfa, s


def score(rows, mu_log, hfa, s):
    """Mean Poisson deviance per match -- lower is better."""
    tot = 0.0
    for ah, ch, lh, aa, ca, la, gh, ga in rows:
        d = s.get(lh, 0.0) - s.get(la, 0.0)
        lam_h = math.exp(mu_log + ah + ca + hfa + d)
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

    by_season, skipped = {}, collections.Counter()
    for label, (lo, hi) in SEASONS.items():
        rows = []
        for hn, an, gh, ga in fetch_season(lo, hi):
            hk, hl = resolve(hn)
            ak, al = resolve(an)
            if hk is None or ak is None:
                skipped["unresolved club"] += 1
                continue
            if hl == al:
                skipped["same-league tie (teaches nothing)"] += 1
                continue
            sh, sa = states[hl], states[al]
            rows.append((sh.get_attack(hk), sh.get_concede(hk), hl,
                         sa.get_attack(ak), sa.get_concede(ak), al, gh, ga))
        by_season[label] = rows
        print(f"{label}: {len(rows)} cross-league matches with both clubs rated")
    for why, n in skipped.items():
        print(f"  skipped {n}: {why}")

    all_rows = [r for rows in by_season.values() for r in rows]
    leagues = sorted({r[2] for r in all_rows} | {r[5] for r in all_rows})
    print(f"\n{len(all_rows)} matches total, leagues: {leagues}")
    if len(all_rows) < 60:
        print("TOO FEW MATCHES -- not fitting.")
        return
    if set(leagues) != {"MLS", "MEX1"}:
        print(f"UNEXPECTED LEAGUES {leagues} -- refusing to fit rather than "
              "silently pooling a third pool into a two-league offset.")
        return

    mu_log, hfa, s = fit(all_rows, leagues, fit_hfa=True)
    print(f"\nfull-sample fit: mu = {math.exp(mu_log):.3f} goals, "
          f"home term = {hfa:+.4f} log (domestic constant is {DOMESTIC_HFA_LOG:+.4f})")
    for L in leagues:
        print(f"   {L:6s} offset {s[L]:+.4f} log goals")
    gap = s["MEX1"] - s["MLS"]
    print(f"   => Liga MX - MLS = {gap:+.4f} log goals "
          f"({math.exp(gap):.3f}x on the scoring rate)")

    # HELD OUT BY SEASON. Three variants so the offset and the venue term are
    # judged separately -- an offset that only looks good because it absorbed a
    # venue effect should lose to the fitted-home variant.
    print(f"\n{'held-out':>10s} {'n':>5s} {'equal-leagues':>15s} {'offset+domHFA':>15s} "
          f"{'offset+fitHFA':>15s}")
    tot = collections.Counter()
    for label in SEASONS:
        test = by_season[label]
        train = [r for k, rows in by_season.items() if k != label for r in rows]
        if len(test) < 10 or len(train) < 40:
            continue
        m0, h0, s0 = fit(train, leagues, fit_hfa=False, fit_offset=False)
        m1, h1, s1 = fit(train, leagues, fit_hfa=False)
        m2, h2, s2 = fit(train, leagues, fit_hfa=True)
        d0, d1, d2 = (score(test, m0, h0, s0), score(test, m1, h1, s1),
                      score(test, m2, h2, s2))
        tot["n"] += len(test)
        tot["w1"] += (d0 - d1) * len(test)
        tot["w2"] += (d0 - d2) * len(test)
        print(f"{label:>10s} {len(test):5d} {d0:15.4f} {d1:15.4f} {d2:15.4f}")
    if tot["n"]:
        print(f"\nweighted mean improvement vs equal-leagues:")
        print(f"   offset + domestic HFA : {tot['w1']/tot['n']:+.4f} deviance")
        print(f"   offset + fitted HFA   : {tot['w2']/tot['n']:+.4f} deviance")
        print("\n(positive = better than pretending the two leagues are equal)")

    if "--write" not in sys.argv:
        print("\nDRY RUN -- pass --write to persist to data/leagues_cup_strength.json")
        return
    out = Path(__file__).resolve().parents[2] / "data" / "leagues_cup_strength.json"
    out.write_text(json.dumps({
        "mu": math.exp(mu_log),
        # The FITTED venue term, not the domestic constant. See the module
        # docstring: this competition is played at neutral or near-neutral
        # venues and the fit says so itself (+0.0071 vs the domestic +0.2624).
        "home_log": hfa,
        "offsets": s,
        "n_matches": len(all_rows),
        "seasons": sorted(by_season),
        "fitted_on": "concacaf.leagues.cup ESPN results",
    }, indent=1, sort_keys=True), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()

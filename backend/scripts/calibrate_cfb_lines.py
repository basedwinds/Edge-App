"""Fit CFB's own margin model. NFL's constants must NOT be borrowed.

WHY IT HAS TO BE MEASURED. game_lines.MARGIN_SLOPE/MARGIN_STD are NFL numbers
(0.04146 / 13.52). College football is a different sport statistically -- far
wider talent spread, so blowouts are routine -- and using NFL's spread width
would price every CFB game as far more certain than it is.

METHOD. Replay data/cfb_game_cache.json chronologically with the same Elo update
the app's own CFB service uses, so the ratings a game is predicted from are
PRE-GAME (no leakage from its own result), then regress actual margin on the
pre-game Elo difference. Slope is the points-per-Elo-point conversion; the
residual standard deviation is the spread width.

Reports out-of-sample by season -- fit on prior seasons, score the held-out one
-- because a slope fitted and scored on the same games flatters itself.
"""
import json
import statistics

# elo_cfb, NOT elo. This import was `app.models.baseline.elo` -- the NFL module --
# and that single wrong word invalidated every constant this script produced.
# elo.py runs K=20 with 1/3 season regression; elo_cfb.py runs K=100 with none,
# which is a completely different rating scale (elo_diff sd 127 against 230 on
# the same 4,836 games). The slope fitted on the narrow scale was then applied at
# runtime to the wide one, overstating every CFB margin by ~65%.
#
# That is the SAME failure the comment below already describes and claims to have
# fixed. It was half-fixed: the replay moved off a hand-rolled Elo and onto "the
# app's own primitives", but reached for the wrong sport's primitives. Proof, by
# re-running both replays on identical data: the elo.py replay reproduces the
# shipped 0.08569 exactly, and the elo_cfb replay gives 0.05194.
from app.models.baseline.elo_cfb import EloState, effective_home_field_adv, update_ratings

CACHE = "data/cfb_game_cache.json"

# CRITICAL: THE REPLAY MUST USE THE APP'S OWN ELO PRIMITIVES.
#
# The first version of this script hand-rolled its own Elo (K=20, HFA=55, 25%
# season reversion) and fitted a slope of 0.13636 on it. That number was
# unusable: elo_service_cfb builds ratings through EloState/update_ratings, and
# a direct comparison of the two scales over all 257 shared teams gave sd 60 for
# the hand-rolled replay against sd 218 for the service -- a 3.6x difference,
# regression slope 3.349. Applying a slope fitted on the narrow scale to the
# wide one overstates every margin by that factor, which is what surfaced as a
# 99.7% cover probability on a game the moneyline model put at 86%.
#
# Fitting against the app's own primitives is the only way the constant can be
# valid where it is USED. Anything else silently re-derives a different sport.
def load():
    d = json.load(open(CACHE))
    rows = [g for g in d.values()
            if g.get("home_score") is not None and g.get("away_score") is not None]
    rows.sort(key=lambda g: (g["season"], g["date"]))
    return rows


def replay(rows, home_adv=None):
    """(elo_diff_pre_game, home_margin, season, total) per game, on the SERVICE's
    rating scale -- same EloState, same update_ratings, same season handling."""
    state = EloState()
    out = []
    for g in rows:
        state.start_season_if_new(g["season"])
        h, a = g["home_abbr"], g["away_abbr"]
        adv = (effective_home_field_adv(bool(g.get("neutral")))
               if home_adv is None else (0.0 if g.get("neutral") else home_adv))
        diff = (state.get(h) + adv) - state.get(a)
        margin = g["home_score"] - g["away_score"]
        out.append((diff, margin, g["season"], g["home_score"] + g["away_score"]))
        update_ratings(state, h, a, g["home_score"], g["away_score"], adv)
    return out


def fit(pairs):
    """Least-squares slope through the origin, plus residual sd."""
    num = sum(d * m for d, m, *_ in pairs)
    den = sum(d * d for d, _m, *_ in pairs)
    slope = num / den if den else 0.0
    resid = [m - slope * d for d, m, *_ in pairs]
    return slope, statistics.pstdev(resid)


rows = load()
print(f"CFB games with final scores: {len(rows)}  seasons "
      f"{min(g['season'] for g in rows)}-{max(g['season'] for g in rows)}")

# Home advantage: pick the value that best centres the residuals.
print("\nhome-field advantage sweep (Elo points):")
best = None
for hfa in (0, 25, 40, 55, 65, 80, 100):
    pairs = replay(rows, home_adv=hfa)
    slope, sd = fit(pairs)
    bias = statistics.mean(m - slope * d for d, m, *_ in pairs)
    print(f"  hfa={hfa:4}  slope={slope:.5f}  resid sd={sd:5.2f}  mean resid={bias:+6.3f}")
    if best is None or abs(bias) < abs(best[1]):
        best = (hfa, bias, slope, sd)
print(f"  -> least-biased hfa={best[0]}")

pairs = replay(rows, home_adv=best[0])
slope, sd = fit(pairs)
print(f"\nFITTED ON ALL SEASONS:  MARGIN_SLOPE={slope:.5f}  MARGIN_STD={sd:.2f}")
print(f"  NFL for comparison:     MARGIN_SLOPE=0.04146    MARGIN_STD=13.52")

print("\nOUT-OF-SAMPLE by season (fit on the others, score the held-out one):")
seasons = sorted({s for _d, _m, s, _t in pairs})
for hold in seasons:
    tr = [p for p in pairs if p[2] != hold]
    te = [p for p in pairs if p[2] == hold]
    s_tr, _sd_tr = fit(tr)
    resid = [m - s_tr * d for d, m, *_ in te]
    print(f"  {hold}: n={len(te):4}  slope(train)={s_tr:.5f}  "
          f"held-out sd={statistics.pstdev(resid):5.2f}  mean resid={statistics.mean(resid):+6.3f}")

print("\nTOTALS (for a future totals model, not fitted here):")
totals = [t for _d, _m, _s, t in pairs]
print(f"  league mean total={statistics.mean(totals):.2f}  sd={statistics.pstdev(totals):.2f}")
print(f"  NFL for comparison: mean=44.16  naive sd=14.14")
print("  A totals MODEL needs per-team offence/defence strength, which CFB does")
print("  not have -- the league mean alone cannot price a specific matchup.")

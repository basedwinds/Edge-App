"""Per-sport probability calibration via temperature scaling in logit space:
p_cal = sigmoid(logit(p) / T). One parameter per sport, fit offline by
minimizing walk-forward log-loss and accepted ONLY when it improves BOTH ECE
and Brier out-of-sample (chronological holdout) -- see
scripts/fit_temperature.py. A single parameter is deliberate: a small/noisy
sport can't overfit it (it just lands at T~=1.0, an identity no-op).

Measured 2026-07-22 (scripts/measure_calibration.py + fit_temperature.py):
  MLB  n=22,763  ECE 0.005  -> already calibrated, T=1.0 (temp made it worse OOS)
  NBA  n=15,672  ECE 0.008  -> already calibrated, T=1.0 (temp made it worse OOS)
  NFL  n= 6,952  ECE 0.016  -> already calibrated, T=1.03~=1.0 (no OOS gain)
  CBB  n=27,039  ECE 0.010  -> already calibrated, T=1.0 (temp made it worse OOS)
  WNBA n= 1,320  ECE 0.049  -> the miscalibration is small-sample NOISE (gaps
                              have no consistent direction), T=1.0 (OOS no-op)
  CFB  n= 3,936  ECE 0.033  -> REAL systematic under-confidence (probs too timid,
                              pulled toward 0.5 by CFB's extreme talent gaps);
                              T=0.83 improves OOS ECE (+0.005) AND Brier (+0.0004)
Every large, well-tuned team Elo (MLB/NBA/NFL/CBB) came out already calibrated;
CFB is the lone real correction. Not yet measured: Tennis/Soccer/esports (their
served model_prob paths differ; add here if a future measurement warrants).
So only CFB carries a non-identity temperature today. Large, well-tuned team
Elos are already calibrated; the hook exists for the rest so a future drift (or
a newly-integrated sport like CFB) can be corrected with a single measured
constant, same "constants come from real data" discipline as the Elo K-factors.
This does NOT make any model beat the market -- it makes its probabilities
honest, which makes quarter-Kelly SIZING better (staking the right amount on a
70% that's really a 70%), nothing more.
"""
import math

# Sport key (matches Market.sport) -> fitted temperature. Absent = 1.0 (identity).
TEMPERATURE: dict[str, float] = {
    # 2026-08-03: was 0.83, which was BACKWARDS -- it sharpened an already
    # overconfident model and scored WORSE than no calibration at all on
    # held-out seasons (logloss 0.56308 vs 0.55019 raw). Refit walk-forward on
    # 3,860 games (2022-2025, 2021 dropped as Elo burn-in), trained on
    # 2022-2023 and scored on 2024-2025: T=1.26 wins on both metrics
    # (brier 0.18507 / logloss 0.54657 vs raw 0.18626 / 0.55019).
    #
    # T>1 SOFTENS. Favourite-side calibration on the held-out seasons, by Elo
    # gap, is why -- the old value made every band worse:
    #     gap        T=0.83 err    T=1.26 err
    #     0-100        +0.044        +0.016
    #     100-200      +0.050        -0.022
    #     200-300      +0.120        +0.030
    #     300-500      +0.055        -0.026
    #     500+         +0.027        -0.016
    # T=1.26's errors are small and alternate in sign; T=0.83's are uniformly
    # positive, i.e. systematically overconfident. See
    # scripts/cfb_calibration_audit.py to reproduce.
    "cfb": 1.26,
}

_EPS = 1e-6


def apply(sport: str, prob: float | None) -> float | None:
    """Calibrate a model probability for `sport`. Identity (returns prob
    unchanged) for any sport without a fitted, validated temperature."""
    if prob is None:
        return None
    t = TEMPERATURE.get(sport, 1.0)
    if t == 1.0:
        return prob
    p = min(max(prob, _EPS), 1 - _EPS)
    logit = math.log(p / (1 - p))
    return round(1.0 / (1.0 + math.exp(-logit / t)), 4)

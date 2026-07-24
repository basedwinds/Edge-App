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
    "cfb": 0.83,  # validated OOS; applied once CFB is integrated at its season
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

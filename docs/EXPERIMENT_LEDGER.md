# Experiment ledger

**Check this file BEFORE starting any model investigation.**

Append-only. One row per hypothesis that has been *tested*, with the number that
decided it. Docstrings record what a constant **is**; memory files go stale. This
records what was **tried and killed**, so a settled question stays settled.

It exists because settled questions kept reopening — on 2026-08-14 alone, three
did: a CFB correction described as "not yet wired" that had been live for days, a
racing minimum-starts gate whose CS2 equivalent had already been tested and
upheld, and per-track racing parameters that had been rejected on hold-out.

## How to add a row

Date · Area · Hypothesis · Verdict · **The decisive number** · Script.

Verdicts: **SHIPPED** · **REJECTED** · **UPHELD** (challenged, kept unchanged) ·
**INCONCLUSIVE** (test was invalid — say so, do not record a false negative) ·
**BLOCKED**.

A rejection with no number is not a rejection, it is an opinion. Record the
figure that would have to change for the answer to change.

---

## Racing

| Date | Hypothesis | Verdict | Decisive number | Script |
|---|---|---|---|---|
| 2026-08-15 | Pack-racing (Daytona/Talladega/Atlanta) needs its own top-N params | **SHIPPED** | 5/5 held-out seasons; pooled calib err 0.1057 → 0.0162; worst decile miss 17pp → ~2pp. `PACK_TOPN_PARAMS = {grid_pts 5.0, attrition 0.30}` | `check_racing_pack_racing.py` |
| 2026-08-15 | Use NASCAR's `restrictor_plate` flag as the pack-racing selector | **REJECTED** | Flag marks the RESTRICTED ENGINE PACKAGE, not pack racing. Truck: 8/50 flagged incl. **Richmond**, a 0.75-mile short track. Selector is now flag **AND** track ≥ 1.0 mi | — |
| 2026-08-15 | Minimum-starts gate for thin driver ratings | **REJECTED** | Effect is real but 1.1–1.3x, inside a known ~2.4x global overstatement; `MIN_STARTS=7` would delete **21%** of Truck inventory. Xfinity incoherent (0.80x at 2–3 starts) | `check_racing_start_counts.py` |
| 2026-08-15 | `has_qualifying` flags races whose grid was set by rulebook | **REJECTED** | Flag is False for **434/435** completed races incl. ones that demonstrably had qualifying. Field does not mean its name | — |
| 2026-08-15 | Qualifying **gap** beats ordinal grid position | **INCONCLUSIVE** | corr 0.017 vs 0.411 looks like a kill but is not: Pearson on a heavy-tailed gap vs a uniform rank collapses. Test measured skew, not signal. Redo with within-race standardisation | `#190` |
| 2026-08-07 | Per-**track** grid/attrition parameters | **REJECTED** | Physical spread real (12x grid predictiveness, 6x attrition) but ~3.7 races/track. Superseded by the per-**type** binary above | `fit_racing_track_aware.py` |
| 2026-08-07 | Refit grid_pts per series | **SHIPPED** | 90 → 30 for all NASCAR + IndyCar; four independent series land on the same interior optimum | `fit_racing_params_per_series.py` |
| 2026-08-07 | Separate top-N params from winner params | **SHIPPED** | Hold-out calib err −68% to −80% across five series | `fit_racing_joint_holdout.py` |

## CFB

| Date | Hypothesis | Verdict | Decisive number | Script |
|---|---|---|---|---|
| 2026-08-14 | CFB margin model saturates; a tanh beats the line | **REJECTED** | Tanh barely beat the line out of sample (RMSE 19.136 → 19.090, one season worse). Real cause was a wrong-Elo-scale fit | `fit_cfb_margin_curve.py` |
| 2026-08-14 | CFB margin constants were fitted on the wrong Elo scale | **SHIPPED** | Replay with `elo.py` reproduces the shipped 0.08569 exactly; with `elo_cfb` gives 0.05194. Refit: slope 0.04698, HFA 140. **Out-of-sample validation cannot catch this** — a stable fit on the wrong ruler is still stable | `calibrate_cfb_lines.py` |
| 2026-08-14 | CFB win totals are systematically ~50pp above market | **REJECTED** | mean(model − market) **−0.0198**; 199 rows ≥+10pp vs **335** ≤−10pp. Centred slightly BELOW market; the tail is a YES-only selection artifact | — |
| 2026-08-11 | G5 pool is inflated against P5 (cross-tier) | **SHIPPED** | Mirror-image bias +9.07 / −10.19 by orientation; D=+100 Elo, 4/4 held-out folds better | — |

## Cross-sport / staking

| Date | Hypothesis | Verdict | Decisive number | Script |
|---|---|---|---|---|
| 2026-08-14 | Wide books manufacture apparent edge | **CONFIRMED, no action** | Within-sport r up to +0.736 (soccer) across 14,430 priced rows — but the gates already strip it: **96%** of staked bets sit on books ≤10c. CFB is the whole residue (9 of 14) | — |
| 2026-08-15 | The NO-side profit is uniform enough to enable globally | **REJECTED — scoped instead** | Concentrated, and two cells reverse the naive read. Tennis **moneyline −0.9%** (n=363) — the LIQUID cell does not pay, exactly as #192 predicts; the +20.3% "tennis" headline is entirely DERIVED markets (set_spread +24.7 n=282, set_winner +31.5 n=231, game_total +52.8 n=101), a different model path #192 never touched. Soccer **−0.7%** (n=105) but would be **310 of 461** live NO candidates. Allowlist = the 6 cells with their own settled evidence | `check_no_side_by_cell.py` |
| 2026-08-15 | A 10x hard threshold is enough to keep implausible certainty off the board | **NO — it has an outside** | cs2 Spirit/BIG at market 0.175 / model 0.0172 = **10.2x, BLOCKED**; price drifted to 0.165 = **9.6x and the same bet returned at $10**. Unchanged model claim, admitted by a 1c move in a price it does not depend on. Threshold NOT lowered (it was set against 3,912 settled bets) — added a `near-threshold certainty` WARN at 6-10x to `board_artifact_scan.py` so the class stops depending on someone reading the board that hour | `board_artifact_scan.py` |
| 2026-08-15 | `implausible_disagreement` covers both sides | **REJECTED — gap found, fixed** | It covers the two LONGSHOT quadrants only. The FAVOURITE quadrants are open on BOTH sides: cs2 Spirit/BIG scores 0.097x and sails through while the model asserts **0.0172** for a real team in a Bo3. Live board: guard blocks **2 YES rows and 3 NO rows — the same match from both sides**, a bet the app was making today | `implausible_certainty` |
| 2026-08-14 | The NO side is unharvested and profitable | **OPEN** | Backtest **+16.0%** vs YES +15.2%; control arm ~0 both sides. **But tennis is 61% of the sample**; ex-tennis +9.1% [+0.3, +18.0] | `#186` |
| 2026-08-14 | Model probabilities of exactly 0/1 should not stake | **SHIPPED** | 277 priced rows; 0 staked at the time, so preventive. A Monte Carlo 0.0000 means "below sim resolution", not "impossible" | — |
| 2026-08-03 | Longshot price floor for futures | **REJECTED** | Check the UI that renders a gate, not just the gate | — |

## Soccer market coverage

| Date | Hypothesis | Verdict | Decisive number | Script |
|---|---|---|---|---|
| 2026-08-15 | Only moneyline soccer bets ever get recommended | **REFUTED** | 8 types stake today: moneyline_3way 29, game_total 20, team_total 8, btts 2, uefa/national variants, spread 1. Moneyline just dominates by count | — |
| 2026-08-15 | Half-markets (first_half_*/second_half_*) are wrongly suppressed | **WORKING AS DESIGNED** | Two gates, both correct. (1) **92–99% have ZERO volume** vs 51% for moneyline, so `has_traded` refuses to price against an untouched market-maker quote. (2) Of the 4 traded rows clearing 10pp, all fail the **ask** guard: `first_half_total` reads **+20.1pp at the mid and −16.9pp at the ask** on a 0.20/0.94 book | — |

**Do not "fix" half-market suppression.** It is thin inventory quoted so wide the
edge inverts at the executable price. The ask guard in `kelly_fraction`
(`execution_price`) is what catches it.

## Esports

| Date | Hypothesis | Verdict | Decisive number | Script |
|---|---|---|---|---|
| 2026-08-15 | The CS2 Elo GAP is too steep (separate axis from game count) | **SHIPPED — `GAP_SHRINK=0.80`** | Overconfident at EVERY gap above 50 Elo, CI excluding the claim each time, miss widening with gap: 50-99 −0.045, 100-149 −0.033, 150-199 −0.054, 200-299 −0.080, 300-399 −0.083; **0-49 is clean (−0.0037)**. 60% of gated predictions are above 50, so not a tail curiosity. Fitted on train Brier alone, held-out test: **brier 0.23279→0.23020, ece 0.07644→0.05163, logloss 0.66548→0.65471 — all BETTER**. Shrink the DIFFERENCE not the probability, so the clean 0-49 region is barely touched (the tennis global-temperature failure mode). Residual at 300+ is still −0.080: an improvement, not a cure; a tail-specific parameter would be fitted on ~85 rows and was NOT attempted | `fit_cs2_elo_gap_shrink.py` |
| 2026-08-12 | CS2 `MIN_GAMES=3` is too low; thin ratings manufacture edge | **UPHELD** | Opposite of the hunch: 3 games claimed .799 delivered .784 (1.05x); **50+** claimed .843 delivered .755 (1.35x). Overconfidence lives at HIGH counts | `measure_cs2_min_games_confidence.py` |
| 2026-08-15 | Esports ratings are overconfident when teams share NO common opponents | **REJECTED** | No gradient by shared-opponent count on 38,284 matches. Overstatement — CS2 **1.01x** at 0 shared vs 0.92–0.98x elsewhere (0-shared is its BEST bucket); VALORANT 0.91/0.89/0.90/0.92x; LOL 0.92/0.96/0.98/0.95x. Model stays QUIET where the graph is thin: only 35 of 3,047 disconnected CS2 matches reach 70% confidence vs 543 of 3,226 at 6+ shared | `check_esports_connectivity.py` |
| 2026-08-15 | Esports + tennis share one top-end miscalibration | **RETRACTED — premise was wrong** | Three errors, all mine: (a) directions are OPPOSITE — tennis favourites are OVER-priced (0.844→0.685), esports replica UNDER-priced (0.89–0.98x); (b) the tennis "mirror" is arithmetic — both sides of a match are logged, so p=0.1–0.2 and p=0.8–0.9 are **the same 124 matches**, not two findings; (c) the 38,284-match esports figure comes from a **plain-Elo replica**, while production blends a player-level model — valid for the connectivity question, worthless for calibration | `#193` |
| 2026-08 | LoL patch-version adjustment | **REJECTED** | Descriptive + interventional pair both null | — |
| 2026-08 | Valorant k-core tier restriction | **REJECTED** | — | — |
| 2026-08 | CS2 probability recalibration | **REJECTED** | — | — |
| 2026-08 | Per-map Elo for CS2 | **REJECTED** | Brier 0.23748 vs 0.23368. Valorant and LoL KEPT per-map | — |
| 2026-08 | Esports idle/roster decay | **REJECTED** | Only family with no season regression; CS2/Valorant null | — |

## Tennis

| Date | Hypothesis | Verdict | Decisive number | Script |
|---|---|---|---|---|
| 2026-08-15 | Tennis moneyline is mis-calibrated at the extremes | **CONFIRMED** | Near-perfect MIRROR: p=0.1-0.2 claimed 0.156 delivered **0.315** (+0.158), p=0.8-0.9 claimed 0.844 delivered **0.685** (−0.158). Both ends, equal magnitude = logistic too steep. Not a mix effect — WTA +0.154/−0.154 and ATP +0.173/−0.173 independently | `check_tennis_calibration.py` |
| 2026-08-15 | A global temperature fixes it (reuse `calibration_temp`) | **REJECTED** | T=1.53 improves Brier (0.22789→0.22610) and logloss, but **ECE gets WORSE (0.0355→0.0394)**. Module rule needs BOTH. Deciles show why: the middle is already excellent (±0.015) and softening globally wrecks it (−0.050 to +0.047) to repair thin tails. ~19% of volume is in the bad tails; a global T trades the other 81% | `fit_tennis_temperature.py` |

**Do not retry a global temperature on tennis.** Any fix must leave 0.2–0.8
untouched. A tail-only parameter was NOT attempted: ~326 rows split across both
ends is too thin to fit one without curve-fitting the noise that motivated it.

## MLB / soccer / other

| Date | Hypothesis | Verdict | Decisive number | Script |
|---|---|---|---|---|
| 2026-08-14 | K-BB% beats FIP/ERA for pitcher quality | **BLOCKED** | Evidence complete; real signal r=**0.089** (the 0.305 figure is the REDUNDANCY correlation with elo_diff). Blocked on a prior-season fallback constant (#173) | `check_mlb_pitcher_metric.py` |
| 2026-08-15 | Recent bullpen WORKLOAD predicts beyond team strength | **REJECTED** | corr(fatigue differential, residual) = **−0.0046** on n=2,212 — zero, and the WRONG SIGN. No gradient by bucket (+0.025, +0.007, +0.002, −0.055, −0.039, −0.007, +0.067); both extremes positive. Team-only screen: if it cannot beat team Elo alone it cannot beat team+pitcher | `check_mlb_bullpen_fatigue.py` |
| 2026-08-14 | MLB air density affects run scoring | **REJECTED** | Failed all three gates | `check_mlb_air_density_signal.py` |
| 2026-08-14 | xG beats goals for soccer ratings | **OPEN** | Feasibility passed: beats goals at all 3 windows in 5 of 33 leagues. Understat needs `X-Requested-With` or 404s | `#167` |
| 2026-08 | Per-league soccer home advantage | **PARTIAL** | Tilt was an ERA effect; only BRA1 shipped | — |
| 2026-08 | Soccer Dixon-Coles rho for the draw | **SHIPPED** | Full-time only; high-goal tail still open (#133) | — |

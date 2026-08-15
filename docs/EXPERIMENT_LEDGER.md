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
| 2026-08-15 | Team OFFENCE informs MLB totals if measured as a RATE, not trailing runs | **REJECTED — and it strengthens the original** | Prior-season team OPS, fitted on the RESIDUAL after park+pitchers so it cannot claim their variance: slope +2.264 runs per 1.000 OPS, **CI [−3.876, +8.392]** spans zero; a 1-sd better offensive matchup is worth **+0.061 runs**. The original rejection used CURRENT-season trailing RUNS — different time base, different unit, same verdict. Two independent metrics agreeing beats one | `fit_mlb_total_offence_term.py` |
| 2026-08-15 | Bullpen QUALITY informs MLB totals (workload already rejected) | **REJECTED — and the sign is backwards** | Prior-season combined relief ERA on the same residual: slope **−0.386** runs per 1.00 ERA, CI **[−0.8019, +0.0321]** spans zero, and negative means a WORSE bullpen predicts FEWER runs. Both halves of the bullpen idea are now dead: workload r=−0.005 (#168), quality here | — |
| 2026-08-15 | LEAGUE_AVG_TOTAL (9.0486) is stale and should be refit on recent seasons | **REJECTED — the constant BEATS every adaptive window** | Walk-forward over 2016-2026 (2020 excluded, 60-game covid season): trailing-3yr **0.2627** mean abs err, trailing-5yr 0.2880, all-prior 0.2983 — the shipped constant scores **0.1992** on 2021+. Season means swing **8.57 to 9.67**, a 1.1-run range, so the 0.09 "staleness" is far inside noise. Chasing recency would have made it worse | — |
| 2026-08-15 | How widespread is the arithmetic-mirror trap? | **ELEVEN cells, most at 100%** | tennis/moneyline, cs2/series_winner, valorant/series_winner+map_winner, lol/series_winner+map_winner, wnba/moneyline, mlb/moneyline all **100%**; tennis/set_winner 99%, mlb/total 51%, soccer/first_half_winner 12%. Every two-sided market logs both sides, so ANY over/under or yes/no "agreement" in these cells is forced. Now detected automatically instead of rediscovered | `calibration_report.py` |
| 2026-08-15 | A market_type logging ONLY the over side is a bug to fix | **NO — it is the VENUE's structure, and "fixing" it would add zero information** | Kalshi lists totals/team_totals as ONE-SIDED YES/NO contracts (the under IS the NO side of the same contract); Polymarket lists both tokens but carries no MLB team_totals. So `team_total` is over-only because Kalshi is its only source. Logging a synthetic under would be `1 - over` on the same event — **arithmetically forced, zero independent information**. Mirror-checking a Kalshi-only market is impossible IN PRINCIPLE, not merely unimplemented | `calibration_report.py` paired-row guard |
| 2026-08-15 | The per-game cap's HIGHEST-EDGE rule costs money | **NOT DEMONSTRATED — do not change it** | 558 contested games (2+ eligible rows). PAIRED bootstrap vs the current rule: moneyline-first **+3.7% [−2.1,+9.6]**, lowest-edge +1.7% [−10.2,+13.6], random **−5.1%** [−13.4,+3.2]. No CI excludes 0. The mechanism is real (cap picks derived markets in 28% of contested games; those return +2.0% vs +8.5% for moneyline-like) but a real mechanism is not a measurable loss | `measure_per_game_cap_rule.py` |
| 2026-08-15 | Comparing selection rules as INDEPENDENT arms is valid | **NO — it flipped a sign** | Unpaired, `random` reads +10.2% vs highest-edge +6.7%, an apparent +3.5pp win. PAIRED on the same games it is **−5.1%**. Arms sharing games must be compared within-game; ROI on 5c longshots is heavy-tailed enough that two noisy arms differ by several points from sampling alone. Each arm's own CI is ~30pp wide against ~3pp differences | `measure_per_game_cap_rule.py` |
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
| 2026-08-15 | The CS2 Elo-gap shrink transfers to LoL and Valorant | **SPLIT — Valorant SHIPPED 0.86, LoL REJECTED** | Harness verified identical to production at lam=1 on all 5,604 LoL / 19,644 Valorant matches. **Valorant** mirrors CS2 (significant from 50 Elo up, −0.086 at 300+); fitted 0.86, OOS brier 0.22537→0.22340, ece 0.05201→0.03467, logloss 0.64945→0.64099, all BETTER. **LoL** chose **lam=1.00 on its own train Brier** (0.20212 vs 0.20321 at 0.80) — defect confined to 200+ (16.9% of rows), everything below inside its CI, 0-49 mildly UNDER-confident. Three titles, three constants: 0.80 / 0.86 / none | `fit_esports_elo_gap_shrink.py` |
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
| 2026-08-15 | Ship K-BB% on the MLB moneyline pitcher blend | **SHIPPED — preferred, ERA as fallback** | Harness reproduces the incumbent as a CONTROL: pooled raw-units logistic gives ERA **9.7862 vs the shipped 9.73** (0.6% miss), so the K-BB number from it is comparable. Raw 350.39, scaled off the incumbent to 348.38, then **shrunk 0.9 → 313.54** because at full strength the 40-pt cap binds on **16.8% of games vs ERA's 3.9%** — a cap documented as a backstop, not a routine clamp. Held-out 3 seasons: elo-only 0.68428, +era 0.68395, **+kbb×0.9 0.68362**. K-BB roughly DOUBLES the pitcher term's contribution, but honestly ~0.0003 log-loss | `derive_mlb_pitcher_kbb_constant.py` |
| 2026-08-15 | A pure K-BB swap is safe | **NO — it would LOSE coverage** | A K-BB rate wants batters faced to stabilise (MIN_BF_FOR_KBB=150 ≈ 35 IP) against ERA's MIN_IP=15. A pure swap would drop every starter between those thresholds to NO adjustment at all — trading a validated ERA signal for nothing on exactly the pitchers the model knows least about. K-BB gated on MIN_IP (the population the constant was fitted on), ERA carries the rest | — |
| 2026-08-14 | K-BB% beats FIP/ERA for pitcher quality | **BLOCKED** | Evidence complete; real signal r=**0.089** (the 0.305 figure is the REDUNDANCY correlation with elo_diff). Blocked on a prior-season fallback constant (#173) | `check_mlb_pitcher_metric.py` |
| 2026-08-15 | Starting pitchers move MLB game TOTALS (the module's own open question) | **SHIPPED — shrunk** | ERA had said no (r=0.069) and the docstring flagged ERA as the noisiest proxy. K-BB% says YES: **−8.566 runs per unit, CI [−13.251, −3.936]**, −0.31 runs per sd. Prior-season metric so no lookahead is possible. Fires on 41% of live games; falls back to prior pricing otherwise | `fit_mlb_total_pitcher_term.py` |
| 2026-08-15 | Ship the raw fitted pitcher slope | **NO — it overshoots 1.98x** | Held-out tercile spread: actual 0.325 runs, raw slope predicts 0.642. Shrink 0.4 chosen by CV **inside train** (2023-24 fit → 2025 validate; minimum 0.3619 vs 0.4299 no-term, 0.3790 full). Per-matchup err **0.115 flat → 0.051 shrunk**, full slope 0.140 — WORSE than flat | `PITCHER_KBB_SLOPE=-3.4264` |
| 2026-08-15 | Averaged P(over) across lines is the right metric for a totals term | **NO — errors CANCEL** | Aggregate reads 0.0069 flat / 0.0036 full / 0.0055 shrunk, preferring the arm that overshoots 1.98x, because flat under-prices aces and over-prices bad matchups and those cancel. Same trap as restrictor-plate racing (mean gap ±0.000 hiding 10x decile error). Individual games get bet, not the average | — |
| 2026-08-15 | MLB over-predicts run scoring because LEAGUE_AVG_TOTAL is stale | **NO — it is the DISTRIBUTION SHAPE** | *(CORRECTION 2026-08-15: I cited the over/under MIRROR as proof this was real. It is NOT evidence — 201/201 under rows share a (game,line) with an over row and all 201 have model probs summing to 1.000, so the under is arithmetically `1 - over` on the same event. Same artifact as the #193 retraction, repeated. The conclusion is UNCHANGED because it never rested on the log: the load-bearing evidence is the empirical distribution of 1,839 real games and the held-out season.)* Constant is 9.0486 vs a real 8.958, worth <1pp; the live miss was 13-15pp. Totals are right-skewed counts (median a full run BELOW mean every season, skew +0.64/+0.73/+0.82/+0.74 across 2023-26) priced with a symmetric NORMAL. Given the CORRECT mean the Normal still overstates P(over) by +0.060 at 7.5, +0.054 at 8.5/9.5 | `fit_mlb_total_distribution.py` |
| 2026-08-15 | A negative binomial beats the Normal for MLB totals | **SHIPPED — both game and team totals** | Fitted 2023-25 (n=7,290 / 14,580 team-games), validated on 2026 never used to fit. Game totals mean abs err **0.0430 → 0.0054, NegBin wins 6/6 lines**; team totals **0.0534 → 0.0089, 5/5**. Normal errors all POSITIVE (systematic), NegBin's mixed-sign within ±0.014. NOT Poisson: variance ~20.2 vs mean ~9.0 is heavily overdispersed | `TOTAL_NB_DISPERSION=7.1376`, `TEAM_TOTAL_NB_DISPERSION=3.5593` |
| 2026-08-15 | Recent bullpen WORKLOAD predicts beyond team strength | **REJECTED** | corr(fatigue differential, residual) = **−0.0046** on n=2,212 — zero, and the WRONG SIGN. No gradient by bucket (+0.025, +0.007, +0.002, −0.055, −0.039, −0.007, +0.067); both extremes positive. Team-only screen: if it cannot beat team Elo alone it cannot beat team+pitcher | `check_mlb_bullpen_fatigue.py` |
| 2026-08-14 | MLB air density affects run scoring | **REJECTED** | Failed all three gates | `check_mlb_air_density_signal.py` |
| 2026-08-15 | The soccer model UNDER-prices the high-goal tail (Poisson overdispersion) | **BACKWARDS — it OVER-prices it** | 21,589 big-5 matches, production replay: P(>2.5) model 53.03 vs actual 52.46 (**+0.57pp**), P(>3.5) +1.09, P(>4.5) **+1.20**, P(>5.5) +0.78, P(>6.5) +0.49 — every threshold the SAME way, ~4.8 SE at the peak. Per-total shape (too much at 0 AND 5-9+, too little at 1 and 3) says the model is OVER-dispersed, a rating-spread problem not a distribution-family one | `check_soccer_goal_tail.py` |
| 2026-08-15 | An expected-goals shrink fixes the soccer tail | **DO NOT SHIP — the train objective is FLAT** | Shrinking the home/away SPLIT does literally nothing (0.55601→0.55600) since it preserves the total. Shrinking the TOTAL is the right axis but train separates production from its own optimum by **0.00002** — noise, not a basin. Test-best is 0.80 (0.55559→0.55513) and taking it would be selecting on the held-out set, the move refused in #196/#199. Defect quantified and OPEN, not closed | — |
| 2026-08-15 | Swap soccer ratings from goals to xG | **REJECTED — pure xG is WORSE** | Train logloss 0.99423 vs production's 0.99205. Beating goals at predicting GOALS did not survive the trip through the Dixon-Coles grid into an OUTCOME probability | `fit_soccer_xg_ratings.py` |
| 2026-08-15 | A goals/xG BLEND beats either alone | **VALIDATED, not yet wired** | Interior optimum at **w=0.50** on train (0.99086 vs 0.99089 at 0.25 and 0.99197 at 0.75 — a real basin, not a boundary). Held out: logloss **0.99235 → 0.98934**, brier **0.59168 → 0.58936**, both better, **4/5 leagues** (I1 +0.00047, negligible). Makes sense mechanically: xG is less noisy, goals carry real finishing info xG discards | `fit_soccer_xg_ratings.py` |
| 2026-08-15 | The raw null-model share measures board warmth | **NO — it conflates three things** | 6,729 nulls of 23,254 (29%, over the 24% ceiling) on a HEALTHY board: **1,786 transient** ("simulation not warm yet", all CFB, rebuilt every restart), **~4,131 structural** (no match history / preseason / non-FBS — these NEVER resolve and are correct), **812 unexplained** (3.5%). Only the third can reveal a sport silently losing its model, so only it aborts. The guard was blocking the daily scan after every restart — exactly when verification matters most | `board_artifact_scan.py` |
| 2026-08-15 | The xG cache builder can be reused for the ongoing refresh | **NO — it RESUMES by design** | `build_soccer_xg_cache.py` skips seasons it already has (`if key in cache[code]: continue`). Correct for backfilling 12 seasons, exactly wrong for a refresh: the CURRENT season is already cached and grows weekly, so a resuming crawl would never fetch another match. Separate force-refetch of the current season only — 5 requests, not 60 | `soccer_xg_refresh.py` |
| 2026-08-15 | The Understat name join can be built from scorelines alone | **SHIPPED — 168/168 aliases, 99.3% fixture-verified** | Key is (league, date, scoreline) — entirely NAME-FREE. Verified at FIXTURE level, not by reading the list: 21,433/21,589 reconcile with an identical score. Of 156 that do not, **152 are the same fixture filed 1-2 days apart** (hence DATE_SLACK=2), 2 are genuine source disagreements over AWARDED results (D1 2024-12-14 Union Berlin 1-1 vs app 0-2; I1 2016-08-28 Sassuolo), 2 absent. One collision and it is CORRECT: Parma + Parma Calcio 1913 -> Parma | `build_understat_alias_map.py` |
| 2026-08-15 | Wiring xG live is cheap | **NO — 36% name match** | Only 61 of 168 canonicalised Understat team keys match the app's; misses include current clubs (borussia dortmund, bayer leverkusen, eintracht frankfurt). Needs a date-aligned fixture alias map, the same build ESPN↔football-data (#100) required, plus ongoing Understat ingestion. Gain is 0.30% logloss on 5 of 33 leagues — scope accordingly | `#202` |
| 2026-08 | Per-league soccer home advantage | **PARTIAL** | Tilt was an ERA effect; only BRA1 shipped | — |
| 2026-08 | Soccer Dixon-Coles rho for the draw | **SHIPPED** | Full-time only; high-goal tail still open (#133) | — |

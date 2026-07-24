"""Free, rule-based NBA injury adjustment -- parallel to injury_rules.py
(NFL), reusing the exact same NewsAdjustment/Factor schema (already
sport-agnostic).

Position granularity is coarser than NFL's (confirmed live 2026-07-16: only
G/F/C, no PG/SG/SF/PF split) and only two status values are in use this far
before the season ("Out"/"Day-To-Day") -- more of NFL's richer vocabulary
may appear once games start counting for real; STATUS_RULES below includes
reasonable guesses for those (Doubtful/Questionable/Injured Reserve), NOT
yet confirmed live, same "flagged as rough" treatment as this app's other
unvalidated constants.

PLAYER-VALUE PROXY (2026-07-17): replaces the original flat-weight-only
version. Scoped deliberately narrow -- ESPN's per-athlete stats endpoint
(espn_nba_client.py::fetch_player_season_avg_points) is one network call
PER PLAYER, too expensive to pre-fetch league-wide, so the poller only ever
calls it for players who actually show up on that day's injury report (a
small, naturally-bounded set). Uses the player's most recent season's PPG as
a severity multiplier -- rough, round-number tiers, not fitted (no free
historical injury-outcome dataset exists to fit against, same "auditable,
not precisely estimated" status as NFL's POSITION_WEIGHTS_PP). Unknown PPG
(rookie with no NBA games yet, request failure) keeps the ORIGINAL flat
weight unchanged -- not scaled up or down -- same "unknown = current
default, don't guess in either direction" convention as everywhere else in
this app when a refinement isn't available for a specific case.
"""
from app.models.news_adjustment.schema import Factor, NewsAdjustment, clamp_adjustment

STATUS_RULES = {
    "Out": {"severity": 1.0, "confidence": "high"},
    "Injured Reserve": {"severity": 1.0, "confidence": "high"},  # not yet confirmed live, guessed by analogy to NFL
    "Suspension": {"severity": 1.0, "confidence": "high"},  # not yet confirmed live
    "Doubtful": {"severity": 0.6, "confidence": "medium"},  # not yet confirmed live
    "Day-To-Day": {"severity": 0.35, "confidence": "low"},  # confirmed live 2026-07-16
    "Questionable": {"severity": 0.25, "confidence": "low"},  # not yet confirmed live
}

# Base per-position weight (percentage points, at full severity, BEFORE the
# player-value multiplier below) -- NBA rosters are much smaller (12-15
# active players vs. NFL's 53), so any one starter is a bigger fraction of
# team value than a single NFL non-QB; still a rough number, not fitted.
# CALIBRATED 2026-07-22 against real data (the "free historical injury-outcome
# dataset" earlier docstrings said didn't exist -- it does: ESPN box scores +
# game outcomes, see scripts/build_nba_boxscore_probe.py). On 2024+2025, the
# clean measured effect of a team's current top-3-minutes player being OUT
# (opponent full) is -4.2pp per single player (n=179) and -9.6pp for 2+ out
# (n=80). The original guesses (1.5pp base / star 2.4pp, 4.5pp cap) under-shot
# both, most glaringly the cap (4.5 vs a real 9.6). Bumped so an average
# star/starter (1.0-1.6 tier) lands near the measured 4.2pp and multi-injury
# games can reach the measured ~9.6pp. HONEST CAVEAT: n is modest and the
# per-player SE is wide (~3.7pp), so this is a moderate, deliberately
# conservative move toward the point estimate, not a precise fit; and it makes
# the model MATCH the market (which prices injuries instantly), not beat it.
POSITION_WEIGHT_PP = 3.0
MAX_TOTAL_PP = 10.0

# PPG tiers -> multiplier on POSITION_WEIGHT_PP. Rough round-number cutoffs
# (18/10/5 PPG) -- the RELATIVE tier STRUCTURE is still hand-picked (a star
# should matter more than a role player); only the base magnitude + cap above
# are now data-calibrated.
_PPG_TIERS = [
    (18.0, 1.6),  # star-level scorer
    (10.0, 1.0),  # solid rotation piece -- unchanged from the original flat weight
    (5.0, 0.5),   # bench/limited role
]
_BELOW_LOWEST_TIER_MULTIPLIER = 0.3  # under 5 PPG -- fringe roster player

# Genuinely UNRESOLVED statuses -- a real game-time decision still pending,
# not yet a settled fact. "Out"/"Injured Reserve"/"Suspension" are
# already-KNOWN outcomes (fully priced into the adjustment above) -- nothing
# left to wait for on those. Same distinction as injury_rules.py's (NFL)
# identical constant.
UNRESOLVED_STATUSES = {"Questionable", "Doubtful", "Day-To-Day"}


def _severity_multiplier(ppg: float | None) -> float:
    if ppg is None:
        return 1.0  # unknown = keep the original flat weight, don't guess in either direction
    for threshold, multiplier in _PPG_TIERS:
        if ppg >= threshold:
            return multiplier
    return _BELOW_LOWEST_TIER_MULTIPLIER


def compute_injury_adjustment(
    home_injuries: list[dict],
    away_injuries: list[dict],
    player_ppg: dict[str, float] | None = None,
) -> NewsAdjustment | None:
    """home_injuries/away_injuries: lists of {player_name, position, status,
    athlete_id} from espn_nba_client.fetch_all_injuries(). player_ppg: {
    player_name: avg_points} for whichever injured players the poller
    successfully fetched season stats for (optional -- degrades to the flat
    weight for anyone missing)."""
    player_ppg = player_ppg or {}
    factors: list[Factor] = []
    home_pp = away_pp = 0.0
    requires_review = False

    for injuries, side in ((home_injuries, "home"), (away_injuries, "away")):
        side_total = 0.0
        for inj in injuries:
            rule = STATUS_RULES.get(inj.get("status", ""))
            if rule is None:
                continue
            ppg = player_ppg.get(inj.get("player_name", ""))
            multiplier = _severity_multiplier(ppg)
            pp = POSITION_WEIGHT_PP * multiplier * rule["severity"]
            side_total += pp
            ppg_note = f", {ppg:.1f} PPG last season" if ppg is not None else ""
            factors.append(
                Factor(
                    factor=f"{inj.get('player_name', 'Unknown')} ({inj.get('position', '?')}) listed as {inj.get('status')}{ppg_note}",
                    direction="favor_away" if side == "home" else "favor_home",
                    weight="minor" if pp < 1.0 else ("moderate" if pp < 2.5 else "major"),
                    rationale=f"{POSITION_WEIGHT_PP}pp base position weight x {multiplier:.1f} player-value multiplier x {rule['severity']} severity for {inj.get('status')}.",
                )
            )
            # Real, previously-missing signal (fixed 2026-07-17, was hardcoded
            # False): a rotation-or-better player (pp >= 1.0, same threshold
            # this function already uses for "moderate" weight) whose status
            # is still genuinely undecided is exactly the "wait for the
            # official inactive list/lineup" case -- a confirmed Out doesn't
            # need review, it's already fully priced in above.
            if pp >= 1.0 and inj.get("status") in UNRESOLVED_STATUSES:
                requires_review = True
        if side == "home":
            home_pp = min(side_total, MAX_TOTAL_PP)
        else:
            away_pp = min(side_total, MAX_TOTAL_PP)

    if not factors:
        return None

    net_pp = clamp_adjustment(away_pp - home_pp)  # away injuries help home team, and vice versa
    confidence = "high" if max(home_pp, away_pp) >= 2.5 else ("medium" if max(home_pp, away_pp) >= 1.0 else "low")
    return NewsAdjustment(adjustment_pct=net_pp, confidence=confidence, factors=factors, requires_review=requires_review)

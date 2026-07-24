"""Free, rule-based replacement for the paid Claude API research step.

Uses ESPN's free injury-report endpoint (app/clients/espn_client.py), which
already lists every notable injury for all 32 teams (position abbreviations
and status vocabulary confirmed live 2026-07-14: WR/RB/TE/QB/LB/CB/DT/OT/S/
DE/PK/G/C/FB/DB/LS/P and Active/Questionable/Doubtful/Injured Reserve/Out/
Suspension). QB is handled with real starter-matching (nflverse publishes the
actual starting QB name per game in games.csv). Every other position is
weighted by typical win-probability impact and summed, then discounted unless
confirmed as an actual starter via nflverse's free depth-chart feed
(app/clients/depth_chart_client.py, pos_rank == "1") -- passing
starters_by_team is optional so this still degrades gracefully (full weight,
same as before) if depth-chart data isn't available for some reason.

Kept as a rule-based system rather than an LLM call: no API cost, fully
deterministic, and every adjustment magnitude below is a plain, auditable
number rather than a black box.
"""
from app.models.news_adjustment.schema import Factor, NewsAdjustment, clamp_adjustment
from app.models.qb_ratings import backup_quality_multiplier, lookup_backup_stats

# severity (0-1, scales the adjustment) and confidence per ESPN status.
# Anything not listed here (most commonly "Active") is treated as no injury impact.
STATUS_RULES = {
    "Out": {"severity": 1.0, "confidence": "high"},
    "Injured Reserve": {"severity": 1.0, "confidence": "high"},
    "Suspension": {"severity": 1.0, "confidence": "high"},
    "Physically Unable to Perform": {"severity": 0.8, "confidence": "medium"},
    "Doubtful": {"severity": 0.6, "confidence": "medium"},
    "Questionable": {"severity": 0.25, "confidence": "low"},
}

# a starting QB fully "Out" moves this far. VALIDATED 2026-07-22 against real
# data (no scrape needed -- NflGame carries the actual starting QB per game):
# on 1,671 real backup-QB-starting games (1999-2025, established starter out,
# opponent on its normal starter), the team wins ~6.5pp less than pure team
# Elo (~5pp relative to the ~-1.5pp both-normal baseline) -- see
# scripts/calibrate_nfl_qb_injury.py. So this originally-guessed 6.0 is
# confirmed well-centered in the measured 5-6.5pp range; kept unchanged, now
# data-validated rather than guessed. (Contrast NBA, where the same method
# found injury_rules_nba's guess was ~2x too small and recalibrated it.)
QB_MAX_ADJUSTMENT_PP = 6.0  # within schema's +/-7pp cap

# Genuinely UNRESOLVED statuses -- a real game-time decision still pending,
# not yet a settled fact. "Out"/"Injured Reserve"/"Suspension"/"Physically
# Unable to Perform" are already-KNOWN outcomes (fully priced into the
# adjustment above) -- there's nothing left to wait for on those. Used for
# the "should I wait to bet" signal, distinct from the injury MAGNITUDE
# calculation above (which correctly uses every status, resolved or not).
UNRESOLVED_STATUSES = {"Questionable", "Doubtful"}

# Rough, publicly-reasonable per-position win-probability weight (percentage
# points at full severity) for a non-QB injury, IF the player is a confirmed
# starter. Summed (then capped) rather than trusted individually.
POSITION_WEIGHTS_PP = {
    "RB": 1.2,
    "WR": 1.0,
    "OT": 0.9,
    "DE": 0.8,
    "CB": 0.7,
    "TE": 0.6,
    "LB": 0.5,
    "DT": 0.5,
    "S": 0.4,
    "DB": 0.4,
    "C": 0.4,
    "G": 0.3,
    "PK": 0.3,
    "FB": 0.15,
    "P": 0.1,
    "LS": 0.05,
}
NON_QB_MAX_TOTAL_PP = 3.0  # cap so several minor bench injuries can't dwarf QB-level signal
NON_STARTER_DISCOUNT = 0.25  # weight applied when depth-chart data says this player isn't pos_rank 1

# Losing 2+ starters at the SAME position group at once depletes replacement-
# level depth faster than a linear sum of individual injuries -- e.g. a
# team's 3rd-string tackle is much worse than its 2nd-string one. Applied as
# a separate additive bonus OUTSIDE the linear NON_QB_MAX_TOTAL_PP cap above
# (deliberately -- otherwise a real depth crisis would just get silently
# absorbed by that cap), with its own small ceiling; the overall +/-7pp
# schema-wide clamp (schema.ADJUSTMENT_CAP_PP) is still the final backstop.
POSITION_GROUPS = {
    "offensive line": {"OT", "G", "C"},
    "wide receiver": {"WR"},
    "secondary": {"CB", "S", "DB"},
    "defensive line": {"DE", "DT"},
    "linebacker": {"LB"},
}
CLUSTER_MIN_COUNT = 2
CLUSTER_BONUS_PP = 1.0
CLUSTER_MAX_GROUPS = 2  # at most this many simultaneous group-clusters add a bonus, for a readable factor list

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _normalize_name(name: str) -> str:
    return name.lower().replace(".", "").strip()


def _bump_confidence(current: str, candidate: str) -> str:
    return candidate if CONFIDENCE_RANK[candidate] > CONFIDENCE_RANK[current] else current


def find_qb_status(team_injuries: list[dict], qb_name: str) -> dict | None:
    if not qb_name:
        return None
    target = _normalize_name(qb_name)
    for inj in team_injuries:
        if inj.get("position") != "QB":
            continue
        candidate = _normalize_name(inj.get("player_name", ""))
        if candidate == target or target in candidate or candidate in target:
            return inj
    return None


def _weight_label(severity: float) -> str:
    if severity >= 0.6:
        return "major"
    if severity >= 0.25:
        return "moderate"
    return "minor"


def _is_confirmed_starter(player_name: str, team_starters: set[str] | None) -> bool | None:
    """Returns True/False if depth-chart data is available for this team,
    None if we don't have depth-chart data at all (caller falls back to full
    weight rather than penalizing every injury for a data gap)."""
    if team_starters is None:
        return None
    return _normalize_name(player_name) in team_starters


def _position_group(pos: str) -> str | None:
    for group_name, positions in POSITION_GROUPS.items():
        if pos in positions:
            return group_name
    return None


def _team_contribution(
    team_injuries: list[dict],
    qb_name: str | None,
    team_starters: set[str] | None,
    backup_qb_name: str | None = None,
    qb_career_stats: dict | None = None,
) -> tuple[float, str, list[Factor], bool]:
    """Returns (total_pp, confidence, factors, requires_review) for ONE team,
    all relative to that team (negative = bad for that team)."""
    total = 0.0
    confidence = "low"
    factors: list[Factor] = []
    requires_review = False
    qb_hit = find_qb_status(team_injuries, qb_name or "")

    if qb_hit and qb_hit["status"] in STATUS_RULES:
        rule = STATUS_RULES[qb_hit["status"]]
        backup_stats = lookup_backup_stats(backup_qb_name, qb_career_stats or {})
        multiplier, backup_note = backup_quality_multiplier(backup_stats)
        total -= rule["severity"] * QB_MAX_ADJUSTMENT_PP * multiplier  # bad for this team
        confidence = _bump_confidence(confidence, rule["confidence"])
        # REAL BUG fixed here (2026-07-17): this used to be `severity >= 0.6`,
        # which INCLUDED "Out"/"Injured Reserve"/"Suspension" (severity 1.0,
        # already-settled facts fully priced into the adjustment above) and
        # EXCLUDED "Questionable" (severity 0.25) -- backwards from this
        # schema field's own documented intent ("e.g. starting QB
        # questionable", see schema.py). A recommended bet on a confirmed-Out
        # QB has nothing left to wait for; a Questionable/Doubtful QB is
        # exactly the "check back before betting" case. Fixed to key off
        # genuine unresolved-ness, not raw severity.
        requires_review = qb_hit["status"] in UNRESOLVED_STATUSES
        backup_desc = f"backup {backup_qb_name}: {backup_note}" if backup_qb_name else "no backup QB listed on depth chart"
        factors.append(
            Factor(
                factor=f"Starting QB {qb_hit['player_name']} listed as {qb_hit['status']} ({backup_desc})",
                direction="neutral",  # caller flips to favor_home/favor_away
                weight="major" if multiplier >= 0.75 else "moderate",
                rationale="ESPN injury report, matched against nflverse's listed starting QB for this game; "
                "penalty scaled by the depth-chart backup's career EPA/dropback and start count",
            )
        )

    non_qb_total = 0.0
    group_counts: dict[str, int] = {}
    for inj in team_injuries:
        pos = inj.get("position")
        status = inj.get("status")
        if pos == "QB" or pos not in POSITION_WEIGHTS_PP or status not in STATUS_RULES:
            continue
        rule = STATUS_RULES[status]
        is_starter = _is_confirmed_starter(inj.get("player_name", ""), team_starters)
        discount = 1.0 if is_starter is not False else NON_STARTER_DISCOUNT
        contribution = rule["severity"] * POSITION_WEIGHTS_PP[pos] * discount
        if contribution <= 0:
            continue
        non_qb_total += contribution
        confidence = _bump_confidence(confidence, rule["confidence"])
        starter_note = "confirmed starter" if is_starter else ("depth player" if is_starter is False else "starter unconfirmed")
        factors.append(
            Factor(
                factor=f"{inj['player_name']} ({pos}) listed as {status}",
                direction="neutral",
                weight=_weight_label(rule["severity"] * discount),
                rationale=f"ESPN injury report, {starter_note} per nflverse depth chart",
            )
        )
        # Only a real-or-unconfirmed starter counts toward a "depth
        # depleted" cluster -- a confirmed depth player being hurt doesn't
        # deplete the position group the way losing an actual starter does.
        if is_starter is not False:
            group_name = _position_group(pos)
            if group_name:
                group_counts[group_name] = group_counts.get(group_name, 0) + 1

    non_qb_total = min(non_qb_total, NON_QB_MAX_TOTAL_PP)
    total -= non_qb_total

    clustered = sorted(
        ((g, c) for g, c in group_counts.items() if c >= CLUSTER_MIN_COUNT), key=lambda gc: -gc[1]
    )[:CLUSTER_MAX_GROUPS]
    for group_name, count in clustered:
        total -= CLUSTER_BONUS_PP
        confidence = _bump_confidence(confidence, "medium")
        factors.append(
            Factor(
                factor=f"{count} {group_name} starters injured simultaneously",
                direction="neutral",
                weight="moderate",
                rationale="Position-group injury clustering: replacement-level depth degrades faster than a "
                "linear sum of individual injuries once 2+ starters at the same group are out at once -- "
                "speculative magnitude, not independently backtested",
            )
        )

    return total, confidence, factors, requires_review


def offense_scoring_penalty_pp(
    team_injuries: list[dict],
    qb_name: str | None,
    team_starters: set[str] | None,
    backup_qb_name: str | None = None,
    qb_career_stats: dict | None = None,
) -> float:
    """Non-negative pp magnitude for the TWO injury signals that affect this
    team's own scoring OUTPUT directly (backup-QB quality, position-group
    injury clustering) -- as opposed to the full _team_contribution() total,
    which also includes individual non-QB injuries that are more about
    competitive edge/win probability than raw scoring volume (see Round 4 of
    this project's memory: "backup-QB quality/injury-clustering ... affect
    scoring VOLUME directly, unlike trap-game/coach-changes/road-trip-fatigue
    which are really about competitive edge, not points" -- deliberately
    narrow to just these two, not every injury). Deliberately a SEPARATE
    function from _team_contribution rather than a refactor of it, so the
    already-tested moneyline injury blend is untouched by this addition.
    Used to feed game_lines-space totals models (see markets.py), not the
    moneyline win-probability blend."""
    penalty = 0.0
    qb_hit = find_qb_status(team_injuries, qb_name or "")
    if qb_hit and qb_hit["status"] in STATUS_RULES:
        rule = STATUS_RULES[qb_hit["status"]]
        backup_stats = lookup_backup_stats(backup_qb_name, qb_career_stats or {})
        multiplier, _ = backup_quality_multiplier(backup_stats)
        penalty += rule["severity"] * QB_MAX_ADJUSTMENT_PP * multiplier

    group_counts: dict[str, int] = {}
    for inj in team_injuries:
        pos = inj.get("position")
        status = inj.get("status")
        if pos == "QB" or pos not in POSITION_WEIGHTS_PP or status not in STATUS_RULES:
            continue
        is_starter = _is_confirmed_starter(inj.get("player_name", ""), team_starters)
        if is_starter is not False:
            group_name = _position_group(pos)
            if group_name:
                group_counts[group_name] = group_counts.get(group_name, 0) + 1

    clustered_count = sum(1 for _, c in group_counts.items() if c >= CLUSTER_MIN_COUNT)
    penalty += min(clustered_count, CLUSTER_MAX_GROUPS) * CLUSTER_BONUS_PP

    return penalty


def compute_injury_adjustment(
    away_qb_name: str | None,
    home_qb_name: str | None,
    away_injuries: list[dict],
    home_injuries: list[dict],
    away_starters: set[str] | None = None,
    home_starters: set[str] | None = None,
    away_backup_qb: str | None = None,
    home_backup_qb: str | None = None,
    qb_career_stats: dict | None = None,
) -> NewsAdjustment:
    away_total, away_conf, away_factors, away_review = _team_contribution(
        away_injuries, away_qb_name, away_starters, away_backup_qb, qb_career_stats
    )
    home_total, home_conf, home_factors, home_review = _team_contribution(
        home_injuries, home_qb_name, home_starters, home_backup_qb, qb_career_stats
    )

    for f in away_factors:
        f.direction = "favor_home"  # bad for away = good for home
    for f in home_factors:
        f.direction = "favor_away"  # bad for home = good for away

    # away_total is already negative-for-away; flipping sign gives the
    # home-team-perspective contribution from away's injuries.
    adjustment_pct = -away_total + home_total

    confidence = away_conf if CONFIDENCE_RANK[away_conf] >= CONFIDENCE_RANK[home_conf] else home_conf

    return NewsAdjustment(
        adjustment_pct=clamp_adjustment(adjustment_pct),
        confidence=confidence,
        factors=away_factors + home_factors,
        requires_review=away_review or home_review,
    )

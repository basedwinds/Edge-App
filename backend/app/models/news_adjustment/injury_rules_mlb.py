"""Free, rule-based MLB injury adjustment -- parallel to injury_rules_nba.py,
reusing the same NewsAdjustment/Factor schema.

Deliberately scoped to POSITION PLAYERS only (SP/RP excluded) -- unlike
NFL/NBA, a starting pitcher going down is already captured structurally by
this app's baseline model itself: MlbGame.home_probable_pitcher_id/
away_probable_pitcher_id are refreshed live every poll cycle direct from MLB
Stats API, so the moment a team's probable starter changes, elo_service_mlb's
pitcher blend already reflects the new (or missing) starter on the very next
cycle -- building a SEPARATE "starting pitcher injured" situational signal
would double-count the same information the baseline already prices in.
Relief-pitcher injuries are a real, structurally different gap (not captured
anywhere else in this app) but scoped out of this first version -- smaller,
more speculative signal, left for a future round.

Real status vocabulary confirmed live via espn_mlb_client.py (case
inconsistent -- "Suspension"/"suspension", "Bereavement"/"bereavement" both
seen -- matched case-insensitively below): Out, 60/15/10-Day-IL, "7-Day IL"
(space not hyphen), Suspension, Bereavement, Day-To-Day.

PLAYER-VALUE PROXY: season OPS, from a bulk MLB Stats API call (ALL batters
in one request -- see statsapi_mlb_client.py::get_season_hitting_stats,
unlike NBA's per-athlete PPG calls which had to be scoped to just the
injured set for cost reasons). Tier cutoffs are REAL derived percentiles
from this app's own live pull (2026-07-17, n=383 batters with >=100 PA this
season), not guessed round numbers the way NBA's PPG tiers were: p90=0.843,
p50=0.708 (~league average), p10=0.587. MIN_PA=100 matches the percentile
derivation -- below that, OPS is too small-sample to trust (a 4-PA sample
can show a 3.250 "OPS", confirmed live), falls back to the unscaled flat
weight, same "unknown = current default, don't guess" convention as NBA's
version.
"""
import datetime

from app.clients import statsapi_mlb_client
from app.models.news_adjustment.schema import Factor, NewsAdjustment, clamp_adjustment

_PITCHER_POSITIONS = {"SP", "RP", "P"}

STATUS_RULES = {
    "out": {"severity": 1.0, "confidence": "high"},
    "60-day-il": {"severity": 1.0, "confidence": "high"},
    "15-day-il": {"severity": 1.0, "confidence": "high"},
    "10-day-il": {"severity": 1.0, "confidence": "high"},
    "7-day il": {"severity": 1.0, "confidence": "high"},
    "suspension": {"severity": 1.0, "confidence": "high"},
    "bereavement": {"severity": 0.8, "confidence": "medium"},
    "day-to-day": {"severity": 0.35, "confidence": "low"},
}

POSITION_WEIGHT_PP = 1.2  # base weight (pp), full severity, BEFORE the OPS multiplier -- rough, not fitted, see docstring
MAX_TOTAL_PP = 4.0

# Genuinely UNRESOLVED status -- "day-to-day" is MLB's only real game-time-
# decision tier; every IL designation/suspension/bereavement listing is
# already a settled, known fact (fully priced in via severity above), not
# something to wait on. Same distinction as injury_rules.py's (NFL) and
# injury_rules_nba.py's identical constant.
UNRESOLVED_STATUSES = {"day-to-day"}

MIN_PA = 100
_OPS_TIERS = [
    (0.843, 1.6),  # ~top 10% of qualified batters -- star-level hitter
    (0.708, 1.0),  # ~league-average regular -- unchanged from the original flat weight
    (0.587, 0.5),  # ~bottom 10% -- weak everyday bat
]
_BELOW_LOWEST_TIER_MULTIPLIER = 0.3  # below even the weak-hitter tier -- fringe roster player


def _severity_multiplier(ops: float | None) -> float:
    if ops is None:
        return 1.0
    for threshold, multiplier in _OPS_TIERS:
        if ops >= threshold:
            return multiplier
    return _BELOW_LOWEST_TIER_MULTIPLIER


class BatterOpsCache:
    """In-process cache of the CURRENT season's cumulative batting OPS,
    refreshed on a TTL -- one bulk call for every batter, same role as
    pitcher_ratings_mlb.py's PitcherRatingCache but for hitters."""

    def __init__(self, ttl_seconds: int = 6 * 3600):
        self._ttl = ttl_seconds
        self._season: int | None = None
        self._ops_by_name: dict[str, tuple[float, int]] = {}
        self._fetched_at: datetime.datetime | None = None

    def _refresh_if_stale(self, season: int):
        now = datetime.datetime.utcnow()
        stale = (
            self._fetched_at is None
            or self._season != season
            or (now - self._fetched_at).total_seconds() > self._ttl
        )
        if not stale:
            return
        splits = statsapi_mlb_client.get_season_hitting_stats(season)
        ops_by_name: dict[str, tuple[float, int]] = {}
        for s in splits:
            name = (s.get("player") or {}).get("fullName")
            stat = s.get("stat") or {}
            ops, pa = stat.get("ops"), stat.get("plateAppearances")
            if not name or ops is None or pa is None:
                continue
            try:
                ops_by_name[name] = (float(ops), int(pa))
            except (TypeError, ValueError):
                continue
        self._ops_by_name = ops_by_name
        self._season = season
        self._fetched_at = now

    def get_ops(self, season: int, player_name: str) -> float | None:
        self._refresh_if_stale(season)
        entry = self._ops_by_name.get(player_name)
        if entry is None:
            return None
        ops, pa = entry
        return ops if pa >= MIN_PA else None


def compute_injury_adjustment(
    home_injuries: list[dict],
    away_injuries: list[dict],
    season: int,
    ops_cache: BatterOpsCache,
) -> NewsAdjustment | None:
    """home_injuries/away_injuries: lists of {player_name, position, status,
    athlete_id} from espn_mlb_client.fetch_all_injuries()."""
    factors: list[Factor] = []
    home_pp = away_pp = 0.0
    requires_review = False

    for injuries, side in ((home_injuries, "home"), (away_injuries, "away")):
        side_total = 0.0
        for inj in injuries:
            if inj.get("position") in _PITCHER_POSITIONS:
                continue  # already priced in via the probable-pitcher baseline, see module docstring
            status_lower = (inj.get("status") or "").lower()
            rule = STATUS_RULES.get(status_lower)
            if rule is None:
                continue
            ops = ops_cache.get_ops(season, inj.get("player_name", ""))
            multiplier = _severity_multiplier(ops)
            pp = POSITION_WEIGHT_PP * multiplier * rule["severity"]
            side_total += pp
            ops_note = f", {ops:.3f} OPS this season" if ops is not None else ""
            factors.append(
                Factor(
                    factor=f"{inj.get('player_name', 'Unknown')} ({inj.get('position', '?')}) listed as {inj.get('status')}{ops_note}",
                    direction="favor_away" if side == "home" else "favor_home",
                    weight="minor" if pp < 1.0 else ("moderate" if pp < 2.5 else "major"),
                    rationale=f"{POSITION_WEIGHT_PP}pp base position weight x {multiplier:.1f} player-value multiplier x {rule['severity']} severity for {inj.get('status')}.",
                )
            )
            # Real, previously-missing signal (fixed 2026-07-17, was
            # hardcoded False) -- same "rotation-or-better + still genuinely
            # undecided" logic as injury_rules_nba.py.
            if pp >= 1.0 and status_lower in UNRESOLVED_STATUSES:
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

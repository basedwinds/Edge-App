"""Free, rule-based Soccer injury adjustment -- parallel to
injury_rules_nba.py, reusing the exact same NewsAdjustment/Factor schema.

Genuinely different real shape from every other sport's injury source in
this app: Transfermarkt's own injury list (see transfermarkt_client.py) is
BINARY -- a player is either on it (currently injured, out indefinitely or
for some real known/unknown span) or not -- there is no "Questionable"/
"Day-To-Day" partial-probability vocabulary the way ESPN's NFL/NBA/MLB
injury reports have. So there is no STATUS_RULES severity tier here (unlike
injury_rules_nba.py) and no `requires_review`/UNRESOLVED_STATUSES concept
either -- every row on this list is already a fully-known fact for the
purposes of this adjustment, not a pending game-time decision.

Severity is instead driven entirely by the player's real Transfermarkt
market value (a genuine market-assessed proxy for "how much would losing
this player actually hurt," see transfermarkt_client.py's own docstring) --
rough, round-number EUR tiers, not fitted (no free historical
injury-outcome dataset exists to fit against for Soccer any more than it
does for NBA), same "auditable, not precisely estimated" honesty tier as
every other situational constant in this app."""
from app.models.news_adjustment.schema import Factor, NewsAdjustment, clamp_adjustment

# EUR market-value tiers -> flat pp weight for a single injured player.
# Rough round-number cutoffs, not fitted. A top-tier attacker/midfielder at
# a big club can carry a real market value north of EUR 80-100m -- these
# thresholds are set relative to that real range, not guessed in a vacuum.
_VALUE_TIERS = [
    (50_000_000.0, 2.5),  # genuine star, a real difference-maker missing
    (20_000_000.0, 1.5),  # first-choice starter
    (5_000_000.0, 0.7),   # rotation-level squad player
]
_BELOW_LOWEST_TIER_PP = 0.25  # fringe/reserve player
_UNKNOWN_VALUE_PP = 0.5  # no market value listed -- keep a small, non-zero default rather than guessing in either direction

MAX_TOTAL_PP = 4.5  # cap so several simultaneous injuries can't dwarf the Poisson-model signal entirely


def _player_pp(market_value_eur: float | None) -> float:
    if market_value_eur is None:
        return _UNKNOWN_VALUE_PP
    for threshold, pp in _VALUE_TIERS:
        if market_value_eur >= threshold:
            return pp
    return _BELOW_LOWEST_TIER_PP


def _format_value(market_value_eur: float | None) -> str:
    if market_value_eur is None:
        return "value unknown"
    if market_value_eur >= 1_000_000:
        return f"EUR {market_value_eur / 1_000_000:.1f}m value"
    return f"EUR {market_value_eur / 1_000:.0f}k value"


def compute_injury_adjustment(home_injuries: list[dict], away_injuries: list[dict]) -> NewsAdjustment | None:
    """home_injuries/away_injuries: lists of {player_name, position, injury,
    market_value_eur} for whichever club's Transfermarkt roster this match's
    two teams matched onto (see transfermarkt_client.fetch_league_injuries).
    Every row here is a currently-injured player -- no severity/status field
    to gate on, so every row contributes (unlike injury_rules_nba.py's
    STATUS_RULES lookup, which skips unrecognized statuses)."""
    factors: list[Factor] = []
    home_pp = away_pp = 0.0

    for injuries, side in ((home_injuries, "home"), (away_injuries, "away")):
        side_total = 0.0
        for inj in injuries:
            pp = _player_pp(inj.get("market_value_eur"))
            side_total += pp
            factors.append(
                Factor(
                    factor=f"{inj.get('player_name', 'Unknown')} ({inj.get('position') or '?'}) injured "
                           f"({inj.get('injury') or 'unspecified'}, {_format_value(inj.get('market_value_eur'))})",
                    direction="favor_away" if side == "home" else "favor_home",
                    weight="minor" if pp < 1.0 else ("moderate" if pp < 2.0 else "major"),
                    rationale=f"{pp}pp for a player at this market-value tier currently injured (Transfermarkt).",
                )
            )
        if side == "home":
            home_pp = min(side_total, MAX_TOTAL_PP)
        else:
            away_pp = min(side_total, MAX_TOTAL_PP)

    if not factors:
        return None

    net_pp = clamp_adjustment(away_pp - home_pp)  # away injuries help home team, and vice versa
    confidence = "high" if max(home_pp, away_pp) >= 2.5 else ("medium" if max(home_pp, away_pp) >= 1.0 else "low")
    return NewsAdjustment(adjustment_pct=net_pp, confidence=confidence, factors=factors, requires_review=False)

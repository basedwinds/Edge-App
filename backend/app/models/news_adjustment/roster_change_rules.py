"""Offseason/season-start starter-quality-change signal for QB/RB/WR/TE --
the positions with a clean free career-EPA metric (qb_ratings.py,
skill_position_ratings.py). Compares each team's currently-listed Week-1-ish
starter at these positions against who started there for that team at the
END of the previous season -- catches free-agency signings, trades, and
(when the departing player has career data but the incoming one doesn't)
rookie/journeyman promotions, without needing to track transactions
directly. This is the direct answer to "are we accounting for offseason
signings/drafting" -- confirmed 2026-07-16 that the only prior offseason-
aware mechanism was Elo's flat 1/3 season-to-season mean reversion (generic,
not team-specific), plus qb_ratings.py's career-EPA scoring being invoked
ONLY via the backup-QB-out injury pathway, not as a persistent Week-1
baseline. This module closes that specific gap.

Deliberately does NOT attempt a similar signal for O-line/D-line/secondary/
linebacker: those positions have no comparably clean free box-score quality
metric (sacks-allowed/tackles are heavily scheme- and teammate-confounded).
A cruder "how many starters changed, regardless of direction" proxy was
checked directly against real data first (2026-07-16, same discipline as
every other signal in this project) and REJECTED: raw team-wide starter
turnover is strongly confounded with prior-season record (bad teams turn
over more players -- r=-0.377 between turnover% and prior-season wins,
n=127 team-season transitions from 2021-2025 depth-chart snapshots), and
even after partialling that confound out, turnover only weakly/non-
significantly predicts the following season's win-total change (partial
r=0.13). Not worth building a signal that can't reliably say which
direction a lineup change points -- exactly the problem a QUALITY-aware
metric (this module, for the 4 positions where one exists for free) solves
and a raw turnover COUNT does not.

Magnitude is NOT independently backtest-derived for this specific feature
(no historical "depth-chart swap -> game outcome" regression was run, unlike
epa_mismatch_rules.py's real coefficient). Reuses that module's EPA-to-win-
probability conversion scale as a reasonable, order-of-magnitude
approximation, capped well below a full starting-QB-out injury (see
injury_rules.py) since a single skill-position swap is a smaller effect
than losing the entire starting QB for one game.
"""
from app.models.news_adjustment.schema import Factor, NewsAdjustment, clamp_adjustment
from app.models.skill_position_ratings import _canonical_key

EPA_DIFF_SCALE_PP = 20.9  # reused from epa_mismatch_rules.py, see that module's docstring for the derivation
MAX_ADJUSTMENT_PP = 1.5  # small cap -- single-position swap, unvalidated-for-this-feature magnitude
NOTABLE_EPA_DIFF = 0.08  # below this, treat as a lateral move rather than a real upgrade/downgrade

POSITION_LABELS = {"QB": "starting QB", "RB": "starting RB", "WR": "starting WR", "TE": "starting TE"}


def _epa_value(stats: dict) -> float:
    """qb_ratings.py's career-stats dicts key the rate as "epa_per_dropback";
    skill_position_ratings.py's (RB/WR/TE) key it "epa_per_play" -- same
    underlying quantity (EPA per relevant opportunity), different dict shape
    since the two modules were built independently. Normalized here rather
    than renaming either module's own field, since each name is the more
    natural one in its own context."""
    return stats.get("epa_per_dropback", stats.get("epa_per_play"))


def _single_side_delta(
    team: str, is_home: bool, position: str, old_name: str | None, new_name: str | None, stats_lookup: dict
):
    if not old_name or not new_name or old_name == new_name:
        return None
    old_stats = stats_lookup.get(_canonical_key(old_name))
    new_stats = stats_lookup.get(_canonical_key(new_name))
    if old_stats is None or new_stats is None:
        return None

    old_epa, new_epa = _epa_value(old_stats), _epa_value(new_stats)
    diff = new_epa - old_epa
    if abs(diff) < NOTABLE_EPA_DIFF:
        return None

    team_pct = max(-MAX_ADJUSTMENT_PP, min(MAX_ADJUSTMENT_PP, diff * EPA_DIFF_SCALE_PP))  # positive = upgrade for `team`
    home_signed_pct = team_pct if is_home else -team_pct
    favors_home = home_signed_pct > 0
    factor = Factor(
        factor=f"{team} {POSITION_LABELS[position]} changed since last season: {new_name} "
        f"({new_epa:+.2f} EPA/play) replacing {old_name} ({old_epa:+.2f})",
        direction="favor_home" if favors_home else "favor_away",
        weight="minor",
        rationale="Career EPA/play comparison between this season's projected starter and who started there "
        "for this team at the end of last season -- catches free-agency/trade/draft-driven lineup changes "
        "without tracking transactions directly. Magnitude is a rough analogy to epa_mismatch_rules.py's "
        "scale, not independently backtested for this feature.",
    )
    return home_signed_pct, factor


def compute_roster_change_adjustment(
    home_team: str,
    away_team: str,
    home_current: dict[str, str],
    away_current: dict[str, str],
    home_previous: dict[str, str],
    away_previous: dict[str, str],
    qb_stats: dict,
    rush_stats: dict,
    recv_stats: dict,
) -> NewsAdjustment | None:
    total_pct = 0.0
    factors: list[Factor] = []
    for position, stats_lookup in (("QB", qb_stats), ("RB", rush_stats), ("WR", recv_stats), ("TE", recv_stats)):
        home_res = _single_side_delta(
            home_team, True, position, home_previous.get(position), home_current.get(position), stats_lookup
        )
        away_res = _single_side_delta(
            away_team, False, position, away_previous.get(position), away_current.get(position), stats_lookup
        )
        if home_res:
            total_pct += home_res[0]
            factors.append(home_res[1])
        if away_res:
            total_pct += away_res[0]
            factors.append(away_res[1])

    if not factors:
        return None
    return NewsAdjustment(adjustment_pct=clamp_adjustment(total_pct), confidence="low", factors=factors, requires_review=False)

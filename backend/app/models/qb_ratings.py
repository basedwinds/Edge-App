"""Free per-QB career-quality signal, used to scale the "starting QB out"
injury penalty (app/models/news_adjustment/injury_rules.py) by how capable
the actual backup is, instead of treating every case the same regardless of
who's stepping in. Built from nflverse's cached PBP parquet files
(data/pbp_cache/, already downloaded for the Phase 2 moneyline backtest --
see backend/scripts/backtest_moneyline.py) rather than a new network source.

"Starts" is approximated as games with >=10 QB dropbacks (nflverse's own
qb_dropback flag: pass attempts + sacks + scrambles) for that player -- a
cheap proxy for "played meaningful QB snaps that game," since the data has
no dedicated started-game flag. EPA/dropback (qb_epa, nflverse's QB-specific
EPA attribution) is a flat career average across every cached season -- not
recency-weighted or opponent-adjusted, deliberately rough (same spirit as
this project's other hand-picked constants, e.g. POSITION_WEIGHTS_PP in
injury_rules.py). Good enough to separate "proven, capable backup" from
"unproven/journeyman" without building a full QB model.

Player-name matching is the familiar cross-source headache: PBP uses
nflverse's "F.Last" passer_player_name convention (e.g. "T.McKee"), while
the depth chart and games.csv both use full names ("Tanner McKee"). Both are
folded to the same first-initial+last-name key here -- same tradeoff as
every other cross-source name match in this project (misses suffixes like
"Jr."/"II" and some multi-word surnames; a known simplification, not a bug,
consistent with how depth_chart_client/matching code elsewhere in this
project handles the same issue).
"""
import datetime
import glob
import os

import pandas as pd

MIN_DROPBACKS_PER_START = 10
MIN_CAREER_DROPBACKS_TO_RATE = 100  # below this, a backup's EPA is too noisy to trust

GOOD_BACKUP_MIN_STARTS = 16  # roughly a full season as a starter
SOME_EXPERIENCE_MIN_STARTS = 4
GOOD_BACKUP_MIN_EPA_PER_DROPBACK = -0.05  # permissive "competent, not a disaster" bar

CACHE_TTL = datetime.timedelta(hours=24)
_cache: dict = {"fetched_at": None, "stats_by_qb": None}


def _cache_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "data", "pbp_cache")


def _canonical_key(name: str) -> str:
    cleaned = name.replace(".", " ").replace("'", "").strip()
    parts = [p for p in cleaned.split() if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0].lower()
    return (parts[0][0] + parts[-1]).lower()


def compute_qb_career_stats() -> dict[str, dict]:
    """Returns {canonical_key: {"starts": int, "epa_per_dropback": float}}."""
    files = sorted(glob.glob(os.path.join(_cache_dir(), "pbp_*.parquet")))
    if not files:
        return {}

    cols = ["game_id", "passer_player_name", "qb_dropback", "qb_epa"]
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f, columns=cols))
        except Exception:
            continue
    if not frames:
        return {}

    pbp = pd.concat(frames, ignore_index=True)
    pbp = pbp[(pbp["qb_dropback"] == 1) & pbp["passer_player_name"].notna() & pbp["qb_epa"].notna()]

    per_game = pbp.groupby(["passer_player_name", "game_id"]).size().reset_index(name="dropbacks")
    starts = per_game[per_game["dropbacks"] >= MIN_DROPBACKS_PER_START].groupby("passer_player_name").size()

    totals = pbp.groupby("passer_player_name").agg(total_dropbacks=("qb_epa", "size"), epa_sum=("qb_epa", "sum"))

    out: dict[str, dict] = {}
    for name, row in totals.iterrows():
        if row["total_dropbacks"] < MIN_CAREER_DROPBACKS_TO_RATE:
            continue
        key = _canonical_key(name)
        if not key:
            continue
        out[key] = {
            "starts": int(starts.get(name, 0)),
            "epa_per_dropback": float(row["epa_sum"] / row["total_dropbacks"]),
        }
    return out


def get_qb_career_stats() -> dict[str, dict]:
    now = datetime.datetime.utcnow()
    if (
        _cache["stats_by_qb"] is not None
        and _cache["fetched_at"] is not None
        and now - _cache["fetched_at"] < CACHE_TTL
    ):
        return _cache["stats_by_qb"]

    stats = compute_qb_career_stats()
    _cache.update(fetched_at=now, stats_by_qb=stats)
    return stats


def lookup_backup_stats(backup_name: str | None, career_stats: dict[str, dict]) -> dict | None:
    if not backup_name:
        return None
    return career_stats.get(_canonical_key(backup_name))


def backup_quality_multiplier(stats: dict | None) -> tuple[float, str]:
    """Scales the flat QB-out penalty (1.0 = unchanged) based on how capable
    the listed backup actually is."""
    if stats is None:
        return 1.0, "no career passing data found for the backup -- treated as fully unproven"

    starts, epa = stats["starts"], stats["epa_per_dropback"]
    if starts >= GOOD_BACKUP_MIN_STARTS and epa >= GOOD_BACKUP_MIN_EPA_PER_DROPBACK:
        return 0.5, f"{starts} career starts, {epa:+.2f} EPA/dropback -- proven, capable backup"
    if starts >= SOME_EXPERIENCE_MIN_STARTS:
        return 0.75, f"{starts} career starts, {epa:+.2f} EPA/dropback -- some starting experience"
    return 1.0, f"{starts} career start(s), {epa:+.2f} EPA/dropback -- largely unproven backup"

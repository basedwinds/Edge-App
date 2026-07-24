"""One-off local cache builder for point-in-time starting-pitcher stat
snapshots -- used to validate (and, if real, later power) a starting-pitcher
signal for the MLB baseline model, see elo_mlb.py's docstring for why this
matters more for MLB than NFL/NBA's team-level-only baselines.

Uses MLB Stats API's `stats=byDateRange` bulk endpoint (confirmed live,
ONE call returns cumulative stats for every pitcher, not just ERA-title-
qualified ones -- see statsapi_mlb_client.py's docstring) to build a
CUMULATIVE-TO-DATE snapshot every ~14 days within each season (one snapshot
per {season, cutoff_date} pair -- a pitcher rotation turns over every ~5
days, so 14-day spacing is frequent enough to track real form changes while
keeping this to ~130 total requests across 10 seasons, not ~1,800 for a
daily snapshot). Every downstream lookup uses the snapshot strictly BEFORE a
game's date (walk-forward, no leakage) -- see
check_mlb_pitcher_signal.py for how this cache is consumed.

Run: backend/.venv/Scripts/python.exe scripts/build_mlb_pitcher_snapshot_cache.py
"""
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clients import statsapi_mlb_client  # noqa: E402

CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_pitcher_snapshot_cache.json"
SCHEDULE_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_schedule_cache.json"
SNAPSHOT_INTERVAL_DAYS = 14


def _season_bounds(games: list[dict]) -> dict[int, tuple[dt.date, dt.date]]:
    bounds: dict[int, list[dt.date]] = {}
    for g in games:
        if g["season"] >= 2026:
            continue  # in-progress season handled live, not backfilled
        d = dt.date.fromisoformat(g["gameday"])
        bounds.setdefault(g["season"], [d, d])
        bounds[g["season"]][0] = min(bounds[g["season"]][0], d)
        bounds[g["season"]][1] = max(bounds[g["season"]][1], d)
    return {s: (lo, hi) for s, (lo, hi) in bounds.items()}


def main():
    games = json.loads(SCHEDULE_CACHE_PATH.read_text())
    season_bounds = _season_bounds(games)

    cache: dict[str, dict] = {}
    for season, (start, end) in sorted(season_bounds.items()):
        cache[str(season)] = {}
        cutoff = start + dt.timedelta(days=SNAPSHOT_INTERVAL_DAYS)
        t0 = time.monotonic()
        n_snapshots = 0
        while cutoff <= end:
            splits = statsapi_mlb_client.get_pitching_stats_by_date_range(
                f"{start:%Y-%m-%d}", f"{cutoff - dt.timedelta(days=1):%Y-%m-%d}", season
            )
            snapshot = {}
            for s in splits:
                pid = s.get("player", {}).get("id")
                stat = s.get("stat", {})
                ip = stat.get("inningsPitched")
                gs = stat.get("gamesStarted")
                era = stat.get("era")
                if pid is None or ip is None or era is None:
                    continue
                try:
                    ip_f = float(ip)
                    era_f = float(era)
                except ValueError:
                    continue  # era can be "-.--" (0 innings pitched, div-by-zero placeholder)
                snapshot[str(pid)] = {"era": era_f, "ip": ip_f, "gs": gs or 0}
            cache[str(season)][cutoff.isoformat()] = snapshot
            n_snapshots += 1
            cutoff += dt.timedelta(days=SNAPSHOT_INTERVAL_DAYS)
        print(f"{season}: {n_snapshots} snapshots ({time.monotonic() - t0:.1f}s)")

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=None))
    print(f"Wrote {CACHE_PATH} ({CACHE_PATH.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()

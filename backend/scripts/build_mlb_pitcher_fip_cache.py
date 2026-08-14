"""Point-in-time starter snapshots carrying FIP COMPONENTS, not just ERA.

WHY. The MLB pitcher blend is the strongest signal in the whole MLB model
(r=0.305 vs elo_diff's own 0.186), and it is built on current-season ERA --
the noisiest of the common pitcher descriptors. ERA credits or blames a
pitcher for balls in play his defence handled, so it regresses hard year over
year. FIP uses only the three outcomes a pitcher controls almost alone
(strikeouts, walks/HBP, home runs) and is the standard, better-documented
predictor of FUTURE run prevention. K-BB% is the same idea normalised by
batters faced instead of innings.

    FIP = (13*HR + 3*(BB + HBP) - 2*K) / IP  +  C
    K-BB% = (K - BB) / battersFaced

NO NEW DATA SOURCE, AND THAT IS THE POINT. Every component already comes back
in the SAME free StatsAPI call the ERA cache is built from
(stats=byDateRange&group=pitching) -- verified live before writing this:
homeRuns, baseOnBalls, hitBatsmen, strikeOuts, inningsPitched and battersFaced
are all present on every split. So this costs zero extra API surface and stays
inside the free-sources-only constraint.

WALK-FORWARD BY CONSTRUCTION. byDateRange is queried from each season's start
to the day BEFORE the snapshot date, exactly as the ERA cache does, so a
snapshot can only ever contain games already played. The comparison script
then picks the latest snapshot strictly before each game date.

WHY A SEPARATE FILE. It deliberately does NOT overwrite
mlb_pitcher_snapshot_cache.json. The shipped ERA_DIFF_TO_ELO_POINTS = 9.73 was
derived from that file; regenerating it in place would silently rebase the
evidence for a constant that is already live, and any drift in StatsAPI's
historical numbers would be invisible. This writes alongside it so the old
derivation stays reproducible and the two can be compared on identical games.

C IS NOT APPLIED HERE. The FIP constant only shifts the whole league by a
fixed amount each season, and every use here is a DIFFERENCE between two
starters, where it cancels exactly. Storing the raw component form keeps that
explicit rather than baking in a constant that would do nothing.

Run: backend/.venv/Scripts/python.exe scripts/build_mlb_pitcher_fip_cache.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import datetime as dt  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402

from app.clients import statsapi_mlb_client  # noqa: E402

SCHEDULE_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_schedule_cache.json"
CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_pitcher_fip_cache.json"
SNAPSHOT_INTERVAL_DAYS = 14


def _ip_to_float(raw) -> float | None:
    """StatsAPI writes innings as "124.1" meaning 124 and 1/3 innings, NOT
    124.1 innings. Treating it as a decimal inflates every rate stat's
    denominator by up to a third of an inning -- small per pitcher, but it
    biases FIP in the same direction for everyone, which is exactly the kind of
    quiet systematic error a correlation check would never surface."""
    if raw is None:
        return None
    whole, _, frac = str(raw).partition(".")
    try:
        outs = int(frac) if frac else 0
        if outs not in (0, 1, 2):
            return None
        return int(whole) + outs / 3.0
    except ValueError:
        return None


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


def main() -> None:
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
                ip_f = _ip_to_float(stat.get("inningsPitched"))
                if pid is None or ip_f is None or ip_f <= 0:
                    continue
                try:
                    era_f = float(stat.get("era"))
                except (TypeError, ValueError):
                    continue  # "-.--" placeholder at 0 IP
                def _i(key):
                    try:
                        return int(stat.get(key) or 0)
                    except (TypeError, ValueError):
                        return 0
                snapshot[str(pid)] = {
                    "era": era_f,
                    "ip": ip_f,
                    "gs": stat.get("gamesStarted") or 0,
                    "hr": _i("homeRuns"),
                    "bb": _i("baseOnBalls"),
                    "hbp": _i("hitBatsmen"),
                    "k": _i("strikeOuts"),
                    "bf": _i("battersFaced"),
                }
            cache[str(season)][cutoff.isoformat()] = snapshot
            n_snapshots += 1
            cutoff += dt.timedelta(days=SNAPSHOT_INTERVAL_DAYS)
        print(f"{season}: {n_snapshots} snapshots, "
              f"{len(cache[str(season)].get(max(cache[str(season)], default=''), {}))} pitchers in last "
              f"({time.monotonic() - t0:.1f}s)")

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache))
    print(f"\nwrote {CACHE_PATH} ({CACHE_PATH.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()

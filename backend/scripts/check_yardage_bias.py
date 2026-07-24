"""One-off: does season_projections.py's yardage projection (mean = last
season's REG total) systematically over/under-predict next season? The module
applies a derived -25% regression correction to TD categories but ASSUMED
yardage bias negligible ('worth rechecking with a larger sample'). Measure it."""
import glob
import os

import pandas as pd

cache = os.path.join(os.path.dirname(__file__), "..", "..", "data", "pbp_cache")
files = sorted(glob.glob(os.path.join(cache, "pbp_*.parquet")))
cols = ["season", "season_type", "game_id", "passer_player_name", "rusher_player_name",
        "receiver_player_name", "passing_yards", "rushing_yards", "receiving_yards",
        "pass_attempt", "rush_attempt", "complete_pass"]


def totals(pcol, filt, val):
    df = pd.concat([pd.read_parquet(f, columns=cols) for f in files], ignore_index=True)
    df = df[(df["season_type"] == "REG") & (df[filt] == 1)]
    g = df.groupby([pcol, "season"]).agg(total=(val, "sum"), games=("game_id", "nunique")).reset_index()
    return g[g["games"] >= 8].rename(columns={pcol: "p"})


for name, pcol, filt, val in [
    ("pass_yds", "passer_player_name", "pass_attempt", "passing_yards"),
    ("rush_yds", "rusher_player_name", "rush_attempt", "rushing_yards"),
    ("rec_yds", "receiver_player_name", "complete_pass", "receiving_yards"),
]:
    g = totals(pcol, filt, val)
    m = g.merge(g.assign(season=g["season"] + 1), on=["p", "season"], suffixes=("_next", "_last"))
    if len(m) == 0:
        continue
    err = (m["total_next"] - m["total_last"])
    proj = m["total_last"].mean()
    pct = 100 * err.mean() / proj if proj else 0
    print(f"{name}: n={len(m)} pairs | actual-next avg {m['total_next'].mean():.0f} vs "
          f"projected(last) {proj:.0f} | bias {err.mean():+.0f} ({pct:+.1f}% of projection)")

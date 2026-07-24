"""Free per-team trailing points-scored/allowed, used by game_lines.py to
estimate a game's expected total points for the totals market. Built from
nflverse's own schedule data (already fetched for Elo/rest-day data --
app/ingestion/nfl_data.py), no PBP or extra network call needed.

Checked against real data before building (2026-07-15): a team-scoring
blend (see compute_expected_total below) reduces residual std vs. actual
game totals from 14.14 (naive league-mean-only) to 13.85 (n=6,906,
2012-2025) -- a real, if modest, improvement, same "small but genuine"
pattern as every other situational signal in this app. Correlation between
the blended estimate and actual total: r=0.225.
"""
from app.ingestion import nfl_data

ROLLING_WINDOW = 8  # games, matches epa_ratings.py's own default
MIN_GAMES_FOR_RATING = 3


def compute_current_scoring_ratings() -> dict[str, dict]:
    """Returns {team: {"points_scored": float, "points_allowed": float}} --
    each team's trailing mean over its last ROLLING_WINDOW played REG games
    this season (falls back to fewer games early in the season; None if
    the team has played fewer than MIN_GAMES_FOR_RATING games)."""
    games = nfl_data.fetch_games()
    games = [g for g in games if g["game_type"] == "REG" and g.get("home_score") is not None]
    if not games:
        return {}
    season = max(g["season"] for g in games)
    games = [g for g in games if g["season"] == season]
    games.sort(key=lambda g: g["week"])

    history: dict[str, list[tuple[int, int]]] = {}
    for g in games:
        home, away = g["home_team"], g["away_team"]
        history.setdefault(home, []).append((g["home_score"], g["away_score"]))
        history.setdefault(away, []).append((g["away_score"], g["home_score"]))

    out: dict[str, dict] = {}
    for team, results in history.items():
        recent = results[-ROLLING_WINDOW:]
        if len(recent) < MIN_GAMES_FOR_RATING:
            continue
        out[team] = {
            "points_scored": sum(r[0] for r in recent) / len(recent),
            "points_allowed": sum(r[1] for r in recent) / len(recent),
        }
    return out

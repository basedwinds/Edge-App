"""ONE-OFF CHECK (not a wired-in module) -- does a real in-season NBA trade
create a systematic gap between a team's actual near-term win rate and what
the existing walk-forward Elo model already predicted for those specific
games? If Elo already "sees" the effect of a trade through normal game-to-
game updates fast enough, there's nothing to gain from a separate roster-
change adjustment (unlike NFL's roster_change_rules.py, which targets
OFFSEASON changes Elo can't see coming at all). If a real, systematic gap
exists in the games immediately after a trade, that's grounds to build a
live signal; if not, this gets reported as a checked-and-rejected finding,
same discipline as every other honest negative result in this project.

Data: ESPN's free /transactions endpoint (confirmed live 2026-07-17,
season={calendar_year} param actually changes results despite the response
echoing "season.year":2026 regardless -- verified by real distinct dates
returned per year). No athlete-ID resolution attempted for traded players
(the endpoint has no structured player/ID fields, only free-text
descriptions) -- this check is deliberately scoped to "does ANY real trade
event create an Elo blind spot," not "which specific trades help which
team," since that would need a much bigger lift (name->athlete_id
resolution + career value lookups) not worth doing before this cheaper,
decisive first check.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.db.database import SessionLocal
from app.db.models import NbaGame
from app.models.baseline.elo_nba import EloState, effective_home_court_adv, win_prob, update_ratings

TRANSACTIONS_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/transactions"
TRADE_RE = re.compile(r"^Acquired\b.*\bfrom\b", re.IGNORECASE)
POST_TRADE_WINDOW = 10  # games immediately following the trade


def fetch_trade_events(season_years: list[int]) -> list[tuple[str, str]]:
    """Returns [(team_abbr, iso_date), ...] for every genuine trade found
    (excludes re-signings/signings/waivers/two-way moves)."""
    events = []
    for year in season_years:
        page = 1
        while True:
            r = httpx.get(TRANSACTIONS_URL, params={"season": year, "page": page}, timeout=20)
            data = r.json()
            for t in data.get("transactions", []):
                desc = t.get("description", "")
                if TRADE_RE.match(desc):
                    team = (t.get("team") or {}).get("abbreviation")
                    date = t.get("date", "")[:10]
                    if team and date:
                        events.append((team, date))
            page_count = data.get("pageCount", 1)
            if page >= page_count:
                break
            page += 1
    return events


def build_elo_walkforward(games: list[NbaGame]) -> dict[str, tuple[float, float]]:
    """Returns {game_id: (home_win_prob, actual_home_win)} for every REG game
    with a final score, computed walk-forward (no leakage) -- same method as
    backtest_moneyline_nba.py."""
    state = EloState()
    out = {}
    for g in sorted(games, key=lambda g: (g.season, g.gameday)):
        state.start_season_if_new(g.season)
        home_adv = effective_home_court_adv(g.home_team, g.location, g.home_rest, g.away_rest)
        p_home = win_prob(state.get(g.home_team), state.get(g.away_team), home_adv)
        if g.home_score is not None and g.away_score is not None:
            actual = 1.0 if g.home_score > g.away_score else 0.0
            out[g.id] = (p_home, actual)
            update_ratings(state, g.home_team, g.away_team, g.home_score, g.away_score, home_adv)
    return out


def main():
    print("Fetching real NBA trade transactions (2022-2025 calendar years, ESPN /transactions)...")
    events = fetch_trade_events([2022, 2023, 2024, 2025])
    print(f"Found {len(events)} genuine trade-side events (each real trade counted once per team involved).")

    session = SessionLocal()
    games = session.query(NbaGame).filter(NbaGame.game_type == "REG").all()
    session.close()
    print(f"Building walk-forward Elo over {len(games)} cached REG games...")
    elo_by_game = build_elo_walkforward(games)

    games_by_team: dict[str, list[NbaGame]] = {}
    for g in games:
        games_by_team.setdefault(g.home_team, []).append(g)
        games_by_team.setdefault(g.away_team, []).append(g)
    for team in games_by_team:
        games_by_team[team].sort(key=lambda g: g.gameday)

    diffs = []
    matched_events = 0
    for team, trade_date in events:
        team_games = games_by_team.get(team, [])
        post = [g for g in team_games if g.gameday > trade_date and g.id in elo_by_game][:POST_TRADE_WINDOW]
        if len(post) < POST_TRADE_WINDOW:
            continue  # not enough real post-trade games yet to fill the window
        matched_events += 1
        for g in post:
            p_home, actual_home_win = elo_by_game[g.id]
            is_home = g.home_team == team
            elo_implied = p_home if is_home else (1.0 - p_home)
            actual = actual_home_win if is_home else (1.0 - actual_home_win)
            diffs.append(actual - elo_implied)

    n = len(diffs)
    if n == 0:
        print("No qualifying trade events had a full 10-game post-trade window in cached data.")
        return
    avg_diff = sum(diffs) / n
    print()
    print(f"Trade-side events with a full {POST_TRADE_WINDOW}-game post-trade window: {matched_events}")
    print(f"Total (team, game) observations in those windows: {n}")
    print(f"Avg(actual win - Elo-implied win prob) in the {POST_TRADE_WINDOW} games after ANY real trade: {avg_diff:+.4f}")
    print()
    print("=" * 70)
    if abs(avg_diff) < 0.02:
        print(f"NO SIGNAL: {avg_diff:+.4f} is close to zero -- Elo's normal game-to-game updates")
        print("already seem to keep pace with real trades; no exploitable blind spot found.")
    else:
        direction = "OVERPERFORM" if avg_diff > 0 else "UNDERPERFORM"
        print(f"POSSIBLE SIGNAL: teams involved in a trade {direction} their Elo-implied win rate")
        print(f"by {abs(avg_diff)*100:.1f}pp on average in the {POST_TRADE_WINDOW} games right after -- worth digging into direction/quality next.")
    print("=" * 70)


if __name__ == "__main__":
    main()

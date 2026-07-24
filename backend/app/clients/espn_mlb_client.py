"""Free, no-auth ESPN endpoint for MLB injuries -- confirmed live 2026-07-17
(287 real injuries across all 30 teams). Team-level responses key teams by
full "City Mascot" display name (e.g. "Arizona Diamondbacks"), same
convention already confirmed for Polymarket -- reuses
market_matcher_mlb.py's POLYMARKET_FULLNAME_TO_STATSAPI_ABBR rather than
building a second, redundant name map.

Real status vocabulary confirmed live (not guessed): "Out", "Day-To-Day",
"10-Day-IL", "15-Day-IL", "60-Day-IL", "7-Day IL" (note: no hyphen, unlike
the others), "Suspension"/"suspension" (both cases seen), "Bereavement"/
"bereavement". Real positions confirmed live include pitchers (SP/RP) --
deliberately included in this fetch (filtering pitchers out is
injury_rules_mlb.py's job, not this client's, since a future situational
module might want reliever-injury data even though the v1 module doesn't).
"""
from app.clients.base import get_json
from app.ingestion.market_matcher_mlb import POLYMARKET_FULLNAME_TO_STATSAPI_ABBR

INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/injuries"


def fetch_all_injuries() -> dict[str, list[dict]]:
    """Returns {team_abbr: [{player_name, position, status, athlete_id}]} --
    teams with no injuries are simply absent, not an empty list (same
    "don't manufacture rows" convention as elsewhere)."""
    data = get_json(INJURIES_URL)
    by_team: dict[str, list[dict]] = {}
    for team in data.get("injuries", []):
        abbr = POLYMARKET_FULLNAME_TO_STATSAPI_ABBR.get(team.get("displayName", ""))
        if abbr is None:
            continue
        rows = []
        for inj in team.get("injuries", []):
            athlete = inj.get("athlete") or {}
            rows.append(
                {
                    "player_name": athlete.get("displayName"),
                    "position": (athlete.get("position") or {}).get("abbreviation"),
                    "status": inj.get("status"),
                    "athlete_id": athlete.get("id"),
                }
            )
        if rows:
            by_team[abbr] = rows
    return by_team

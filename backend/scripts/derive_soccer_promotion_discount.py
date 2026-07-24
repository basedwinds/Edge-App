"""Derives a REAL, data-grounded conversion factor between a team's rating
in a country's SECOND tier (E1/SP2/I2/D2/F2) and its real top-flight
strength once promoted -- replacing the old blind "bottom-quartile of the
top-flight" placeholder season_sim_soccer.py used for every promoted team
with an actual estimate derived from how promoted teams historically
performed.

Method: walk both a division and its own second tier's ratings forward
together (same cache, same chronological order). For every real team that
appears in the second tier during season S and then in the top flight
during season S+1 (a genuine promotion, identified directly from which
league each of their matches falls in, not guessed), record:
  - their SECOND-TIER rating at the end of season S (their rating going
    INTO promotion)
  - their TOP-FLIGHT rating at the end of season S+1 (their rating after a
    full season established in the new division -- avoids the noise of
    using just their first few games)
Average the real (top_flight_rating - second_tier_rating) gap across every
such real promotion event, in LOG space (additive there = multiplicative in
goal-rate space) -- this is the constant season_sim_soccer.py/
elo_service_soccer.py apply to a promoted team's real second-tier rating
instead of discarding it for a blind placeholder."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.football_data_client import PROMOTION_SOURCE_DIVISION  # noqa: E402
from app.ingestion import soccer_data  # noqa: E402
from app.models.baseline.elo_soccer import SoccerRatingState, predict_and_update  # noqa: E402


def main():
    matches = soccer_data.load_football_data_matches()
    print(f"Total football-data.co.uk matches (top-flight + second-tier): {len(matches)}")

    states: dict[str, SoccerRatingState] = {}
    # Track each team's rating snapshot at the END of every season they play
    # in a given league: (league, team, season) -> (attack, concede)
    end_of_season_rating: dict[tuple[str, str, str], tuple[float, float]] = {}

    for m in matches:
        league = m["league"]
        state = states.setdefault(league, SoccerRatingState())
        predict_and_update(state, m)
        season = m.get("season")
        if season is None:
            continue
        for team in (m["home_team"], m["away_team"]):
            end_of_season_rating[(league, team, season)] = (state.get_attack(team), state.get_concede(team))

    # Find real promotions: team has a second-tier rating for season S, AND
    # a top-flight rating for the season immediately after S.
    attack_gaps, concede_gaps = [], []
    examples = []
    for top_div, second_div in PROMOTION_SOURCE_DIVISION.items():
        second_tier_seasons = {(team, season) for (lg, team, season) in end_of_season_rating if lg == second_div}
        for team, season in second_tier_seasons:
            start_year = int(season.split("-")[0])
            next_season = f"{start_year + 1}-{start_year + 2}"
            top_flight_key = (top_div, team, next_season)
            if top_flight_key not in end_of_season_rating:
                continue
            second_tier_attack, second_tier_concede = end_of_season_rating[(second_div, team, season)]
            top_flight_attack, top_flight_concede = end_of_season_rating[top_flight_key]
            attack_gaps.append(top_flight_attack - second_tier_attack)
            concede_gaps.append(top_flight_concede - second_tier_concede)
            if len(examples) < 10:
                examples.append((team, top_div, season, second_tier_attack, top_flight_attack))

    n = len(attack_gaps)
    print(f"\nReal promotion events found: {n}\n")
    print("Sample (team, top_div, season promoted from, second-tier attack, top-flight attack next season):")
    for ex in examples:
        print(f"  {ex}")

    if n == 0:
        print("\nNo real promotion events found -- cannot derive a discount, falling back to the old placeholder.")
        return

    avg_attack_gap = sum(attack_gaps) / n
    avg_concede_gap = sum(concede_gaps) / n
    import statistics
    print(f"\nAvg attack gap (top_flight - second_tier): {avg_attack_gap:+.4f} (stdev={statistics.pstdev(attack_gaps):.4f})")
    print(f"Avg concede gap (top_flight - second_tier): {avg_concede_gap:+.4f} (stdev={statistics.pstdev(concede_gaps):.4f})")
    print(f"\nIn goal-rate terms: a promoted team's real top-flight attack strength is on average")
    print(f"exp({avg_attack_gap:.4f}) = {2.718281828**avg_attack_gap:.3f}x their second-tier attack rating,")
    print(f"and their concede (leakiness) is exp({avg_concede_gap:.4f}) = {2.718281828**avg_concede_gap:.3f}x.")


if __name__ == "__main__":
    main()

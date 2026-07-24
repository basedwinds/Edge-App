"""Matches Kalshi CS2 markets to this app's liquipedia.net-sourced Cs2Match
rows. Parallel to market_matcher_valorant.py -- same DELIBERATELY EXACT
(after light normalization) matching discipline, not token-subset fuzzy
matching. CS2 orgs commonly field both a main roster and an Academy/2/Junior
sub-roster under closely related names (e.g. "FaZe" vs "FaZe Academy", "NAVI"
vs "NAVI Junior") -- the exact same false-collision risk
market_matcher_valorant.py's own docstring documents for Valorant's Game
Changers rosters, so the same exact-match default applies here."""
import re
import unicodedata


def normalize_team_name(name: str) -> str:
    if not name:
        return ""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    folded = re.sub(r"[^a-z0-9 ]", " ", folded.lower())
    return re.sub(r"\s+", " ", folded).strip()


def team_names_match(name_a: str, name_b: str) -> bool:
    a, b = normalize_team_name(name_a), normalize_team_name(name_b)
    return bool(a) and a == b


def _match_matches_pair(match: dict, team_a_name: str, team_b_name: str) -> bool:
    """Tries both the match's team_a/team_b (Liquipedia's real full name)
    AND team_a_display/team_b_display (Liquipedia's possibly-abbreviated
    schedule-listing display form, e.g. "FLY" for "FlyQuest") if present --
    Kalshi's own yes_sub_title team names sometimes align with one form,
    sometimes the other (confirmed live: Kalshi shows "Astralis"/"Heroic"
    verbatim, matching Liquipedia's full name, but this isn't guaranteed for
    every team)."""
    candidates_a = [match["team_a"]] + ([match["team_a_display"]] if match.get("team_a_display") else [])
    candidates_b = [match["team_b"]] + ([match["team_b_display"]] if match.get("team_b_display") else [])
    a_matches = any(team_names_match(team_a_name, c) for c in candidates_a)
    b_matches = any(team_names_match(team_b_name, c) for c in candidates_b)
    if a_matches and b_matches:
        return True
    a_matches_swapped = any(team_names_match(team_a_name, c) for c in candidates_b)
    b_matches_swapped = any(team_names_match(team_b_name, c) for c in candidates_a)
    return a_matches_swapped and b_matches_swapped


def match_by_names_only(team_a_name: str, team_b_name: str, all_matches: list[dict]) -> dict | None:
    if not team_a_name or not team_b_name:
        return None
    for match in all_matches:
        if _match_matches_pair(match, team_a_name, team_b_name):
            return match
    return None

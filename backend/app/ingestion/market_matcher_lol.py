"""Matches Kalshi LoL markets to this app's Leaguepedia-sourced LolMatch
rows. Parallel to market_matcher_cs2.py/market_matcher_valorant.py -- same
DELIBERATELY EXACT (after light normalization) matching discipline, not
token-subset fuzzy matching. LoL orgs commonly field both a main roster and
an Academy sub-roster under closely related names (e.g. "Cloud9" vs "Cloud9
Academy", "TSM" vs "TSM Academy") -- the same false-collision risk
market_matcher_valorant.py/market_matcher_cs2.py's own docstrings document
for their titles' equivalent sub-roster patterns."""
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
    if not a or not b:
        return False
    if a == b:
        return True
    # Space-INSENSITIVE fallback -- real bug found live 2026-07-21: Kalshi
    # lists "Nongshim Red Force" while this app's cache has "Nongshim
    # RedForce", so the two never joined and a real LCK team went unrated.
    # Removing internal spaces is SAFE against the main-vs-Academy collision
    # this matcher deliberately guards against: an "Academy"/"GC"/"Challengers"
    # suffix survives space removal as extra characters ("cloud9" !=
    # "cloud9academy"), so it can only ever merge genuine spacing variants of
    # the SAME name, never a sub-roster into its parent.
    return a.replace(" ", "") == b.replace(" ", "")


def _match_matches_pair(match: dict, team_a_name: str, team_b_name: str) -> bool:
    a, b = match["team_a"], match["team_b"]
    return (
        (team_names_match(team_a_name, a) and team_names_match(team_b_name, b))
        or (team_names_match(team_a_name, b) and team_names_match(team_b_name, a))
    )


def match_by_names_only(team_a_name: str, team_b_name: str, all_matches: list[dict]) -> dict | None:
    if not team_a_name or not team_b_name:
        return None
    for match in all_matches:
        if _match_matches_pair(match, team_a_name, team_b_name):
            return match
    return None

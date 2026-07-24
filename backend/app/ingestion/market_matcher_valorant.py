"""Matches Kalshi/Polymarket Valorant markets to this app's vlr.gg-sourced
ValorantMatch rows. Parallel to market_matcher_mma.py, but DELIBERATELY
EXACT match (after light normalization), not the token-subset fuzzy match
MMA/fighter-name matching uses. Real reason: Valorant orgs commonly field
BOTH a main roster and a separate Game Changers (women's division) roster
under closely related names -- e.g. "Gen.G" vs "Gen.G GC", "Fnatic" vs
"Fnatic GC" -- confirmed live 2026-07-19 in vlr.gg's own schedule ("Ninetails
vs Gen.G GC", a real Game Changers match). A token-subset matcher (like
fighter_names_match's "one name's word set contained in the other's") would
silently conflate these as the SAME team, since "Gen.G"'s token set is a
strict subset of "Gen.G GC"'s -- a real, different-team false match, not a
harmless middle-name/suffix case like UFC's. Exact-after-normalization is the
safer default here; only whitespace/punctuation/case/accent noise is
tolerated, not word-subset inclusion.
"""
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
    a, b = match["team_a"], match["team_b"]
    return (
        (team_names_match(team_a_name, a) and team_names_match(team_b_name, b))
        or (team_names_match(team_a_name, b) and team_names_match(team_b_name, a))
    )


def match_by_names_only(team_a_name: str, team_b_name: str, all_matches: list[dict]) -> dict | None:
    """No date filter -- same "small always-loaded upcoming set" reasoning
    as market_matcher_mma.py::match_fight_by_names_only (ValorantMatch only
    ever holds vlr.gg's own live schedule window, not a full season)."""
    if not team_a_name or not team_b_name:
        return None
    for match in all_matches:
        if _match_matches_pair(match, team_a_name, team_b_name):
            return match
    return None

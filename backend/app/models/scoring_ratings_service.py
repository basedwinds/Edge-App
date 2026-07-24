"""In-process cache of current scoring ratings, recomputed each poll cycle
-- same pattern as elo_service.py. Cheap to compute (no PBP, just the
already-fetched schedule) but nfl_data.fetch_games() itself hits the
network, so this avoids doing that on every API request.
"""
from app.models.scoring_ratings import compute_current_scoring_ratings

_cache: dict = {"ratings": None}


def refresh():
    _cache["ratings"] = compute_current_scoring_ratings()


def get_ratings() -> dict[str, dict]:
    return _cache.get("ratings") or {}

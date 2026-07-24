"""In-process cache of current NBA scoring ratings, recomputed each poll
cycle -- same pattern as scoring_ratings_service.py (NFL)."""
from app.models.scoring_ratings_nba import compute_current_scoring_ratings

_cache: dict = {"ratings": None}


def refresh():
    _cache["ratings"] = compute_current_scoring_ratings()


def get_ratings() -> dict[str, dict]:
    return _cache.get("ratings") or {}

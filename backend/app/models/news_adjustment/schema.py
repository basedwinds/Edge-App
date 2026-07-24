from typing import Literal

from pydantic import BaseModel, Field

ADJUSTMENT_CAP_PP = 7.0  # hard clamp, percentage points


class Factor(BaseModel):
    factor: str
    direction: Literal["favor_home", "favor_away", "neutral"]
    weight: Literal["minor", "moderate", "major"]
    rationale: str


class NewsAdjustment(BaseModel):
    adjustment_pct: float = Field(description=f"Home-team win-prob adjustment in percentage points, clamped to [-{ADJUSTMENT_CAP_PP}, {ADJUSTMENT_CAP_PP}]")
    confidence: Literal["low", "medium", "high"]
    factors: list[Factor]
    requires_review: bool = Field(description="True if a genuinely major unknown exists (e.g. starting QB questionable)")


CONFIDENCE_SCALAR = {"low": 0.4, "medium": 0.7, "high": 1.0}
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def clamp_adjustment(adjustment_pct: float) -> float:
    return max(-ADJUSTMENT_CAP_PP, min(ADJUSTMENT_CAP_PP, adjustment_pct))


def merge_adjustments(adjustments: list["NewsAdjustment | None"]) -> "NewsAdjustment | None":
    """Combines adjustments from independent rule modules (injuries, rest,
    weather, ...) into one: sums the percentage points (re-clamped), takes the
    highest confidence and requires_review found, concatenates factors."""
    present = [a for a in adjustments if a is not None]
    if not present:
        return None

    total_pct = clamp_adjustment(sum(a.adjustment_pct for a in present))
    confidence = max((a.confidence for a in present), key=lambda c: _CONFIDENCE_RANK[c])
    factors = [f for a in present for f in a.factors]
    requires_review = any(a.requires_review for a in present)

    return NewsAdjustment(
        adjustment_pct=total_pct,
        confidence=confidence,
        factors=factors,
        requires_review=requires_review,
    )

import math


def brier_score(predictions: list[float], outcomes: list[float]) -> float:
    """Mean squared error between predicted probability and actual outcome
    (1.0/0.0, or 0.5 for a tie). Lower is better; 0 is perfect, 0.25 is a
    coin flip's expected score against a 50/50 outcome distribution."""
    n = len(predictions)
    return sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / n


def log_loss(predictions: list[float], outcomes: list[float], eps: float = 1e-9) -> float:
    n = len(predictions)
    total = 0.0
    for p, o in zip(predictions, outcomes):
        p = min(max(p, eps), 1 - eps)
        total += -(o * math.log(p) + (1 - o) * math.log(1 - p))
    return total / n


def moneyline_to_implied_prob(moneyline: int) -> float:
    """American odds -> raw implied probability (includes vig)."""
    if moneyline > 0:
        return 100.0 / (moneyline + 100.0)
    return -moneyline / (-moneyline + 100.0)


def devig_two_way(prob_a: float, prob_b: float) -> tuple[float, float]:
    """Normalize a two-way market's raw implied probabilities (which sum to
    >1 due to vig) back to a fair 100% book."""
    total = prob_a + prob_b
    return prob_a / total, prob_b / total


def devig_three_way(prob_h: float, prob_d: float, prob_a: float) -> tuple[float, float, float]:
    """Same proportional devig as devig_two_way, extended to Soccer's 3-way
    Home/Draw/Away market -- needed fresh for backtest_moneyline_soccer.py
    since every other sport in this app is 2-way. Proportional (not Shin's
    method or another favorite-longshot-bias-aware devig) to match
    devig_two_way's own existing simplicity -- worth revisiting only if a
    real, measured favorite-longshot bias shows up in the backtest itself,
    not assumed upfront."""
    total = prob_h + prob_d + prob_a
    return prob_h / total, prob_d / total, prob_a / total


def decimal_odds_to_implied_prob(decimal_odds: float) -> float:
    """European decimal odds (football-data.co.uk's own format, e.g. 2.10)
    -> raw implied probability (includes vig): 1 / decimal_odds."""
    return 1.0 / decimal_odds

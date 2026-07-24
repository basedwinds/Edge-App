"""Runs the standalone backtest scripts in backend/scripts/ and captures
their stdout output for display in the app's Backtests page -- those scripts
were previously only runnable from a terminal, their results never
persisted or shown anywhere in the app (the page itself was a dead stub:
"land in Phase 7"). Captures stdout as-is rather than re-deriving each
script's numbers into a shared schema, since the scripts genuinely differ in
what they can honestly report (some are real "model beats de-vigged market"
go/no-go checks, others are calibration-only checks with no real market to
compare against -- see each script's own docstring) -- reformatting them
into one rigid schema risked losing that nuance or introducing a transcription
bug in already-validated numbers. The label/sport/summary below is metadata
ABOUT each script, not a re-implementation of its logic.
"""
import contextlib
import datetime
import importlib
import io
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"

# key -> (module filename without .py, sport, display label, one-line summary)
# Order matters for display -- NFL first (has real market-beating go/no-go
# checks), NBA/MLB after (calibration-only, no historical odds source
# exists for either -- see each script's own docstring).
BACKTESTS: list[tuple[str, str, str, str]] = [
    ("backtest_moneyline", "nfl", "Moneyline", "Elo baseline vs. de-vigged market closing moneyline"),
    ("backtest_moneyline_gbm", "nfl", "Moneyline (regularized logistic regression)", "Engineered-feature logistic regression vs. plain Elo, walk-forward by season"),
    ("backtest_spread", "nfl", "Spread", "Elo margin model vs. de-vigged market closing spread"),
    ("backtest_totals", "nfl", "Totals", "Elo/scoring totals model vs. de-vigged market closing total"),
    ("backtest_team_total", "nfl", "Team Total", "Calibration + blend-vs-naive check only -- no historical team-total market line exists to compare against"),
    ("backtest_roster_change", "nfl", "Roster-change adjustment", "Elo+adjustment vs. Elo alone, both vs. real market moneyline -- small sample by nature (real starter changes are rare)"),
    ("backtest_moneyline_nba", "nba", "Moneyline", "Elo vs. flat home-win-rate baseline -- calibration/skill check only, no free historical NBA odds source exists"),
    ("backtest_spread_nba", "nba", "Spread", "Margin model calibration -- no free historical NBA odds source exists"),
    ("backtest_totals_nba", "nba", "Totals", "Blend-vs-naive calibration check -- no free historical NBA odds source exists"),
    ("backtest_moneyline_mlb", "mlb", "Moneyline", "Team-Elo vs. Elo+starting-pitcher-blend vs. flat home-win-rate -- calibration/skill check only, no free historical MLB odds source exists"),
    ("backtest_spread_mlb", "mlb", "Run Line", "Elo+pitcher margin model calibration -- no free historical MLB odds source exists"),
    ("backtest_totals_mlb", "mlb", "Totals", "Flat/naive vs. team-scoring-blend ablation -- confirms the blend does NOT help for MLB (unlike NFL/NBA), calibration check only"),
    ("derive_mma_elo_constants", "mma", "Moneyline (Elo K derivation)", "Grid-searches K against walk-forward Brier on the full ufcstats.com history -- calibration/skill check only, no free historical UFC odds source exists"),
    ("backtest_mma_distance", "mma", "Went the Distance", "Logistic regression vs. naive base-rate baseline, walk-forward by year -- this app's flagship differentiator market, re-tested fresh here (see the script's own docstring for the earlier, separate research project's original finding)"),
    ("backtest_mma_method", "mma", "Method of Finish", "Multinomial logistic regression (KO/TKO vs. Submission vs. Decision) vs. naive base-rate baseline, walk-forward by year -- Brier beats baseline in 17/17 yearly folds"),
    ("backtest_mma_rounds", "mma", "Rounds (ends before round N)", "Round-of-finish multinomial vs. naive per-scheduled_rounds baseline -- weaker/noisier than distance or method-of-finish (13/17 yearly folds on the raw 5-way target), but the market-relevant summed ladder question is more robust (10-15/17 depending on rung, always net-positive)"),
    ("backtest_moneyline_tennis", "tennis", "Moneyline", "Surface-blended walk-forward Elo vs. de-vigged market closing price -- real historical odds exist at ALL THREE tiers (tour/challenger/ITF, tennis-data.co.uk + tennisexplorer.com), a genuinely new capability vs. this user's earlier standalone tennis research. Market beats the model at every tier -- NO-GO across the board."),
]


def _run_script(module_name: str) -> tuple[str, float]:
    """Imports scripts/{module_name}.py and calls its main(), capturing
    stdout. Scripts insert backend/ onto sys.path themselves at import time
    (idempotent), same as when run standalone from the CLI."""
    import sys

    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    module = importlib.import_module(module_name)
    importlib.reload(module)  # picks up code changes without a full process restart

    buf = io.StringIO()
    start = time.monotonic()
    with contextlib.redirect_stdout(buf):
        module.main()
    duration = time.monotonic() - start
    return buf.getvalue(), duration


def run_one(key: str) -> dict:
    entry = next((b for b in BACKTESTS if b[0] == key), None)
    if entry is None:
        raise KeyError(f"unknown backtest key: {key}")
    _, sport, label, summary = entry
    output, duration = _run_script(key)
    return {
        "key": key,
        "sport": sport,
        "label": label,
        "summary": summary,
        "output": output,
        "duration_sec": round(duration, 2),
        "run_at": datetime.datetime.utcnow().isoformat(),
    }


def run_all() -> list[dict]:
    return [run_one(key) for key, _, _, _ in BACKTESTS]

from fastapi import APIRouter, HTTPException

from app.models import backtest_runner, backtest_store

router = APIRouter(prefix="/backtests", tags=["backtests"])


@router.get("")
def list_backtests():
    """Returns metadata for every known backtest plus its last persisted
    result (if it's ever been run) -- results are cached, not re-run on
    every page load, since several of these take 10-20+ seconds."""
    stored = backtest_store.load_all()
    return [
        {
            "key": key,
            "sport": sport,
            "label": label,
            "summary": summary,
            "result": stored.get(key),
        }
        for key, sport, label, summary in backtest_runner.BACKTESTS
    ]


@router.post("/run")
def run_all_backtests():
    """Runs every backtest script sequentially (~45s total across all 9 --
    NFL's roster-change and moneyline-GBM checks are the slow ones) and
    persists the results. Deliberately synchronous: this is a rarely-used
    manual action (mirrors the existing "trigger an immediate data refresh"
    button), not something hit on a schedule."""
    results = backtest_runner.run_all()
    backtest_store.save_results(results)
    return results


@router.post("/run/{key}")
def run_one_backtest(key: str):
    try:
        result = backtest_runner.run_one(key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown backtest key: {key}")
    backtest_store.save_results([result])
    return result

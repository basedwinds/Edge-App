"""Tiny JSON-file persistence for backtest_runner.py results -- these run on
demand (some take 10-20s, too slow for a page-load call) and need to survive
an app restart, but don't warrant a DB table for what's fundamentally a
cached blob of stdout text per script."""
import json
from pathlib import Path

from app.config import settings

_STORE_PATH = Path(settings.data_dir) / "backtest_results.json"


def load_all() -> dict[str, dict]:
    if not _STORE_PATH.exists():
        return {}
    try:
        return json.loads(_STORE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_results(results: list[dict]) -> None:
    existing = load_all()
    for r in results:
        existing[r["key"]] = r
    _STORE_PATH.write_text(json.dumps(existing, indent=2))

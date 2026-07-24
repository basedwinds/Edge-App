"""Real backtest of elo_lol.py's map-1 win prediction against ACTUAL Kalshi
market prices -- parallel to backtest_valorant_market_odds.py (same Map-1-
only scoping, see that script's own docstring for why), but LoL's own
estimated_start_time comes from Leaguepedia's real Cargo DateTime_UTC field
(not vlr.gg's rougher UTC-assumption guess -- see lol_data.py's own
docstring), so this should get a real pre-match price for a much larger
share of matched events than Valorant's own backtest did.

Same real live window as CS2/Valorant's own backtests: Kalshi's KXLOLMAP
settled-market history starts 2026-05-14.

Run: backend/.venv/Scripts/python.exe scripts/backtest_lol_market_odds.py
"""
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.ingestion.market_matcher_lol import team_names_match  # noqa: E402
from app.models.calibration import brier_score  # noqa: E402
from app.models.baseline.elo_lol import BASE_RATING, LolEloState, map_win_prob, update_ratings  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "lol_historical_match_cache.json"
BACKTEST_CACHE_PATH = DATA_DIR / "lol_market_odds_backtest_cache.json"

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES_TICKER = "KXLOLMAP"
_MAP_SUFFIX_RE = re.compile(r"^(.*)-(\d+)$")

_client = httpx.Client(timeout=30.0, headers={"User-Agent": "nfl-edge-app/0.1 (personal research project)"})


def _get_json_with_retry(url: str, params: dict, max_retries: int = 5) -> dict:
    for attempt in range(max_retries):
        resp = _client.get(url, params=params)
        if resp.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"still rate-limited after {max_retries} attempts: {url}")


def fetch_all_settled_markets() -> list[dict]:
    cursor = ""
    all_markets = []
    while True:
        params = {"series_ticker": SERIES_TICKER, "status": "settled", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        d = _get_json_with_retry(f"{KALSHI_BASE}/markets", params)
        markets = d.get("markets", [])
        all_markets.extend(markets)
        cursor = d.get("cursor", "")
        if not cursor or not markets:
            break
    return all_markets


def last_trade_price_before(ticker: str, cutoff_unix: int) -> float | None:
    d = _get_json_with_retry(f"{KALSHI_BASE}/markets/trades", {"ticker": ticker, "limit": 1, "max_ts": cutoff_unix})
    trades = d.get("trades", [])
    return float(trades[0]["yes_price_dollars"]) if trades else None


def load_historical_matches() -> list[dict]:
    rows = json.loads(MATCH_CACHE_PATH.read_text(encoding="utf-8"))
    rows = [r for r in rows if r.get("best_of") and r.get("winner")]
    rows.sort(key=lambda r: r.get("estimated_start_time") or r["match_date"])
    return rows


def main():
    print("Fetching all settled KXLOLMAP markets from Kalshi...")
    markets = fetch_all_settled_markets()
    print(f"  {len(markets)} settled market-rows")

    events: dict[tuple[str, int], list[dict]] = {}
    for m in markets:
        event_ticker = m["event_ticker"]
        prefix = f"{SERIES_TICKER}-"
        if not event_ticker.startswith(prefix):
            continue
        rest = event_ticker[len(prefix):]
        suffix_match = _MAP_SUFFIX_RE.match(rest)
        if not suffix_match:
            continue
        match_code, map_number = suffix_match.group(1), int(suffix_match.group(2))
        if map_number != 1:
            continue
        events.setdefault((match_code, map_number), []).append(m)
    print(f"  {len(events)} real Map-1 events found")

    print("Loading historical match cache for team/date matching + walk-forward training...")
    historical = load_historical_matches()

    resolved_events = []
    for (match_code, map_number), rows in events.items():
        if len(rows) != 2:
            continue
        team_names = {r.get("yes_sub_title", ""): r for r in rows}
        if len(team_names) != 2:
            continue
        winner_row = next((r for r in rows if r.get("result") == "yes"), None)
        if winner_row is None:
            continue
        team_a, team_b = list(team_names.keys())
        resolved_events.append({
            "match_code": match_code, "team_a": team_a, "team_b": team_b,
            "winner_team": winner_row.get("yes_sub_title"), "yes_ticker_a": team_names[team_a]["ticker"],
        })
    print(f"  {len(resolved_events)} events have exactly 2 real team rows + a real settled winner")

    matched = []
    for ev in resolved_events:
        for row in historical:
            if (team_names_match(ev["team_a"], row["team_a"]) and team_names_match(ev["team_b"], row["team_b"])) or \
               (team_names_match(ev["team_a"], row["team_b"]) and team_names_match(ev["team_b"], row["team_a"])):
                matched.append({**ev, "cache_row": row})
                break
    print(f"  {len(matched)}/{len(resolved_events)} events matched to a real historical match record")

    price_cache = json.loads(BACKTEST_CACHE_PATH.read_text(encoding="utf-8")) if BACKTEST_CACHE_PATH.exists() else {}
    for i, ev in enumerate(matched):
        key = f"{ev['match_code']}-1"
        if key in price_cache:
            continue
        real_start = ev["cache_row"]["estimated_start_time"]
        if not real_start:
            continue
        cutoff_unix = int(dt.datetime.fromisoformat(real_start.replace("Z", "+00:00")).timestamp())
        try:
            price = last_trade_price_before(ev["yes_ticker_a"], cutoff_unix)
        except Exception as e:
            print(f"  [{i+1}/{len(matched)}] FAILED: {e}")
            continue
        price_cache[key] = price
        if (i + 1) % 50 == 0:
            print(f"  fetched {i+1}/{len(matched)} closing prices...")
            BACKTEST_CACHE_PATH.write_text(json.dumps(price_cache), encoding="utf-8")
    BACKTEST_CACHE_PATH.write_text(json.dumps(price_cache), encoding="utf-8")

    with_price = [ev for ev in matched if price_cache.get(f"{ev['match_code']}-1") is not None]
    print(f"  {len(with_price)}/{len(matched)} events have a real pre-match closing price")

    backtest_by_source_id = {ev["cache_row"]["source_match_id"]: ev for ev in with_price}
    state = LolEloState()
    model_preds, market_preds, outcomes = [], [], []
    for m in historical:
        team_a, team_b, winner = m["team_a"], m["team_b"], m["winner"]
        a_r, b_r = state.get(team_a), state.get(team_b)

        bt = backtest_by_source_id.get(m["source_match_id"])
        if bt is not None:
            model_p_a = map_win_prob(a_r, b_r)
            actual_a = 1.0 if winner == "team_a" else 0.0
            market_p_team_a_side = price_cache[f"{bt['match_code']}-1"]
            market_p_a = market_p_team_a_side if team_names_match(bt["team_a"], team_a) else 1.0 - market_p_team_a_side
            model_preds.append(model_p_a)
            market_preds.append(market_p_a)
            outcomes.append(actual_a)

        update_ratings(state, team_a, team_b, winner)

    print(f"\n{len(outcomes)} real backtest Map-1s with both a model prediction and a real market closing price")
    if outcomes:
        model_brier = brier_score(model_preds, outcomes)
        market_brier = brier_score(market_preds, outcomes)
        model_acc = sum(1 for p, o in zip(model_preds, outcomes) if (p >= 0.5) == (o >= 0.5)) / len(outcomes)
        market_acc = sum(1 for p, o in zip(market_preds, outcomes) if (p >= 0.5) == (o >= 0.5)) / len(outcomes)
        print(f"Model  Brier: {model_brier:.5f}  accuracy: {model_acc:.4f}")
        print(f"Market Brier: {market_brier:.5f}  accuracy: {market_acc:.4f}")
        print(f"\n{'MODEL BEATS MARKET' if model_brier < market_brier else 'MARKET BEATS MODEL'} on this real, {len(outcomes)}-match sample.")


if __name__ == "__main__":
    main()

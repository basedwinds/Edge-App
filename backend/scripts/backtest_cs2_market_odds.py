"""Real backtest of elo_cs2.py's series-winner prediction against ACTUAL
Kalshi market prices -- the missing piece every other CS2 script so far
only measured internal walk-forward accuracy against. This is a genuinely
new capability for this app's esports build: Kalshi exposes real historical
trade-level data (confirmed live 2026-07-20, via /trade-api/v2/markets/trades
with a max_ts filter) for ANY market including already-settled ones, so a
"closing line"-style comparison IS possible here, unlike this app's other
sports' esports titles where "no historical odds archive exists" was
assumed and never actually checked against Kalshi's own trade history.

Real, confirmed live window: KXCS2GAME (series winner) has 2,582 settled
market-rows (~1,291 real matches) from 2026-05-14 through 2026-07-20 -- ~2
months, not the multi-year window this app's own historical Elo crawl
covers, but Kalshi's esports markets themselves are simply that new (every
title's settled-market history starts on the exact same date, confirmed
live, strongly suggesting a real platform-wide esports launch/scale-up
around 2026-05-14, not a per-title coincidence).

Methodology: for each settled KXCS2GAME event (2 markets, one per team's own
YES side), takes the market's own LAST REAL TRADE before its occurrence_datetime
(the scheduled match start) as the "closing" market price -- same real
"informed price right before the game" concept as this app's own CLV system
(app/models/clv.py), just applied in bulk across history instead of one
placed bet at a time. Matches the event's own team names against
data/cs2_historical_match_cache.json (already covers this exact window,
since that crawl runs through mid-2026) to get the real match record, then
computes elo_cs2.py's own prediction using ratings trained WALK-FORWARD up
to (not including) that match -- same no-lookahead discipline the K grid
search already uses, just now also recording the market's own real price
at each step for a head-to-head Brier comparison.

Run: backend/.venv/Scripts/python.exe scripts/backtest_cs2_market_odds.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.ingestion.market_matcher_cs2 import team_names_match  # noqa: E402
from app.models.calibration import brier_score, log_loss  # noqa: E402
from app.models.baseline.elo_cs2 import BASE_RATING, Cs2EloState, K, map_win_prob, series_score_distribution, update_ratings  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "cs2_historical_match_cache.json"
BACKTEST_CACHE_PATH = DATA_DIR / "cs2_market_odds_backtest_cache.json"

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES_TICKER = "KXCS2GAME"

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


def group_into_events(markets: list[dict]) -> dict[str, list[dict]]:
    events: dict[str, list[dict]] = {}
    for m in markets:
        events.setdefault(m["event_ticker"], []).append(m)
    return events


def last_trade_price_before(ticker: str, cutoff_unix: int) -> float | None:
    """Real "closing line" price for this market's own YES side -- the most
    recent real trade before the match's own scheduled start, same concept
    as app/models/clv.py's own closing-price lookup."""
    d = _get_json_with_retry(f"{KALSHI_BASE}/markets/trades", {"ticker": ticker, "limit": 1, "max_ts": cutoff_unix})
    trades = d.get("trades", [])
    if not trades:
        return None
    return float(trades[0]["yes_price_dollars"])


def load_historical_matches() -> list[dict]:
    rows = json.loads(MATCH_CACHE_PATH.read_text(encoding="utf-8"))
    rows = [r for r in rows if r.get("best_of") and r.get("winner")]
    rows.sort(key=lambda r: r.get("estimated_start_time") or r["match_date"])
    return rows


def match_event_to_cache(team_a: str, team_b: str, occurrence_iso: str, cache_by_date: dict[str, list[dict]]) -> dict | None:
    """Matches a Kalshi event's own 2 real team names against the historical
    cache's rows on the SAME real calendar date (+/- 1 day for timezone
    slack, same tolerance category as this app's other cross-source date
    joins) -- exact-normalized team match, same discipline as
    market_matcher_cs2.py's own live-matching code."""
    import datetime as dt
    base_date = occurrence_iso[:10]
    candidate_dates = [base_date]
    try:
        d = dt.date.fromisoformat(base_date)
        candidate_dates += [(d + dt.timedelta(days=delta)).isoformat() for delta in (-1, 1)]
    except ValueError:
        pass
    for date in candidate_dates:
        for row in cache_by_date.get(date, []):
            if (team_names_match(team_a, row["team_a"]) and team_names_match(team_b, row["team_b"])) or \
               (team_names_match(team_a, row["team_b"]) and team_names_match(team_b, row["team_a"])):
                return row
    return None


def main():
    print("Fetching all settled KXCS2GAME markets from Kalshi...")
    markets = fetch_all_settled_markets()
    events = group_into_events(markets)
    print(f"  {len(markets)} settled market-rows across {len(events)} real events")

    print("Loading historical match cache for team/date matching + walk-forward training...")
    historical = load_historical_matches()
    cache_by_date: dict[str, list[dict]] = {}
    for row in historical:
        cache_by_date.setdefault(row["match_date"], []).append(row)

    # Resolve each Kalshi event to (team_a, team_b, occurrence_datetime,
    # real_winner_side) BEFORE touching the network for trade history, so a
    # failed/slow trade lookup doesn't waste a repeated event-resolution pass.
    resolved_events = []
    for event_ticker, rows in events.items():
        if len(rows) != 2:
            continue
        team_names = {r.get("yes_sub_title", ""): r for r in rows}
        if len(team_names) != 2:
            continue
        occurrence = rows[0].get("occurrence_datetime")
        if not occurrence:
            continue
        winner_row = next((r for r in rows if r.get("result") == "yes"), None)
        if winner_row is None:
            continue
        team_a, team_b = list(team_names.keys())
        resolved_events.append({
            "event_ticker": event_ticker, "team_a": team_a, "team_b": team_b,
            "occurrence_datetime": occurrence, "winner_team": winner_row.get("yes_sub_title"),
            "yes_ticker_a": team_names[team_a]["ticker"],
        })

    print(f"  {len(resolved_events)} events have exactly 2 real team rows + a real settled winner")

    matched = []
    for i, ev in enumerate(resolved_events):
        cache_row = match_event_to_cache(ev["team_a"], ev["team_b"], ev["occurrence_datetime"], cache_by_date)
        if cache_row is not None:
            matched.append({**ev, "cache_row": cache_row})
    print(f"  {len(matched)}/{len(resolved_events)} events matched to a real historical match record")

    # Fetch each matched event's real pre-match closing price (cached to
    # disk -- these are network calls, one per event, worth checkpointing).
    #
    # REAL BUG fixed here (found live 2026-07-20 -- the first run of this
    # script produced an implausible 98.4% "market accuracy"/0.01434 Brier,
    # near-perfect, which was the tell): Kalshi's own occurrence_datetime is
    # NOT reliable as a "before the match" cutoff -- confirmed live, a real
    # settled market's own close_time (12:19:31) was almost 3 HOURS BEFORE
    # its own occurrence_datetime (15:00:00), meaning the match had already
    # fully resolved and the market had already closed well before the
    # "scheduled" time. Using occurrence_datetime as max_ts therefore let
    # trades all the way up to the moment of SETTLEMENT through the filter
    # -- comparing the model against the market's own near-certain final
    # price, not a genuine pre-match closing line. Fixed by using this
    # match's own REAL start time from the historical cache instead
    # (Liquipedia's own confirmed-accurate timer widget, see cs2_data.py),
    # which is independent of Kalshi's own scheduling accuracy entirely.
    price_cache = json.loads(BACKTEST_CACHE_PATH.read_text(encoding="utf-8")) if BACKTEST_CACHE_PATH.exists() else {}
    import datetime as dt
    for i, ev in enumerate(matched):
        if ev["event_ticker"] in price_cache:
            continue
        real_start = ev["cache_row"]["estimated_start_time"]
        cutoff_unix = int(dt.datetime.fromisoformat(real_start.replace("Z", "+00:00")).timestamp())
        try:
            price = last_trade_price_before(ev["yes_ticker_a"], cutoff_unix)
        except Exception as e:
            print(f"  [{i+1}/{len(matched)}] FAILED fetching trade price: {e}")
            continue
        price_cache[ev["event_ticker"]] = price
        if (i + 1) % 50 == 0:
            print(f"  fetched {i+1}/{len(matched)} closing prices...")
            BACKTEST_CACHE_PATH.write_text(json.dumps(price_cache), encoding="utf-8")
    BACKTEST_CACHE_PATH.write_text(json.dumps(price_cache), encoding="utf-8")

    with_price = [ev for ev in matched if price_cache.get(ev["event_ticker"]) is not None]
    print(f"  {len(with_price)}/{len(matched)} events have a real pre-match closing price")

    # Walk-forward train Elo through the FULL historical dataset in
    # chronological order, recording model-vs-market for each backtest event
    # the moment its own match is reached (no lookahead -- ratings at that
    # point reflect only STRICTLY EARLIER real matches).
    backtest_by_source_id = {ev["cache_row"]["source_match_id"]: ev for ev in with_price}
    state = Cs2EloState()
    model_preds, market_preds, outcomes = [], [], []
    for m in historical:
        team_a, team_b, best_of, winner = m["team_a"], m["team_b"], m["best_of"], m["winner"]
        a_r, b_r = state.get(team_a), state.get(team_b)

        bt = backtest_by_source_id.get(m["source_match_id"])
        if bt is not None:
            map_p = map_win_prob(a_r, b_r)
            dist = series_score_distribution(map_p, best_of)
            model_p_a = sum(p for (a, b), p in dist.items() if a > b)
            actual_a = 1.0 if winner == "team_a" else 0.0
            # Kalshi's own event team_a might be EITHER side of our cache's
            # team_a/team_b -- reorient the market price to "prob team_a
            # (this cache row's team_a) wins" before comparing.
            market_p_team_a_side = price_cache[bt["event_ticker"]]
            if team_names_match(bt["team_a"], team_a):
                market_p_a = market_p_team_a_side
            else:
                market_p_a = 1.0 - market_p_team_a_side
            model_preds.append(model_p_a)
            market_preds.append(market_p_a)
            outcomes.append(actual_a)

        update_ratings(state, team_a, team_b, winner)

    print(f"\n{len(outcomes)} real backtest matches with both a model prediction and a real market closing price")
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

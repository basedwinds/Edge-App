"""Backtests the WNBA baseline Elo vs real Kalshi KXWNBAGAME closing prices
(task #40) -- the measured-edge number that decides whether WNBA is worth
betting + its bankroll share.

Closing price = the last real trade STRICTLY BEFORE the game's real ESPN start
time (re-scraped here) -- NOT Kalshi's own occurrence_datetime, which for WNBA
is post-game (~05:00 UTC) and would leak live/near-settlement trades, the exact
tell that inflated the first CS2 backtest to a fake 98%.
"""
import json
import sys
import time
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from app.models.calibration import brier_score  # noqa: E402
import scripts.derive_wnba_elo as W  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0"}
KB = "https://api.elections.kalshi.com/trade-api/v2"
SB = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
PRICE_CACHE = DATA_DIR / "wnba_market_backtest_prices.json"

CITY = {"Atlanta": "ATL", "Chicago": "CHI", "Connecticut": "CON", "Dallas": "DAL",
        "Golden State": "GS", "Indiana": "IND", "Las Vegas": "LV", "Los Angeles": "LA",
        "Minnesota": "MIN", "New York": "NY", "Phoenix": "PHX", "Portland": "POR",
        "Seattle": "SEA", "Toronto": "TOR", "Washington": "WSH"}

_c = httpx.Client(timeout=30.0, headers=UA)


def get_json(url, params, tries=5):
    for a in range(tries):
        r = _c.get(url, params=params)
        if r.status_code == 429:
            time.sleep(2 * (a + 1)); continue
        r.raise_for_status(); return r.json()
    raise RuntimeError("rate-limited")


def settled_events():
    cursor, mkts = "", []
    while True:
        p = {"series_ticker": "KXWNBAGAME", "status": "settled", "limit": 200}
        if cursor: p["cursor"] = cursor
        d = get_json(f"{KB}/markets", p)
        mkts += d.get("markets", []); cursor = d.get("cursor", "")
        if not cursor or not d.get("markets"): break
    ev = {}
    for m in mkts:
        ev.setdefault(m["event_ticker"], []).append(m)
    out = []
    for et, rows in ev.items():
        if len(rows) != 2: continue
        occ = rows[0].get("occurrence_datetime")
        win = next((r for r in rows if r.get("result") == "yes"), None)
        if not occ or win is None: continue
        a = rows[0]
        out.append({"date": occ[:10], "ca": a.get("yes_sub_title"), "cb": rows[1].get("yes_sub_title"),
                    "winner_city": win.get("yes_sub_title"), "yes_ticker": a["ticker"], "yes_city": a.get("yes_sub_title")})
    return out


def espn_start_times(dates):
    """{(date, frozenset(abbr pair)): start_iso} for the needed dates."""
    out = {}
    for day in sorted(dates):
        try:
            d = get_json(SB, {"dates": day.replace("-", "")})
        except Exception:
            continue
        for e in d.get("events", []):
            comp = e.get("competitions", [{}])[0]
            cs = comp.get("competitors", [])
            if len(cs) != 2: continue
            ab = frozenset(c["team"]["abbreviation"] for c in cs)
            out[(day, ab)] = e.get("date")
        time.sleep(0.2)
    return out


def main():
    evs = settled_events()
    print(f"{len(evs)} settled KXWNBAGAME events")
    # map cities -> abbrevs, keep only fully-mapped real-team games
    for e in evs:
        e["a"] = CITY.get(e["ca"]); e["b"] = CITY.get(e["cb"])
    evs = [e for e in evs if e["a"] and e["b"]]
    starts = espn_start_times({e["date"] for e in evs})
    for e in evs:
        # match date +/-1 day to the start-time map
        e["start"] = None
        for dd in (e["date"], _shift(e["date"], -1), _shift(e["date"], 1)):
            k = (dd, frozenset((e["a"], e["b"])))
            if k in starts:
                e["start"] = starts[k]; e["cache_date"] = dd; break
    evs = [e for e in evs if e["start"]]
    print(f"{len(evs)} matched to a real ESPN start time")

    prices = json.loads(PRICE_CACHE.read_text(encoding="utf-8")) if PRICE_CACHE.exists() else {}
    for i, e in enumerate(evs):
        if e["yes_ticker"] in prices: continue
        cutoff = int(dt.datetime.fromisoformat(e["start"].replace("Z", "+00:00")).timestamp())
        try:
            d = get_json(f"{KB}/markets/trades", {"ticker": e["yes_ticker"], "limit": 1, "max_ts": cutoff})
            t = d.get("trades", [])
            prices[e["yes_ticker"]] = float(t[0]["yes_price_dollars"]) if t else None
        except Exception:
            prices[e["yes_ticker"]] = None
        if (i + 1) % 40 == 0:
            PRICE_CACHE.write_text(json.dumps(prices), encoding="utf-8")
        time.sleep(0.1)
    PRICE_CACHE.write_text(json.dumps(prices), encoding="utf-8")
    priced = [e for e in evs if prices.get(e["yes_ticker"]) is not None]
    print(f"{len(priced)} events have a real pre-tip closing price")

    # walk-forward WNBA Elo, score matched games
    rows = W.load()
    _, adv = W.measure_home_adv(rows)
    bt = {}
    for e in priced:
        bt[(e["cache_date"], frozenset((e["a"], e["b"])))] = e
    r = {}; cur = None
    model_p, market_p, outs = [], [], []
    for g in rows:
        if g["season"] != cur:
            cur = g["season"]
            for t in r: r[t] = W.BASE + (1 - W.SEASON_REGRESSION) * (r[t] - W.BASE)
        h, a = g["home"], g["away"]
        hr, ar = r.get(h, W.BASE), r.get(a, W.BASE)
        key = (g["date"], frozenset((h, a)))
        e = bt.get(key)
        if e is not None:
            hadv = 0.0 if g["neutral"] else adv
            p_home = W.win_prob(hr, ar, hadv)
            actual_home = 1.0 if g["home_score"] > g["away_score"] else 0.0
            mp = prices[e["yes_ticker"]]
            # reorient the yes-side price to the home team's perspective
            mkt_home = mp if CITY.get(e["yes_city"]) == h else (1 - mp)
            model_p.append(p_home); market_p.append(mkt_home); outs.append(actual_home)
        delta = 32.0 * ((1.0 if g["home_score"] > g["away_score"] else 0.0) - W.win_prob(hr, ar, 0.0 if g["neutral"] else adv))
        r[h] = hr + delta; r[a] = ar - delta

    n = len(outs)
    print(f"\n{n} backtest games with model + real closing price")
    if n:
        mb, kb = brier_score(model_p, outs), brier_score(market_p, outs)
        macc = sum(1 for p, o in zip(model_p, outs) if (p >= .5) == (o >= .5)) / n
        kacc = sum(1 for p, o in zip(market_p, outs) if (p >= .5) == (o >= .5)) / n
        print(f"MODEL  Brier {mb:.5f}  acc {macc:.3f}")
        print(f"MARKET Brier {kb:.5f}  acc {kacc:.3f}")
        print(f"\n{'MODEL BEATS MARKET' if mb < kb else 'MARKET BEATS MODEL'} (gap {mb-kb:+.5f})")


def _shift(d, n):
    return (dt.date.fromisoformat(d) + dt.timedelta(days=n)).isoformat()


if __name__ == "__main__":
    main()

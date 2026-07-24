"""Backtests the racing engine's field win probabilities vs real Kalshi race-
winner closing prices (F1: KXF1RACE, IndyCar: KXINDYCARRACE, NASCAR:
KXNASCARRACE). Produces the measured-edge read for the racing engine.

Closing price = last real trade STRICTLY BEFORE the real ESPN green-flag time
(from the results cache), per driver. Model prob = the walk-forward Bradley-
Terry field win prob using only pre-race ratings (no leakage). Both are scored
by Brier against did-win over every driver-observation, and also compared on
the harder question: how often each ranks the actual winner #1.

Only the last ~2 months of settled markets are retained by Kalshi, so in-season
series (F1/IndyCar/NASCAR right now) give a small but real sample.
"""
import json
import sys
import time
import datetime as dt
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import scripts.racing_engine as E  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0"}
KB = "https://api.elections.kalshi.com/trade-api/v2"
DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
SERIES = {"f1": "KXF1RACE", "irl": "KXINDYCARRACE", "nascar": "KXNASCARRACE"}

_c = httpx.Client(timeout=30.0, headers=UA)


def get(url, params, tries=5):
    for a in range(tries):
        r = _c.get(url, params=params)
        if r.status_code == 429:
            time.sleep(2 * (a + 1)); continue
        r.raise_for_status(); return r.json()
    raise RuntimeError("rate-limited")


def norm(name):
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    n = n.lower().replace(".", "").replace("-", " ")
    for suf in (" jr", " sr", " iii", " ii"):
        if n.endswith(suf):
            n = n[: -len(suf)]
    return n.strip().split()[-1]  # last name


def settled(series):
    cur, ms = "", []
    while True:
        p = {"series_ticker": series, "status": "settled", "limit": 500}
        if cur:
            p["cursor"] = cur
        d = get(f"{KB}/markets", p)
        ms += d.get("markets", []); cur = d.get("cursor", "")
        if not cur or not d.get("markets"):
            break
    ev = {}
    for m in ms:
        occ = m.get("occurrence_datetime")
        if not occ:
            continue
        ev.setdefault(m["event_ticker"], {"date": occ[:10], "drivers": []})
        ev[m["event_ticker"]]["drivers"].append(
            {"name": m.get("yes_sub_title"), "ticker": m["ticker"], "won": m.get("result") == "yes"})
    return ev


def main():
    league = sys.argv[1] if len(sys.argv) > 1 else "f1"
    series = SERIES[league]
    races = E.load(league)
    by_date = {}
    for r in races:
        by_date.setdefault((r["date"] or "")[:10], r)

    ev = settled(series)
    print(f"{series}: {len(ev)} settled race events")
    # match each Kalshi event to an ESPN race by date
    matched = []
    for et, info in ev.items():
        r = by_date.get(info["date"])
        if r:
            matched.append((et, info, r))
    print(f"{len(matched)} matched to an ESPN race")

    # closing prices per driver ticker (last trade before ESPN green flag)
    pc_path = DATA_DIR / f"racing_{league}_backtest_prices.json"
    prices = json.loads(pc_path.read_text(encoding="utf-8")) if pc_path.exists() else {}
    for et, info, r in matched:
        cutoff = int(dt.datetime.fromisoformat(r["date"].replace("Z", "+00:00")).timestamp())
        for d in info["drivers"]:
            if d["ticker"] in prices:
                continue
            try:
                t = get(f"{KB}/markets/trades", {"ticker": d["ticker"], "limit": 1, "max_ts": cutoff})
                tr = t.get("trades", [])
                prices[d["ticker"]] = float(tr[0]["yes_price_dollars"]) if tr else None
            except Exception:
                prices[d["ticker"]] = None
            time.sleep(0.05)
    pc_path.write_text(json.dumps(prices), encoding="utf-8")

    # walk-forward the engine; capture pre-race field probs for the matched race ids
    target_ids = {r["id"] for _, _, r in matched}
    pre_probs = {}
    rr = {}; cur = None; seen = 0
    for race in races:
        if race["season"] != cur:
            cur = race["season"]
            for d in rr:
                rr[d] = E.BASE + (1 - E.SEASON_REGRESSION) * (rr[d] - E.BASE)
        field = [x["driver_id"] for x in race["results"]]
        ratings = {d: rr.get(d, E.BASE) for d in field}
        if race["id"] in target_ids and seen >= 10:
            pre_probs[race["id"]] = (E.field_win_probs(ratings),
                                     {x["driver_id"]: x["driver"] for x in race["results"]})
        order = {x["driver_id"]: x["order"] for x in race["results"]}
        n = len(field); delta = {d: 0.0 for d in field}
        for i in field:
            for j in field:
                if i != j:
                    delta[i] += (1.0 if order[i] < order[j] else 0.0) - E._logistic(ratings[i] - ratings[j])
        for d in field:
            rr[d] = ratings[d] + (40.0 / (n - 1)) * delta[d]
        seen += 1

    # join + score
    model_p, market_p, outs = [], [], []
    m_hit = k_hit = races_scored = 0
    for et, info, r in matched:
        if r["id"] not in pre_probs:
            continue
        probs, idname = pre_probs[r["id"]]
        model_by_last = {norm(idname[d]): p for d, p in probs.items()}
        race_rows = []
        for d in info["drivers"]:
            mp = prices.get(d["ticker"])
            key = norm(d["name"])
            if mp is None or key not in model_by_last:
                continue
            race_rows.append((model_by_last[key], mp, 1.0 if d["won"] else 0.0, d["name"]))
        if not race_rows:
            continue
        for mo, mk, o, _ in race_rows:
            model_p.append(mo); market_p.append(mk); outs.append(o)
        races_scored += 1
        m_top = max(race_rows, key=lambda x: x[0])
        k_top = max(race_rows, key=lambda x: x[1])
        m_hit += m_top[2] == 1.0
        k_hit += k_top[2] == 1.0

    n = len(outs)
    print(f"\n{races_scored} races, {n} driver-observations with model + real closing price")
    if n:
        mb, kb = E.brier(model_p, outs), E.brier(market_p, outs)
        print(f"MODEL  Brier {mb:.5f}  | top-pick won {m_hit}/{races_scored}")
        print(f"MARKET Brier {kb:.5f}  | top-pick won {k_hit}/{races_scored}")
        print(f"\n{'MODEL BEATS MARKET' if mb < kb else 'MARKET BEATS MODEL'} (gap {mb-kb:+.5f})")


if __name__ == "__main__":
    main()

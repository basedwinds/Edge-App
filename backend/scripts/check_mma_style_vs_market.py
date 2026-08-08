"""Does the MMA style block move predictions TOWARD the market, or AWAY from it?

THE QUESTION THIS ANSWERS, and why it is not the same as the accuracy test.
check_mma_style_features.py established that style features beat production Elo
on held-out fights (log loss 0.67641 -> 0.66170). That says the model got more
accurate. It does NOT say there is an edge, because the market may already price
exactly the same information -- in which case style makes us better at agreeing
with a price we cannot beat.

The two cases have opposite consequences and are distinguishable without waiting
for results:

  TOWARD the market  -> style is recovering information the market already has.
                        Good for accuracy, WORSE for edge: it shrinks the
                        disagreements the app actually bets on, and the ones that
                        survive are the ones style could not explain.
  AWAY from the market -> style is claiming information the market lacks. That is
                        either a real edge or noise, and only settled results can
                        say which -- but it is at least the shape of an edge.

WHY NOT JUST BACKTEST AGAINST HISTORICAL ODDS: there is no free historical UFC
moneyline archive (the same gap distance_service_mma documents for go-the-
distance markets). The app's own settled MMA bets are useless here for a subtler
reason -- there are only 35 with a stored market price, and they are a SELECTED
sample: the app only places a bet where the model already disagreed with the
market, so scoring on them measures the tail it chose to bet, not calibration
against the market as a whole.

So this measures direction on LIVE markets, where every fight is present rather
than only the ones we liked.

Vig is removed pairwise before comparing: a two-sided moneyline book prices to
more than 1.00, and leaving that in would make every model look like it
systematically disagrees with the market on the favourite's side.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sqlalchemy import func

from app.db.database import SessionLocal
from app.db.models import Market, MarketSnapshot, MmaFight
from app.ingestion import ufc_data
from app.models import mma_features
from app.models.baseline import elo_mma

BASE = elo_mma.BASE_RATING
K = elo_mma.K


def _f(v, d=0.0):
    return d if v is None else float(v)


def style_vec(a, b, as_of_age_a=None, as_of_age_b=None):
    """Same antisymmetric block as check_mma_style_features, built from two
    snapshot dicts rather than a historical feature row. Age is NOT included --
    production's Elo already applies an age correction structurally."""
    ko = _f(a.get("ko_win_rate")) * _f(b.get("ko_loss_rate")) - _f(b.get("ko_win_rate")) * _f(a.get("ko_loss_rate"))
    sub = _f(a.get("sub_win_rate")) * _f(b.get("sub_loss_rate")) - _f(b.get("sub_win_rate")) * _f(a.get("sub_loss_rate"))
    return [
        ko, sub,
        _f(a.get("avg_td_landed")) - _f(b.get("avg_td_landed")),
        _f(a.get("avg_sig_str_landed")) - _f(b.get("avg_sig_str_landed")),
        _f(a.get("finish_rate")) - _f(b.get("finish_rate")),
        _f(a.get("reach_in"), 71.0) - _f(b.get("reach_in"), 71.0),
        _f(a.get("experience")) - _f(b.get("experience")),
        _f(a.get("win_rate")) - _f(b.get("win_rate")),
    ]


def main() -> None:
    fights = ufc_data.load_fights()
    raw = ufc_data.load_raw_fight_rows()
    bios = ufc_data.load_fighter_bios()
    rows = mma_features.build_feature_rows(fights, raw, bios)
    by_id = {f["id"]: f for f in fights}

    # --- train both models on ALL history, walk-forward Elo -------------------
    elo: dict[str, float] = {}
    Xe, Xf, y = [], [], []
    for r in rows:
        f = by_id.get(r["fight_id"])
        if f is None or f.get("is_draw") or f.get("is_no_contest"):
            continue
        a, b, w = f["fighter_a_id"], f["fighter_b_id"], f.get("winner_id")
        if w not in (a, b):
            continue
        ra = elo.get(a, BASE) + elo_mma.age_adjustment_elo(r.get("a_age"))
        rb = elo.get(b, BASE) + elo_mma.age_adjustment_elo(r.get("b_age"))
        st = [
            _f(r["a_ko_win_rate"]) * _f(r["b_ko_loss_rate"]) - _f(r["b_ko_win_rate"]) * _f(r["a_ko_loss_rate"]),
            _f(r["a_sub_win_rate"]) * _f(r["b_sub_loss_rate"]) - _f(r["b_sub_win_rate"]) * _f(r["a_sub_loss_rate"]),
            _f(r["a_avg_td_landed"]) - _f(r["b_avg_td_landed"]),
            _f(r["a_avg_sig_str_landed"]) - _f(r["b_avg_sig_str_landed"]),
            _f(r["a_finish_rate"]) - _f(r["b_finish_rate"]),
            _f(r["a_reach_in"], 71.0) - _f(r["b_reach_in"], 71.0),
            _f(r["a_experience"]) - _f(r["b_experience"]),
            _f(r["a_win_rate"]) - _f(r["b_win_rate"]),
        ]
        lab = 1 if w == a else 0
        ed = ra - rb
        Xe += [[ed], [-ed]]
        Xf += [[ed] + st, [-ed] + [-v for v in st]]
        y += [lab, 1 - lab]
        pa = 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))
        sa = float(lab)
        elo[a] = elo.get(a, BASE) + K * (sa - pa)
        elo[b] = elo.get(b, BASE) + K * ((1 - sa) - (1 - pa))

    m_elo = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000)).fit(np.array(Xe), np.array(y))
    m_sty = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000)).fit(np.array(Xf), np.array(y))
    print(f"trained on {len(y)} oriented samples ({len(y)//2} fights)")

    snaps = mma_features.compute_current_snapshots(fights, raw, as_of=dt.date.today())
    for fid, bio in bios.items():
        if fid in snaps:
            snaps[fid]["reach_in"] = mma_features.parse_reach_inches(bio.get("reach"))

    # --- live markets ---------------------------------------------------------
    s = SessionLocal()
    sub = (s.query(MarketSnapshot.market_id, func.max(MarketSnapshot.ts).label("ts"))
           .group_by(MarketSnapshot.market_id).subquery())
    q = (s.query(Market, MarketSnapshot, MmaFight)
         .join(sub, sub.c.market_id == Market.id)
         .join(MarketSnapshot, (MarketSnapshot.market_id == Market.id) & (MarketSnapshot.ts == sub.c.ts))
         .join(MmaFight, MmaFight.id == Market.mma_fight_id)
         .filter(Market.sport == "mma", Market.market_type == "moneyline", Market.status == "active").all())

    # group the two sides of each fight so vig can be removed pairwise
    per_fight: dict = {}
    for mk, sn, ft in q:
        price = sn.last_price if sn.last_price else None
        if price is None or not (0 < price < 1):
            continue
        per_fight.setdefault(ft.id, {"fight": ft, "sides": {}})["sides"][(mk.team or "").strip()] = price

    toward = away = 0
    d_elo, d_sty, moves = [], [], []
    for fid, rec in per_fight.items():
        ft = rec["fight"]
        sides = rec["sides"]
        if len(sides) != 2:
            continue
        pa_raw = sides.get(ft.fighter_a_name)
        pb_raw = sides.get(ft.fighter_b_name)
        if pa_raw is None or pb_raw is None:
            continue
        tot = pa_raw + pb_raw
        if tot <= 0:
            continue
        mkt = pa_raw / tot                      # vig-free market P(fighter A)
        sa, sb = snaps.get(ft.fighter_a_id), snaps.get(ft.fighter_b_id)
        if not sa or not sb:
            continue
        ed = (elo.get(ft.fighter_a_id, BASE) - elo.get(ft.fighter_b_id, BASE))
        st = style_vec(sa, sb)
        p_elo = float(m_elo.predict_proba([[ed]])[0][1])
        p_sty = float(m_sty.predict_proba([[ed] + st])[0][1])
        d_elo.append(abs(p_elo - mkt))
        d_sty.append(abs(p_sty - mkt))
        moves.append(p_sty - p_elo)
        if abs(p_sty - mkt) < abs(p_elo - mkt):
            toward += 1
        else:
            away += 1

    n = len(d_elo)
    if not n:
        print("no live fights with both sides priced and both fighters snapshotted")
        return
    print(f"\nlive fights usable: {n}")
    print(f"mean |model - market|   Elo only : {np.mean(d_elo):.4f}")
    print(f"                        Elo+style: {np.mean(d_sty):.4f}")
    print(f"style moved TOWARD the market on {toward}/{n} fights ({toward/n*100:.0f}%), away on {away}")
    print(f"mean absolute shift from style: {np.mean(np.abs(moves)):.4f}")
    verdict = ("TOWARD -- style mostly recovers what the market already prices; "
               "expect BETTER accuracy but FEWER/smaller edges"
               if np.mean(d_sty) < np.mean(d_elo) else
               "AWAY -- style claims information the market lacks; shape of an edge, "
               "but only settled results can tell edge from noise")
    print(f"\nVERDICT: {verdict}")


if __name__ == "__main__":
    main()

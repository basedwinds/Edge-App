"""Does the DEFENCE stack move MMA predictions toward the market, or away from it?

This is the test that decides whether any of the MMA feature work should change
a price. check_mma_defence_features established the stack is more ACCURATE
(-1.63% log loss vs Elo, defence the biggest single block). Accuracy is not edge:
if the market already prices the same information, a better model just agrees
with a price it cannot beat.

THE PRIOR HERE IS BAD, and worth stating before the result. Strikes absorbed and
takedown defence are PUBLISHED headline UFC stats -- they sit on every fighter's
profile page and every bettor sees them. The style block, which is built from
less prominent numbers, already came back TOWARD the market on 11 of 13 live
fights. Defence being priced too is the expected outcome, not a surprise.

  TOWARD  -> the market already has it. Better accuracy, FEWER real edges. What
             it removes are disagreements that were OUR blind spot, which is
             still useful -- it stops the app betting into its own ignorance --
             but it is not new information.
  AWAY    -> the stack claims something the market lacks. Edge-shaped, though
             only settled results can separate edge from noise.

Vig removed pairwise: a two-sided book prices to more than 1.00, and leaving that
in would fake a systematic disagreement on every favourite.
"""
from __future__ import annotations

import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sqlalchemy import func

from check_mma_defence_features import (
    BASE, DEF_KEYS, K, OFF_KEYS, Roll, _f, fight_seconds,
)

from app.db.database import SessionLocal
from app.db.models import Market, MarketSnapshot, MmaFight
from app.ingestion import ufc_data
from app.models import mma_features
from app.models.baseline import elo_mma


def style_from_rows(fr, side_a=True):
    """The style block exactly as the training path builds it."""
    return [
        _f(fr["a_ko_win_rate"]) * _f(fr["b_ko_loss_rate"]) - _f(fr["b_ko_win_rate"]) * _f(fr["a_ko_loss_rate"]),
        _f(fr["a_sub_win_rate"]) * _f(fr["b_sub_loss_rate"]) - _f(fr["b_sub_win_rate"]) * _f(fr["a_sub_loss_rate"]),
        _f(fr["a_avg_td_landed"]) - _f(fr["b_avg_td_landed"]),
        _f(fr["a_avg_sig_str_landed"]) - _f(fr["b_avg_sig_str_landed"]),
        _f(fr["a_finish_rate"]) - _f(fr["b_finish_rate"]),
        _f(fr["a_reach_in"], 71.0) - _f(fr["b_reach_in"], 71.0),
        _f(fr["a_experience"]) - _f(fr["b_experience"]),
        _f(fr["a_win_rate"]) - _f(fr["b_win_rate"]),
    ]


def style_from_snaps(a, b, ra, rb):
    """Same block, from live snapshots. Key order must match style_from_rows."""
    return [
        _f(a.get("ko_win_rate")) * _f(b.get("ko_loss_rate")) - _f(b.get("ko_win_rate")) * _f(a.get("ko_loss_rate")),
        _f(a.get("sub_win_rate")) * _f(b.get("sub_loss_rate")) - _f(b.get("sub_win_rate")) * _f(a.get("sub_loss_rate")),
        _f(a.get("avg_td_landed")) - _f(b.get("avg_td_landed")),
        _f(a.get("avg_sig_str_landed")) - _f(b.get("avg_sig_str_landed")),
        _f(a.get("finish_rate")) - _f(b.get("finish_rate")),
        _f(ra, 71.0) - _f(rb, 71.0),
        _f(a.get("experience")) - _f(b.get("experience")),
        _f(a.get("win_rate")) - _f(b.get("win_rate")),
    ]


def main() -> None:
    fights = ufc_data.load_fights()
    raw = ufc_data.load_raw_fight_rows()
    bios = ufc_data.load_fighter_bios()
    rows = mma_features.build_feature_rows(fights, raw, bios)
    by_id = {f["id"]: f for f in fights}
    raw_by = {}
    for r in raw:
        fid = r["fight_url"].rstrip("/").rsplit("/", 1)[-1]
        raw_by[(fid, r["fighter_id"])] = r

    elo: dict[str, float] = {}
    roll: dict[str, Roll] = defaultdict(Roll)
    Xe, Xf, y = [], [], []

    for fr in rows:
        f = by_id.get(fr["fight_id"])
        if f is None or f.get("is_draw") or f.get("is_no_contest"):
            continue
        a, b, w = f["fighter_a_id"], f["fighter_b_id"], f.get("winner_id")
        ra_row, rb_row = raw_by.get((f["id"], a)), raw_by.get((f["id"], b))
        if w in (a, b) and ra_row and rb_row:
            sa, sb = roll[a].snap(), roll[b].snap()
            if sa and sb:
                ed = ((elo.get(a, BASE) + elo_mma.age_adjustment_elo(fr.get("a_age")))
                      - (elo.get(b, BASE) + elo_mma.age_adjustment_elo(fr.get("b_age"))))
                vec = ([ed] + style_from_rows(fr)
                       + [sa[k] - sb[k] for k in OFF_KEYS]
                       + [sa[k] - sb[k] for k in DEF_KEYS])
                lab = 1 if w == a else 0
                Xe += [[ed], [-ed]]
                Xf += [vec, [-v for v in vec]]
                y += [lab, 1 - lab]
        if ra_row and rb_row:
            secs = fight_seconds(ra_row)
            roll[a].add(ra_row, rb_row, secs)
            roll[b].add(rb_row, ra_row, secs)
        if w in (a, b):
            pa = 1.0 / (1.0 + 10 ** ((elo.get(b, BASE) - elo.get(a, BASE)) / 400.0))
            sa_ = 1.0 if w == a else 0.0
            elo[a] = elo.get(a, BASE) + K * (sa_ - pa)
            elo[b] = elo.get(b, BASE) + K * ((1 - sa_) - (1 - pa))

    m_elo = make_pipeline(StandardScaler(), LogisticRegression(max_iter=3000)).fit(np.array(Xe), np.array(y))
    m_full = make_pipeline(StandardScaler(), LogisticRegression(max_iter=3000)).fit(np.array(Xf), np.array(y))
    print(f"trained on {len(y)//2} fights")

    snaps = mma_features.compute_current_snapshots(fights, raw, as_of=dt.date.today())
    reach = {fid: mma_features.parse_reach_inches(b.get("reach")) for fid, b in bios.items()}

    s = SessionLocal()
    sub = (s.query(MarketSnapshot.market_id, func.max(MarketSnapshot.ts).label("ts"))
           .group_by(MarketSnapshot.market_id).subquery())
    q = (s.query(Market, MarketSnapshot, MmaFight)
         .join(sub, sub.c.market_id == Market.id)
         .join(MarketSnapshot, (MarketSnapshot.market_id == Market.id) & (MarketSnapshot.ts == sub.c.ts))
         .join(MmaFight, MmaFight.id == Market.mma_fight_id)
         .filter(Market.sport == "mma", Market.market_type == "moneyline", Market.status == "active").all())

    per_fight: dict = {}
    for mk, sn, ft in q:
        p = sn.last_price
        if p is None or not (0 < p < 1):
            continue
        per_fight.setdefault(ft.id, {"f": ft, "sides": {}})["sides"][(mk.team or "").strip()] = p

    toward = away = 0
    d_elo, d_full, shifts = [], [], []
    for rec in per_fight.values():
        ft, sides = rec["f"], rec["sides"]
        pa_raw, pb_raw = sides.get(ft.fighter_a_name), sides.get(ft.fighter_b_name)
        if pa_raw is None or pb_raw is None:
            continue
        tot = pa_raw + pb_raw
        if tot <= 0:
            continue
        mkt = pa_raw / tot
        a, b = ft.fighter_a_id, ft.fighter_b_id
        sa, sb = roll[a].snap(), roll[b].snap()
        na, nb = snaps.get(a), snaps.get(b)
        if not (sa and sb and na and nb):
            continue
        ed = elo.get(a, BASE) - elo.get(b, BASE)
        vec = ([ed] + style_from_snaps(na, nb, reach.get(a), reach.get(b))
               + [sa[k] - sb[k] for k in OFF_KEYS]
               + [sa[k] - sb[k] for k in DEF_KEYS])
        p_elo = float(m_elo.predict_proba([[ed]])[0][1])
        p_full = float(m_full.predict_proba([vec])[0][1])
        d_elo.append(abs(p_elo - mkt))
        d_full.append(abs(p_full - mkt))
        shifts.append(p_full - p_elo)
        toward += int(abs(p_full - mkt) < abs(p_elo - mkt))
        away += int(abs(p_full - mkt) >= abs(p_elo - mkt))

    n = len(d_elo)
    if not n:
        print("no live fights with both sides priced and both fighters rolled")
        return
    print(f"\nlive fights usable: {n}")
    print(f"mean |model - market|   Elo only      : {np.mean(d_elo):.4f}")
    print(f"                        Elo+style+def : {np.mean(d_full):.4f}")
    print(f"moved TOWARD the market on {toward}/{n} ({toward/n*100:.0f}%), away on {away}")
    print(f"mean absolute shift: {np.mean(np.abs(shifts)):.4f}")
    print("\nVERDICT:", "TOWARD -- already priced; better accuracy, fewer real edges"
          if np.mean(d_full) < np.mean(d_elo) else
          "AWAY -- claims information the market lacks; edge-shaped")


if __name__ == "__main__":
    main()

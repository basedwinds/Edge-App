"""Does DEFENCE add signal to MMA winner prediction, beyond Elo and style?

THE GAP THIS TESTS. mma_features computes what a fighter DOES -- avg_sig_str_landed,
avg_td_landed -- and nothing about what is done TO them. The raw ufcstats rows have
carried the other half all along and nobody has used it: every fight has exactly
two rows (verified: 8,780 of 8,780), so each fighter's opponent row gives strikes
absorbed, takedowns conceded, control time surrendered, knockdowns taken.

In MMA these are not decoration. "Takedown defence" and "strikes absorbed per
minute" are among the most quoted fighter attributes precisely because durability
and the ability to stay standing decide fights that raw output does not.

WHAT IS BUILT, per fighter, rolling and strictly PRE-fight:

  offence   sig_str_landed_pm, td_landed_p15, ctrl_pm, kd_pm, sub_att_p15,
            sig_str_accuracy, td_accuracy
  defence   sig_str_ABSORBED_pm, td_CONCEDED_p15, ctrl_CONCEDED_pm,
            kd_ABSORBED_pm, td_defence_rate, sig_str_defence_rate

Everything is rate-normalised by time in the cage, not per fight. A fighter who
absorbs 40 strikes in a 25-minute decision is not the same as one who absorbs 40
in a 90-second loss, and per-fight averages cannot tell them apart -- which is
also why finish-heavy records distort raw totals.

td_defence_rate and sig_str_defence_rate come from the OPPONENT's attempt counts
(1 - landed/attempted against you), which is the standard definition and is only
computable because both rows are present.

MEASURED AGAINST THE RIGHT BASELINE. Production Elo (K=72 + age adjustment, age
used for prediction only and never written back into a rating -- that exact bug
corrupted an earlier run of the style test and inflated its result). Blocks are
added cumulatively so each one's MARGINAL value is visible: Elo, then style, then
defence. Held out on a later slice, both corner assignments trained so
orientation bias cannot be learned.

HONEST PRIOR: style turned out worth only -0.64% once its baseline was fixed, and
this app has rejected many MMA features before. Defence is the most plausible
remaining signal, not a likely one.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, log_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from app.ingestion import ufc_data
from app.models import mma_features
from app.models.baseline import elo_mma

BASE, K = elo_mma.BASE_RATING, elo_mma.K
TRAIN_FRAC = 0.70
MIN_FIGHTS = 2          # need some history before a rate means anything
EPS = 1e-9


def _f(v, d=0.0):
    return d if v is None else float(v)


def _of(s):
    """'12 of 19' -> (12.0, 19.0). '---' / junk -> (0,0)."""
    try:
        a, b = str(s).split(" of ")
        return float(a), float(b)
    except Exception:
        return 0.0, 0.0


def _mmss(s):
    try:
        m, sec = str(s).split(":")
        return float(m) * 60 + float(sec)
    except Exception:
        return 0.0


def fight_seconds(row) -> float:
    """Total cage time: completed rounds + time into the final one."""
    try:
        rnd = int(float(row.get("round") or 1))
    except Exception:
        rnd = 1
    return max((rnd - 1) * 300.0 + _mmss(row.get("time")), 1.0)


class Roll:
    """Rolling career totals for one fighter. Rates are computed on demand so a
    fighter with two long decisions is not treated like one with two quick KOs."""

    def __init__(self):
        self.secs = 0.0
        self.n = 0
        self.ss_l = self.ss_a = 0.0        # sig strikes landed / attempted (own)
        self.ss_abs = self.ss_faced = 0.0  # absorbed / faced (opponent attempts)
        self.td_l = self.td_a = 0.0
        self.td_conc = self.td_faced = 0.0
        self.ctrl = self.ctrl_conc = 0.0
        self.kd = self.kd_abs = 0.0
        self.sub = 0.0

    def snap(self) -> dict | None:
        if self.n < MIN_FIGHTS or self.secs <= 0:
            return None
        mins = self.secs / 60.0
        p15 = self.secs / 900.0    # per 15 minutes, the usual takedown unit
        return {
            "ss_pm": self.ss_l / mins,
            "ss_abs_pm": self.ss_abs / mins,
            "ss_acc": self.ss_l / (self.ss_a + EPS),
            "ss_def": 1.0 - self.ss_abs / (self.ss_faced + EPS),
            "td_p15": self.td_l / (p15 + EPS),
            "td_conc_p15": self.td_conc / (p15 + EPS),
            "td_acc": self.td_l / (self.td_a + EPS),
            "td_def": 1.0 - self.td_conc / (self.td_faced + EPS),
            "ctrl_pm": self.ctrl / mins,
            "ctrl_conc_pm": self.ctrl_conc / mins,
            "kd_pm": self.kd / mins,
            "kd_abs_pm": self.kd_abs / mins,
            "sub_p15": self.sub / (p15 + EPS),
        }

    def add(self, own, opp, secs):
        self.secs += secs
        self.n += 1
        l, a = _of(own.get("Sig. str."));  self.ss_l += l; self.ss_a += a
        ol, oa = _of(opp.get("Sig. str.")); self.ss_abs += ol; self.ss_faced += oa
        tl, ta = _of(own.get("Td"));        self.td_l += tl; self.td_a += ta
        otl, ota = _of(opp.get("Td"));      self.td_conc += otl; self.td_faced += ota
        self.ctrl += _mmss(own.get("Ctrl"))
        self.ctrl_conc += _mmss(opp.get("Ctrl"))
        self.kd += _f(own.get("KD"))
        self.kd_abs += _f(opp.get("KD"))
        self.sub += _f(own.get("Sub. att"))


DEF_KEYS = ["ss_abs_pm", "ss_def", "td_conc_p15", "td_def", "ctrl_conc_pm", "kd_abs_pm"]
OFF_KEYS = ["ss_pm", "ss_acc", "td_p15", "td_acc", "ctrl_pm", "kd_pm", "sub_p15"]


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
    samples = []

    for fr in rows:
        f = by_id.get(fr["fight_id"])
        if f is None or f.get("is_draw") or f.get("is_no_contest"):
            continue
        a, b, w = f["fighter_a_id"], f["fighter_b_id"], f.get("winner_id")
        ra_row = raw_by.get((f["id"], a))
        rb_row = raw_by.get((f["id"], b))
        if w in (a, b) and ra_row and rb_row:
            sa, sb = roll[a].snap(), roll[b].snap()
            ea = elo.get(a, BASE) + elo_mma.age_adjustment_elo(fr.get("a_age"))
            eb = elo.get(b, BASE) + elo_mma.age_adjustment_elo(fr.get("b_age"))
            style = [
                _f(fr["a_ko_win_rate"]) * _f(fr["b_ko_loss_rate"]) - _f(fr["b_ko_win_rate"]) * _f(fr["a_ko_loss_rate"]),
                _f(fr["a_sub_win_rate"]) * _f(fr["b_sub_loss_rate"]) - _f(fr["b_sub_win_rate"]) * _f(fr["a_sub_loss_rate"]),
                _f(fr["a_avg_td_landed"]) - _f(fr["b_avg_td_landed"]),
                _f(fr["a_avg_sig_str_landed"]) - _f(fr["b_avg_sig_str_landed"]),
                _f(fr["a_finish_rate"]) - _f(fr["b_finish_rate"]),
                _f(fr["a_reach_in"], 71.0) - _f(fr["b_reach_in"], 71.0),
                _f(fr["a_experience"]) - _f(fr["b_experience"]),
                _f(fr["a_win_rate"]) - _f(fr["b_win_rate"]),
            ]
            if sa and sb:
                off = [sa[k] - sb[k] for k in OFF_KEYS]
                dfn = [sa[k] - sb[k] for k in DEF_KEYS]
                # A's offence against B's matching weakness -- the same cross
                # logic the style block uses, now with real rate stats.
                cross = [
                    sa["ss_pm"] * (1.0 - sb["ss_def"]) - sb["ss_pm"] * (1.0 - sa["ss_def"]),
                    sa["td_p15"] * (1.0 - sb["td_def"]) - sb["td_p15"] * (1.0 - sa["td_def"]),
                    sa["kd_pm"] * sb["kd_abs_pm"] - sb["kd_pm"] * sa["kd_abs_pm"],
                ]
                samples.append((ea - eb, style, off, dfn, cross, 1 if w == a else 0))

        # ---- update AFTER recording (walk-forward) --------------------------
        if ra_row and rb_row:
            secs = fight_seconds(ra_row)
            roll[a].add(ra_row, rb_row, secs)
            roll[b].add(rb_row, ra_row, secs)
        if w in (a, b):
            pa = 1.0 / (1.0 + 10 ** ((elo.get(b, BASE) - elo.get(a, BASE)) / 400.0))
            sa_ = 1.0 if w == a else 0.0
            elo[a] = elo.get(a, BASE) + K * (sa_ - pa)
            elo[b] = elo.get(b, BASE) + K * ((1 - sa_) - (1 - pa))

    split = int(len(samples) * TRAIN_FRAC)
    print(f"fights usable (both sides with >={MIN_FIGHTS} prior fights): {len(samples)}  holdout={len(samples)-split}")

    MODES = [
        ("Elo only", lambda s: [s[0]]),
        ("+ style", lambda s: [s[0]] + s[1]),
        ("+ offence rates", lambda s: [s[0]] + s[1] + s[2]),
        ("+ DEFENCE", lambda s: [s[0]] + s[1] + s[2] + s[3]),
        ("+ off x def cross", lambda s: [s[0]] + s[1] + s[2] + s[3] + s[4]),
        ("Elo + defence only", lambda s: [s[0]] + s[3]),
    ]

    def build(sl, fn):
        X, y = [], []
        for s in sl:
            v = fn(s)
            X += [v, [-x for x in v]]
            y += [s[5], 1 - s[5]]
        return np.array(X), np.array(y)

    print(f"\n{'model':24s} {'log loss':>10s} {'accuracy':>9s} {'vs Elo':>9s}")
    base = None
    for name, fn in MODES:
        Xtr, ytr = build(samples[:split], fn)
        Xte, yte = build(samples[split:], fn)
        m = make_pipeline(StandardScaler(), LogisticRegression(max_iter=3000)).fit(Xtr, ytr)
        p = m.predict_proba(Xte)[:, 1]
        ll = log_loss(yte, p)
        if base is None:
            base = ll
        print(f"{name:24s} {ll:10.5f} {accuracy_score(yte,(p>=0.5).astype(int)):9.3f} {(ll-base)/base*100:+8.2f}%")


if __name__ == "__main__":
    main()

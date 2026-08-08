"""Advisory flag: where does a fuller MMA model disagree with the shipped price?

WHAT THIS IS FOR. The MMA moneyline price comes from Elo alone -- elo_mma states
it carries "zero fighter-feature data (styles, physical...)". Measured against
held-out fights, adding style and DEFENCE features makes the model meaningfully
more accurate (-1.63% log loss, +1.6pp accuracy; defence is the single biggest
block, scripts/check_mma_defence_features.py).

Whether that accuracy is an EDGE is unresolved and cannot be resolved offline:
there is no free historical UFC odds archive, and this app's own settled MMA bets
are a SELECTED sample (placed only where the model already disagreed with the
market), so they cannot measure calibration against the market either. On live
markets the fuller model moved toward the market on only 9 of 17 fights -- a coin
flip, unlike the style block alone which went toward on 11 of 13.

So the fuller model is NOT used to price anything. Doing that would swap a known
model for an unproven one. But throwing the information away is also wrong, and
this is the middle path the app already uses elsewhere (racing's pre-qualifying
note, the CFB/MLS approximate badges, MLB's naive-total note): surface it, change
nothing.

WHAT THE NUMBER MEANS. |p_full - p_elo| for one fight. A large value says the
shipped Elo price and a demonstrably more accurate model disagree about this
specific matchup -- which is the honest signal that Elo may be missing something
here (a fighter with bad takedown defence facing a wrestler, say, which Elo
cannot see and the fuller model can). It does NOT say who is right.

Deliberately NOT a staking gate. The fuller model has never beaten the market on
any measured sample, so using it to suppress or size bets would be acting on an
unproven belief. It is a caution, in the same spirit as flagging an MLB game with
no announced pitcher: the bet is still there, you just know one more thing about
its uncertainty.

Fails soft everywhere. Any error, missing fighter, or thin history returns None
and no flag -- MMA pricing must never depend on this module working.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from collections import defaultdict

log = logging.getLogger("mma_model_disagreement")

# Flag threshold. The mean absolute shift this model applies on live fights was
# measured at 0.071 (17 fights, scripts/check_mma_defence_vs_market.py), so 0.10
# is roughly 1.4x typical -- it surfaces genuine outliers rather than the routine
# wobble that would fire on most of the card and mean nothing.
DISAGREEMENT_THRESHOLD = 0.10

MIN_FIGHTS = 2
EPS = 1e-9

_cache: dict = {"model": None, "elo": None, "roll": None, "snaps": None,
                "reach": None, "refreshed_at": None}
_lock = threading.Lock()

OFF_KEYS = ["ss_pm", "ss_acc", "td_p15", "td_acc", "ctrl_pm", "kd_pm", "sub_p15"]
DEF_KEYS = ["ss_abs_pm", "ss_def", "td_conc_p15", "td_def", "ctrl_conc_pm", "kd_abs_pm"]


def _f(v, d=0.0):
    return d if v is None else float(v)


def _of(s):
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


def _secs(row) -> float:
    try:
        rnd = int(float(row.get("round") or 1))
    except Exception:
        rnd = 1
    return max((rnd - 1) * 300.0 + _mmss(row.get("time")), 1.0)


class _Roll:
    """Career totals for one fighter. Rates are per MINUTE of cage time, not per
    fight -- absorbing 40 strikes across a 25-minute decision is a different
    fighter from one absorbing 40 in a 90-second loss, and per-fight averages
    cannot tell them apart."""

    __slots__ = ("secs", "n", "ss_l", "ss_a", "ss_abs", "ss_faced", "td_l", "td_a",
                 "td_conc", "td_faced", "ctrl", "ctrl_conc", "kd", "kd_abs", "sub")

    def __init__(self):
        for s in self.__slots__:
            setattr(self, s, 0.0)

    def snap(self):
        if self.n < MIN_FIGHTS or self.secs <= 0:
            return None
        mins = self.secs / 60.0
        p15 = self.secs / 900.0
        return {
            "ss_pm": self.ss_l / mins, "ss_abs_pm": self.ss_abs / mins,
            "ss_acc": self.ss_l / (self.ss_a + EPS),
            "ss_def": 1.0 - self.ss_abs / (self.ss_faced + EPS),
            "td_p15": self.td_l / (p15 + EPS), "td_conc_p15": self.td_conc / (p15 + EPS),
            "td_acc": self.td_l / (self.td_a + EPS),
            "td_def": 1.0 - self.td_conc / (self.td_faced + EPS),
            "ctrl_pm": self.ctrl / mins, "ctrl_conc_pm": self.ctrl_conc / mins,
            "kd_pm": self.kd / mins, "kd_abs_pm": self.kd_abs / mins,
            "sub_p15": self.sub / (p15 + EPS),
        }

    def add(self, own, opp, secs):
        self.secs += secs
        self.n += 1
        l, a = _of(own.get("Sig. str."));   self.ss_l += l;    self.ss_a += a
        ol, oa = _of(opp.get("Sig. str.")); self.ss_abs += ol; self.ss_faced += oa
        tl, ta = _of(own.get("Td"));        self.td_l += tl;   self.td_a += ta
        otl, ota = _of(opp.get("Td"));      self.td_conc += otl; self.td_faced += ota
        self.ctrl += _mmss(own.get("Ctrl"))
        self.ctrl_conc += _mmss(opp.get("Ctrl"))
        self.kd += _f(own.get("KD"))
        self.kd_abs += _f(opp.get("KD"))
        self.sub += _f(own.get("Sub. att"))


def _style(a, b, ra, rb):
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


def refresh() -> None:
    """Train the fuller model on all settled fights and snapshot every fighter.
    Called from the MMA poller after ratings refresh. Never raises."""
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        from app.ingestion import ufc_data
        from app.models import mma_features
        from app.models.baseline import elo_mma

        fights = ufc_data.load_fights()
        raw = ufc_data.load_raw_fight_rows()
        bios = ufc_data.load_fighter_bios()
        rows = mma_features.build_feature_rows(fights, raw, bios)
        by_id = {f["id"]: f for f in fights}
        raw_by = {}
        for r in raw:
            fid = r["fight_url"].rstrip("/").rsplit("/", 1)[-1]
            raw_by[(fid, r["fighter_id"])] = r

        BASE, K = elo_mma.BASE_RATING, elo_mma.K
        elo: dict[str, float] = {}
        roll: dict[str, _Roll] = defaultdict(_Roll)
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
                    st = [
                        _f(fr["a_ko_win_rate"]) * _f(fr["b_ko_loss_rate"]) - _f(fr["b_ko_win_rate"]) * _f(fr["a_ko_loss_rate"]),
                        _f(fr["a_sub_win_rate"]) * _f(fr["b_sub_loss_rate"]) - _f(fr["b_sub_win_rate"]) * _f(fr["a_sub_loss_rate"]),
                        _f(fr["a_avg_td_landed"]) - _f(fr["b_avg_td_landed"]),
                        _f(fr["a_avg_sig_str_landed"]) - _f(fr["b_avg_sig_str_landed"]),
                        _f(fr["a_finish_rate"]) - _f(fr["b_finish_rate"]),
                        _f(fr["a_reach_in"], 71.0) - _f(fr["b_reach_in"], 71.0),
                        _f(fr["a_experience"]) - _f(fr["b_experience"]),
                        _f(fr["a_win_rate"]) - _f(fr["b_win_rate"]),
                    ]
                    vec = [ed] + st + [sa[k] - sb[k] for k in OFF_KEYS] + [sa[k] - sb[k] for k in DEF_KEYS]
                    lab = 1 if w == a else 0
                    Xe += [[ed], [-ed]]
                    Xf += [vec, [-v for v in vec]]
                    y += [lab, 1 - lab]
            if ra_row and rb_row:
                s = _secs(ra_row)
                roll[a].add(ra_row, rb_row, s)
                roll[b].add(rb_row, ra_row, s)
            if w in (a, b):
                pa = 1.0 / (1.0 + 10 ** ((elo.get(b, BASE) - elo.get(a, BASE)) / 400.0))
                sa_ = 1.0 if w == a else 0.0
                elo[a] = elo.get(a, BASE) + K * (sa_ - pa)
                elo[b] = elo.get(b, BASE) + K * ((1 - sa_) - (1 - pa))

        if len(y) < 500:
            log.info("mma disagreement: only %d training rows, skipping", len(y) // 2)
            return

        m_elo = make_pipeline(StandardScaler(), LogisticRegression(max_iter=3000)).fit(np.array(Xe), np.array(y))
        m_full = make_pipeline(StandardScaler(), LogisticRegression(max_iter=3000)).fit(np.array(Xf), np.array(y))

        with _lock:
            _cache.update({
                "model": (m_elo, m_full), "elo": dict(elo),
                "roll": {k: v.snap() for k, v in roll.items()},
                "snaps": mma_features.compute_current_snapshots(fights, raw, as_of=dt.date.today()),
                "reach": {fid: mma_features.parse_reach_inches(bb.get("reach")) for fid, bb in bios.items()},
                "refreshed_at": dt.datetime.utcnow(),
            })
        log.info("mma disagreement model refreshed: %d fights, %d fighters rolled", len(y) // 2, len(roll))
    except Exception:
        log.exception("mma disagreement refresh failed; flag stays off")


def disagreement(fighter_a_id: str, fighter_b_id: str) -> float | None:
    """|p_full - p_elo| for this matchup, or None if it cannot be computed.
    Never raises -- pricing must not depend on this."""
    try:
        with _lock:
            models = _cache.get("model")
            elo, roll, snaps, reach = (_cache.get("elo"), _cache.get("roll"),
                                       _cache.get("snaps"), _cache.get("reach"))
        if not models or not elo or not roll:
            return None
        sa, sb = roll.get(fighter_a_id), roll.get(fighter_b_id)
        na, nb = (snaps or {}).get(fighter_a_id), (snaps or {}).get(fighter_b_id)
        if not (sa and sb and na and nb):
            return None
        from app.models.baseline import elo_mma
        BASE = elo_mma.BASE_RATING
        ed = elo.get(fighter_a_id, BASE) - elo.get(fighter_b_id, BASE)
        vec = ([ed] + _style(na, nb, (reach or {}).get(fighter_a_id), (reach or {}).get(fighter_b_id))
               + [sa[k] - sb[k] for k in OFF_KEYS] + [sa[k] - sb[k] for k in DEF_KEYS])
        m_elo, m_full = models
        p_elo = float(m_elo.predict_proba([[ed]])[0][1])
        p_full = float(m_full.predict_proba([vec])[0][1])
        return abs(p_full - p_elo)
    except Exception:
        return None


def note_for(fighter_a_id: str, fighter_b_id: str) -> str | None:
    """Advisory note when the fuller model materially disagrees, else None."""
    d = disagreement(fighter_a_id, fighter_b_id)
    if d is None or d < DISAGREEMENT_THRESHOLD:
        return None
    return (
        f"Extra caution on this fight: this price uses fighter Elo only, and a fuller model that "
        f"also reads style and defensive stats (takedown defence, strikes absorbed, control time) "
        f"disagrees with it by {d*100:.0f} points here. That fuller model is measurably more "
        f"accurate on past fights but has never been shown to beat the market, so it is not used "
        f"to price anything. Read this as 'the shipped model may be missing something specific "
        f"about this matchup', not as a reason the bet is wrong."
    )

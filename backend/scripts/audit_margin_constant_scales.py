"""Does each sport's shipped MARGIN_SLOPE reproduce on the Elo scale it is USED on?

WHY THIS EXISTS. CFB's MARGIN_SLOPE was fitted by a replay that imported
app.models.baseline.elo -- the NFL module, K=20 with 1/3 season regression --
while production prices off elo_cfb, K=100 with none. Over the same 4,836 games
those scales give elo_diff sd 127.3 against 229.8, so the constant was ~65% too
steep everywhere it was applied. Fixed 2026-08-14.

THE REASON A SCRIPT IS NEEDED RATHER THAN A CODE REVIEW. The original CFB fit had
a completely healthy five-fold out-of-sample table: slope stable to +/-2%,
held-out residuals centred within a point. A stable fit on the wrong ruler is
still stable, so NO amount of out-of-sample validation can catch this. The only
test that can is re-deriving the constant with a replay built from the module
production actually uses, and comparing.

Reading imports is not sufficient either. It would have caught CFB, but a script
can import the right module and still diverge -- by skipping season regression,
using a different home-field constant, or filtering games differently. This
compares NUMBERS, which subsumes all of those.

WHAT "MATCHING" MEANS. Each sport is replayed through its OWN primitives:
elo_nba/elo_mlb expose predict_and_update, which applies SEASON_REGRESSION at
season boundaries; using the lower-level update_ratings instead skips it, widens
the ratings and shrinks the refit slope by ~12%. That is a harness artifact, not
a defect -- and it is exactly the kind of near-miss that makes a 10-15% tolerance
the wrong place to raise an alarm. CFB's real defect was 39%.

    < 15%   OK       reproduces; any gap is harness detail
    15-40%  CHECK    worth reading the fitting script's imports
    > 40%   MISMATCH the CFB signature

WNBA is handled separately: its slope is DERIVED in closed form rather than
fitted, so there is no replay to compare. What is checked instead is the
derivation's own assumption -- log(10)/1600 encodes a /400 Elo logistic (1600 =
4*400), so if that sport's Elo ever stopped dividing by 400 the formula would be
silently wrong.

Run: backend/.venv/Scripts/python.exe scripts/audit_margin_constant_scales.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import inspect  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402

import numpy as np  # noqa: E402

DATA = Path(__file__).resolve().parents[2] / "data"
OK, CHECK = 0.15, 0.40


def _verdict(off: float) -> str:
    if off < OK:
        return "OK"
    return "CHECK" if off < CHECK else "** MISMATCH **"


def _report(label, shipped_slope, shipped_std, refit, resid_sd, n, extra=""):
    off = abs(refit - shipped_slope) / shipped_slope
    print(f"{label:6s} n={n:6d}  shipped={shipped_slope:.5f}  refit={refit:.5f}  "
          f"off={off:6.1%}  sd={resid_sd:6.2f} (shipped {shipped_std})  "
          f"{_verdict(off)}{extra}")


def _replay(elo, rows, hfa, neutral_fn, use_predict):
    state = elo.EloState()
    diffs, margins = [], []
    for g in rows:
        adv = 0.0 if neutral_fn(g) else float(hfa)
        diffs.append((state.get(g["home_team"]) + adv) - state.get(g["away_team"]))
        margins.append(g["home_score"] - g["away_score"])
        if use_predict:
            elo.predict_and_update(state, g)
        else:
            elo.update_ratings(state, g["home_team"], g["away_team"],
                               g["home_score"], g["away_score"], adv)
    d, m = np.array(diffs), np.array(margins)
    slope = float(np.sum(d * m) / np.sum(d * d))
    return slope, (m - slope * d).std(), len(rows)


def _load(name, keep=None):
    path = DATA / name
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    rows = raw.values() if isinstance(raw, dict) else raw
    out = [g for g in rows
           if isinstance(g, dict)
           and g.get("home_score") is not None and g.get("away_score") is not None
           and (keep is None or keep(g))]
    out.sort(key=lambda g: (g.get("season", 0), g.get("gameday") or g.get("date") or ""))
    return out


def main() -> None:
    print("Each sport replayed through its OWN Elo primitives, slope refitted, "
          "compared to the shipped constant.\n")

    # NFL -- games come from the ingestion layer, not a JSON cache.
    from app.ingestion import nfl_data
    from app.models import game_lines as g_nfl
    from app.models.baseline import elo as e_nfl
    rows = [g for g in nfl_data.fetch_games()
            if g.get("home_score") is not None and g.get("away_score") is not None]
    rows.sort(key=lambda g: (g.get("season", 0), g.get("week", 0)))
    st = e_nfl.EloState()
    d, m = [], []
    for g in rows:
        if hasattr(st, "start_season_if_new"):
            st.start_season_if_new(g.get("season"))
        adv = e_nfl.effective_home_field_adv(g.get("home_team"), g.get("location"))
        d.append((st.get(g["home_team"]) + adv) - st.get(g["away_team"]))
        m.append(g["home_score"] - g["away_score"])
        e_nfl.update_ratings(st, g["home_team"], g["away_team"],
                             g["home_score"], g["away_score"], adv)
    d, m = np.array(d), np.array(m)
    sl = float(np.sum(d * m) / np.sum(d * d))
    _report("NFL", g_nfl.MARGIN_SLOPE, g_nfl.MARGIN_STD, sl, (m - sl * d).std(), len(rows))

    # CFB -- the sport this audit was written for.
    from app.models import game_lines_cfb as g_cfb
    from app.models.baseline import elo_cfb as e_cfb, elo_service_cfb as s_cfb
    rows = [g for g in s_cfb._historical_games()
            if g.get("home_score") is not None and g.get("away_score") is not None]
    rows.sort(key=lambda x: (x["season"], x["gameday"], str(x["id"])))
    st = e_cfb.EloState()
    d, m = [], []
    for g in rows:
        # Ratings use elo_cfb's OWN home-field constant; the MARGIN model has its
        # own, larger one. Conflating the two shifts the slope ~5%.
        upd_adv = e_cfb.effective_home_field_adv(bool(g.get("neutral")))
        mar_adv = 0.0 if g.get("neutral") else g_cfb.HOME_FIELD_ADV
        d.append((st.get(g["home_team"]) + mar_adv) - st.get(g["away_team"]))
        m.append(g["home_score"] - g["away_score"])
        e_cfb.update_ratings(st, g["home_team"], g["away_team"],
                             g["home_score"], g["away_score"], upd_adv)
    d, m = np.array(d), np.array(m)
    sl = float(np.sum(d * m) / np.sum(d * d))
    _report("CFB", g_cfb.MARGIN_SLOPE, g_cfb.MARGIN_STD, sl, (m - sl * d).std(), len(rows))

    # NBA / MLB -- predict_and_update applies SEASON_REGRESSION; update_ratings does not.
    from app.models import game_lines_nba as g_nba, game_lines_mlb as g_mlb
    from app.models.baseline import elo_nba as e_nba, elo_mlb as e_mlb
    rows = _load("nba_schedule_cache.json")
    if rows:
        sl, sd, n = _replay(e_nba, rows, getattr(e_nba, "HOME_COURT_ADV", 48.0),
                            lambda g: str(g.get("location")) == "Neutral", True)
        _report("NBA", g_nba.MARGIN_SLOPE, g_nba.MARGIN_STD, sl, sd, n)
    rows = _load("mlb_schedule_cache.json", keep=lambda g: g.get("game_type") == "R")
    if rows:
        sl, sd, n = _replay(e_mlb, rows, getattr(e_mlb, "HOME_FIELD_ADV", 22.0),
                            lambda g: str(g.get("location")) == "Neutral", True)
        _report("MLB", g_mlb.MARGIN_SLOPE, g_mlb.MARGIN_STD, sl, sd, n)

    # WNBA -- derived, not fitted. Check the derivation's own assumption instead.
    from app.models import game_lines_wnba as g_wnba
    from app.models.baseline import elo_wnba as e_wnba
    derived = math.log(10) / 1600.0 * g_wnba.MARGIN_STD * math.sqrt(2 * math.pi)
    uses_400 = "/ 400" in inspect.getsource(e_wnba) or "/400" in inspect.getsource(e_wnba)
    agree = abs(derived - g_wnba.MARGIN_SLOPE) / g_wnba.MARGIN_SLOPE < 1e-6
    print(f"\nWNBA   slope is DERIVED, not fitted: log(10)/1600 * MARGIN_STD * sqrt(2pi)")
    print(f"       formula gives {derived:.6f}, shipped {g_wnba.MARGIN_SLOPE:.6f}  "
          f"{'OK' if agree else '** DRIFTED **'}")
    print(f"       /1600 assumes a /400 Elo logistic -- elo_wnba divides by 400? "
          f"{'yes, OK' if uses_400 else '** NO, formula is invalid **'}")

    print("\nA sport reading MISMATCH means its fitting script is replaying a different")
    print("Elo than production prices with. Check that script's baseline import first.")


if __name__ == "__main__":
    main()
